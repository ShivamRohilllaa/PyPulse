import hashlib
import hmac
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AlertChannel, AlertDelivery, Check, CheckHour, Incident, Maintenance, Monitor, Variable, WorkspaceSettings
from .probes import seal
from .uptime_engine import alert_tick, check_monitor, in_maintenance, queue_followups, request_options, send_delivery
from .uptime_views import measurement_context


class UptimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser("owner")
        cls.reader = User.objects.create_user("reader")

    def setUp(self):
        self.client.force_login(self.owner)

    def monitor_data(self, **updates):
        defaults = dict(name="Cart API",url="https://example.org/api",interval=30,expected_status=200,contains="",
                        warn_ms=1000,failure_threshold=2,method="GET",group="Checkout",tags="Cart, edge",environment="prod")
        return {**defaults,**updates}

    def test_ui_create_edit_clone_filters_delete(self):
        data = self.monitor_data(options=json.dumps({"auth":{"type":"bearer","token":"private-value"}}))
        self.assertEqual(self.client.post(reverse("new_monitor"),data).status_code,302)
        m = Monitor.objects.get()
        self.assertNotIn("private-value",m.request_secret)
        self.assertNotContains(self.client.get(reverse("edit_monitor",args=[m.pk])),"private-value")
        data["options"] = ""
        self.assertEqual(self.client.post(reverse("edit_monitor",args=[m.pk]),data).status_code,302)
        m.refresh_from_db()
        self.assertEqual(request_options(m)["auth"]["token"],"private-value")
        self.assertContains(self.client.get(reverse("monitors"),{"tag":"cart","environment":"prod"}),"Cart API")
        self.assertNotContains(self.client.get(reverse("monitors"),{"tag":"car"}),"Cart API")
        self.client.post(reverse("monitor_action",args=[m.pk,"clone"]))
        duplicate = Monitor.objects.exclude(pk=m.pk).get()
        self.assertTrue(duplicate.paused)
        self.assertFalse(duplicate.public)
        self.assertEqual(request_options(m),request_options(duplicate))
        self.client.post(reverse("monitor_action",args=[m.pk,"pause"]))
        m.refresh_from_db()
        self.assertTrue(m.paused)
        self.assertEqual(self.client.post(reverse("monitor_action",args=[m.pk,"delete"])).status_code,302)
        self.assertFalse(Monitor.objects.filter(pk=m.pk).exists())

    def test_live_request_to_loopback_and_recovery(self):
        calls = []
        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append((self.path,self.headers.get("Authorization"),json.loads(body)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"healthy": len(calls) > 1}).encode())
            def log_message(self, *args):
                pass
        with ThreadingHTTPServer(("127.0.0.1",0),Endpoint) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                var = Variable(name="BASE_URL", environment="prod")
                var.set_value(f"http://127.0.0.1:{server.server_port}")
                var.save()
                options = {"auth":{"type":"bearer","token":"local-only"},"query":{"source":"probe"},
                           "body":{"ping":1},"json_equals":{"healthy":True}}
                data = self.monitor_data(url="{{BASE_URL}}/ready",method="POST",options=json.dumps(options),failure_threshold=1)
                response = self.client.post(reverse("new_monitor"),data)
                self.assertEqual(response.status_code,302,response.context and response.context["form"].errors)
                m = Monitor.objects.get()
                check_monitor(m.pk)
                self.assertFalse(Check.objects.get().ok)
                self.assertTrue(Incident.objects.filter(monitor=m,closed=None).exists())
                check_monitor(m.pk)
                self.assertEqual(calls,[("/ready?source=probe","Bearer local-only",{"ping":1})]*2)
                m.refresh_from_db()
                self.assertEqual(m.state,"Up")
                self.assertIsNotNone(Incident.objects.get().closed)
            finally:
                server.shutdown()
                thread.join()

    def test_accuracy_and_public_redaction(self):
        now = timezone.now()
        m = Monitor.objects.create(name="Cart",url="https://example.org/private?token=private-value",public=True)
        for hours,ms,ok in [(2,50,True),(1,80,False),(25,450,True)]:
            Check.objects.create(monitor=m,at=now-timedelta(hours=hours),ms=ms,ok=ok,status=200)
        self.assertContains(Client().get(reverse("status")),"50.00%")
        self.assertNotContains(Client().get(reverse("status")),"private")
        detail = self.client.get(reverse("monitor",args=[m.pk]))
        self.assertNotContains(detail,"private-value")
        self.assertNotContains(self.client.get(reverse("edit_monitor",args=[m.pk])),"private-value")
        self.assertEqual(detail.context["mean"],65)
        self.assertEqual(detail.context["p95"],80)
        CheckHour.objects.create(monitor=m,hour=(now-timedelta(days=2)).replace(minute=0,second=0,microsecond=0),
            total=2,passed=1,min_ms=20,max_ms=60,elapsed_sum_ms=80)
        ctx=measurement_context(m,"7d")
        self.assertEqual((ctx["count"],ctx["mean"],ctx["availability"],ctx["p95"]),(5,132,60,450))
        partial=self.client.get(reverse("monitor",args=[m.pk]),{"partial":1})
        self.assertNotContains(partial,"<!doctype")
        self.assertContains(partial,"Response time")
        workspace=WorkspaceSettings.current()
        workspace.status_enabled=False
        workspace.save()
        self.assertEqual(Client().get(reverse("status")).status_code,404)

    def test_channel_validation_write_only_and_test_delivery(self):
        data=dict(name="on-call",kind="webhook",enabled="on",configuration=json.dumps({"url":"http://127.0.0.1:8000/hook","secret":"sensitive-value"}))
        self.assertEqual(self.client.post(reverse("new_channel"),data).status_code,302)
        channel=AlertChannel.objects.get()
        self.assertNotIn("sensitive-value",channel.config_secret)
        self.assertNotContains(self.client.get(reverse("edit_channel",args=[channel.pk])),"sensitive-value")
        invalid={**data,"name":"","kind":"email"}
        self.assertNotContains(self.client.post(reverse("new_channel"),invalid),"sensitive-value")
        self.client.post(reverse("channel_action",args=[channel.pk,"test"]))
        with patch("console.uptime_engine.post_json") as sink:
            alert_tick()
        _,body,headers=sink.call_args.args
        self.assertEqual(hmac.new(b"sensitive-value",body,hashlib.sha256).hexdigest(),headers["X-PyPulse-Signature"])
        self.assertEqual(AlertDelivery.objects.get().disposition,"Delivered")
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(reverse("channel_action",args=[channel.pk,"test"])).status_code,403)

    def test_followups_and_recurring_maintenance(self):
        now=timezone.now()
        m=Monitor.objects.create(name="Orders",url="https://example.org",reminder_minutes=2,state="Down")
        issue=Incident.objects.create(monitor=m,next_reminder=now,next_escalation=now)
        ordinary=AlertChannel.objects.create(name="main",kind="slack",config_secret=seal('{}'))
        escalate=AlertChannel.objects.create(name="oncall",kind="teams",escalation_only=True,config_secret=seal('{}'))
        m.channels.add(ordinary)
        m.escalation_channel=escalate
        m.save()
        window=Maintenance.objects.create(monitor=m,start=now-timedelta(days=8,minutes=15),end=now-timedelta(days=8)+timedelta(minutes=15),recurrence="daily")
        self.assertTrue(in_maintenance(m,now))
        queue_followups(now)
        self.assertFalse(AlertDelivery.objects.exists())
        window.delete()
        queue_followups(now+timedelta(minutes=15))
        queue_followups(now+timedelta(minutes=15))
        self.assertEqual(set(AlertDelivery.objects.values_list("event","channel_id")),{("Reminder",ordinary.pk),("Escalation",escalate.pk)})
        self.client.post(reverse("ack",args=[issue.pk]))
        queue_followups(now+timedelta(hours=1))
        issue.refresh_from_db()
        self.assertEqual(issue.acknowledged_by,"owner")
        self.assertEqual(AlertDelivery.objects.count(),2)

    def test_teams_slack_smtp_payloads_and_retry(self):
        for kind in ("slack","teams"):
            channel=AlertChannel.objects.create(name=kind,kind=kind,config_secret=seal(json.dumps({"url":"https://example.invalid"})))
            delivery=AlertDelivery.objects.create(channel=channel,event="Test",state="Test")
            with patch("console.uptime_engine.post_json") as sink:
                send_delivery(delivery)
            payload=json.loads(sink.call_args.args[1])
            self.assertIn("Test",payload["text"] if kind=="slack" else payload["attachments"][0]["content"]["body"][0]["text"])
            delivery.delete()
        config={"host":"smtp.example.invalid","port":587,"from":"a@example.org","to":["b@example.org"],"security":"starttls"}
        channel=AlertChannel.objects.create(name="mail",kind="email",config_secret=seal(json.dumps(config)))
        delivery=AlertDelivery.objects.create(channel=channel,event="Test",state="Test")
        with patch("console.uptime_engine.smtplib.SMTP") as smtp:
            send_delivery(delivery)
            client=smtp.return_value.__enter__.return_value
            client.starttls.assert_called_once()
            self.assertEqual(client.send_message.call_args.args[0]["To"],"b@example.org")
        with patch("console.uptime_engine.send_delivery",side_effect=OSError):
            for attempt in range(3):
                AlertDelivery.objects.filter(pk=delivery.pk).update(due=timezone.now())
                alert_tick()
        delivery.refresh_from_db()
        self.assertEqual((delivery.disposition,delivery.attempts),("Failed",3))

    def test_90_day_rollups_and_incident_pagination(self):
        m=Monitor.objects.create(name="Payments",url="https://example.org",public=False)
        now=timezone.now()
        Check.objects.create(monitor=m,ms=100,ok=True,slow=True)
        CheckHour.objects.create(monitor=m,hour=(now-timedelta(days=45)).replace(minute=0,second=0,microsecond=0),
            total=3,passed=2,slow_count=1,elapsed_sum_ms=600,min_ms=100,max_ms=300)
        Incident.objects.bulk_create([Incident(monitor=m,opened=now-timedelta(days=i),closed=now) for i in range(28)])
        self.client.force_login(self.reader)
        detail=self.client.get(reverse("monitor",args=[m.pk]),{"range":"90d","ipage":"2"})
        self.assertContains(detail,"Page 2 of 3")
        self.assertEqual([detail.context[k] for k in ("count","availability","mean","p99","slow_count","incident_count")],
                         [4,75,175,100,2,28])
        self.assertContains(self.client.get(reverse("incidents"),{"page":2}),"Page 2 of 2")
        self.assertEqual(self.client.post(reverse("monitor_action",args=[m.pk,"pause"])).status_code,403)
        self.assertNotContains(Client().get(reverse("status")),"Payments")

    def test_admin_and_publication_settings(self):
        workspace=WorkspaceSettings.current()
        workspace.status_title="Example API status"
        workspace.interval, workspace.warn_ms = 120, 750
        workspace.save()
        form_response=self.client.get(reverse("new_monitor"))
        self.assertEqual(form_response.context["form"].initial["interval"],120)
        self.assertEqual(form_response.context["form"].initial["warn_ms"],750)
        self.assertEqual(self.client.get(reverse("admin:console_monitor_add")).status_code,200)
        data=self.monitor_data(public="on",options=json.dumps({"query":{"secret":"never-show-this"}}))
        self.client.post(reverse("new_monitor"),data)
        m=Monitor.objects.get()
        self.assertNotContains(self.client.get(reverse("admin:console_monitor_change",args=[m.pk])),"never-show-this")
        public=Client().get(reverse("status"))
        self.assertContains(public,"Example API status")
        self.assertContains(public,m.name)
        self.assertNotContains(public,"never-show-this")
        self.assertNotContains(public,m.url)
        self.client.post(reverse("monitor_action",args=[m.pk,"pause"]))
        self.assertNotContains(Client().get(reverse("status")),m.name)

    def test_search_and_state_filters_compose_and_hide_legacy_query(self):
        primary=Monitor.objects.create(name="Checkout health",url="https://orders.example.org/ready?token=old-secret",
                                       state="Down",group="Commerce",environment="prod",tags="edge")
        Monitor.objects.create(name="Paused payments",url="https://orders.example.org/pay",state="Down",paused=True)
        Monitor.objects.create(name="Orders staging",url="https://staging.example.org/ready",state="Up")
        for params in ({"q":"CHECKOUT"},{"q":"ORDERS.EXAMPLE","state":"down","group":"Commerce","tag":"edge","environment":"prod"}):
            response=self.client.get(reverse("monitors"),params)
            self.assertEqual([m.pk for m in response.context["monitors"]],[primary.pk])
            self.assertEqual(response.context["down"],1)
        paused=self.client.get(reverse("monitors"),{"state":"paused","q":"orders.example"})
        self.assertEqual([m.name for m in paused.context["monitors"]],["Paused payments"])
        self.assertEqual(paused.context["down"],0)
        self.assertFalse(self.client.get(reverse("monitors"),{"q":"old-secret"}).context["monitors"])

    def test_shared_and_workspace_wide_maintenance(self):
        now=timezone.now().replace(second=0,microsecond=0)
        a,b,c=(Monitor.objects.create(name=n,url="https://example.org",public=True) for n in ("Alpha","Beta","Gamma"))
        data={"name":"Release window","start":(now-timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"),
              "end":(now+timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"),"recurrence":"weekly"}
        location=reverse("maintenance")
        self.assertEqual(self.client.post(location,{**data,"targets":[a.pk,b.pk]}).status_code,302)
        shared=Maintenance.objects.get()
        self.assertEqual(set(shared.targets.values_list("pk",flat=True)),{a.pk,b.pk})
        self.assertIsNone(shared.monitor_id)
        self.assertTrue(in_maintenance(a,now))
        self.assertFalse(in_maintenance(c,now))
        self.assertTrue(in_maintenance(b,now+timedelta(days=7)))
        self.assertContains(self.client.get(reverse("monitor",args=[a.pk])),"Release window")
        page=self.client.get(location)
        self.assertContains(page,"Alpha")
        self.assertContains(page,"Beta")
        self.assertContains(page,"Active")
        self.assertContains(Client().get(reverse("status")),"Scheduled maintenance",count=2)
        self.assertEqual(self.client.post(location,{**data,"targets":[a.pk],"all_monitors":"on"}).status_code,400)
        self.assertEqual(self.client.post(location,data).status_code,400)
        self.assertEqual(self.client.post(location,{**data,"name":"Platform update","all_monitors":"on"}).status_code,302)
        later=Monitor.objects.create(name="Created later",url="https://example.org")
        self.assertTrue(in_maintenance(later,now))
        global_window=Maintenance.objects.get(all_monitors=True)
        self.client.post(reverse("remove_shared_window",args=[global_window.pk]))
        self.assertFalse(in_maintenance(c,now))
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(location).status_code,200)
        self.assertEqual(self.client.post(location,{**data,"targets":[a.pk]}).status_code,403)
        self.assertEqual(self.client.post(reverse("remove_shared_window",args=[shared.pk])).status_code,403)
        self.assertEqual(Maintenance.objects.count(),1)

    def test_preview_is_bounded_authorized_and_does_not_record(self):
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        data=self.monitor_data(options=json.dumps({"auth":{"type":"bearer","token":"secret-input"}}),timeout_ms=60000)
        m=Monitor.objects.create(name="Saved",url="https://example.org",state="Down",failures=3)
        endpoint=reverse("edit_monitor_preview",args=[m.pk])
        with patch("console.uptime_engine.probes.probe", return_value={
                "status":200,"error":"","ms":42.5,"headers":{"Set-Cookie":"secret-output"},"body":b"secret-response"}) as probe:
            self.assertEqual(client.post(endpoint,data).status_code,403)
            self.assertEqual(client.get(endpoint).status_code,405)
            client.get(reverse("edit_monitor",args=[m.pk]))
            data["csrfmiddlewaretoken"]=client.cookies["csrftoken"].value
            response=client.post(endpoint,data,HTTP_ACCEPT="application/json")
            self.assertEqual(response.json(),{"result":"Passed","http":200,"ms":42.5})
            self.assertEqual(probe.call_args.kwargs["timeout_ms"],5000)
            self.assertEqual(probe.call_args.args[0]["auth"]["token"],"secret-input")
            self.assertNotIn(b"secret",response.content)
            data["url"]="invalid URL"
            self.assertEqual(client.post(endpoint,data,HTTP_ACCEPT="application/json").status_code,400)
            client.force_login(self.reader)
            self.assertEqual(client.post(endpoint,data).status_code,403)
            self.assertEqual(client.post(reverse("new_monitor_preview"),data).status_code,403)
            probe.assert_called_once()
        m.refresh_from_db()
        self.assertEqual((m.name,m.failures,m.state),("Saved",3,"Down"))
        self.assertFalse(Check.objects.exists())
        self.assertFalse(Incident.objects.exists())
        self.assertFalse(AlertDelivery.objects.exists())
        self.assertEqual(Monitor.objects.count(),1)

    def test_failed_slow_pagination_and_latest_measurement(self):
        m=Monitor.objects.create(name="Orders",url="https://example.org")
        now=timezone.now()
        Check.objects.bulk_create([Check(monitor=m,at=now-timedelta(minutes=i+1),ms=i+1,ok=i%2==0,
                                   slow=i%4==0,status=200 if i%2==0 else 503) for i in range(60)])
        latest=Check.objects.create(monitor=m,at=now,ms=437,ok=True,status=200)
        listing=self.client.get(reverse("monitors"))
        self.assertEqual(listing.context["monitors"][0].last_check,latest.at)
        self.assertContains(listing,"437.0 ms")
        for scope,n in (("failed",30),("slow",15),("issues",45)):
            response=self.client.get(reverse("monitor",args=[m.pk]),{"result":scope,"checks_page":2})
            page=response.context["checks"]
            self.assertEqual(page.paginator.count,n)
            self.assertEqual(page.number,min(2,page.paginator.num_pages))
            self.assertEqual(response.context["count"],61)
            if scope=="failed":
                self.assertTrue(all(not item.ok for item in page))
                self.assertContains(response,"Newer checks")

    def test_unsaved_monitor_preview_reports_assertion_without_persisting(self):
        form=self.client.get(reverse("new_monitor"))
        self.assertContains(form,reverse("new_monitor_preview"))
        options={"json_equals":{"ready":True}}
        data=self.monitor_data(options=json.dumps(options),timeout_ms=600)
        response_data={"status":200,"error":"","ms":10,"body":b'{"ready":false,"private":"sensitive"}',"headers":{}}
        with patch("console.uptime_engine.probes.probe",return_value=response_data) as probe:
            response=self.client.post(reverse("new_monitor_preview"),data)
            self.assertContains(response,"JSON value mismatch")
            self.assertNotContains(response,"sensitive")
            self.assertEqual(probe.call_args.kwargs["timeout_ms"],600)
        self.assertFalse(Monitor.objects.exists())
        self.assertFalse(Check.objects.exists())
