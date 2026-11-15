import asyncio
import csv
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase, override_settings
from .benchconfig import validate_options,validate_budget
from .benchrunner import Driver,benchmark
from .engine import enqueue
from .forms import ExerciseForm
from .models import Exercise,NetworkProfile,Variable
from .benchstats import target


class Benchmarks(TransactionTestCase):
    def setUp(self):
        observed=self.observed=[]
        class Endpoint(BaseHTTPRequestHandler):
            def do_GET(req):
                path=urlsplit(req.path)
                observed.append((path.path,req.headers.get("X-Session")))
                if path.path=="/lookup":
                    item=parse_qs(path.query)["sku"][0]
                    code,body=200,json.dumps({"item":{"id":item}}).encode()
                elif path.path.startswith("/items/"):
                    code,body=200,b"ok"
                elif path.path=="/redirect":
                    code,body=302,b""
                else: code,body=503,b"no"
                req.send_response(code)
                if code==302: req.send_header("Location","/items/should-never-be-read")
                req.send_header("X-Session","live-secret-not-in-samples")
                req.send_header("Content-Length",str(len(body)))
                req.end_headers()
                req.wfile.write(body)
            def log_message(self,*args): pass
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
        thread=threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":.05},daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.authority="127.0.0.1:"+str(self.server.server_port)
        self.url="http://"+self.authority
        ctx=override_settings(TEST_HOSTS={self.authority});ctx.enable();self.addCleanup(ctx.disable)

    def exercise(self,**attrs):
        attrs={"name":"Checkout","url":self.url+"/lookup","pattern":"latency","rate":1,"seconds":1,**attrs}
        return Exercise.objects.create(**attrs)

    def run_case(self,test):
        run=enqueue(test)
        asyncio.run(benchmark(run))
        run.refresh_from_db()
        return run

    def test_actual_worker_profiles_data_and_extraction(self):
        direct=NetworkProfile.objects.create(name="Lab unshaped")
        slow=NetworkProfile.objects.create(name="Lab 90 ms",latency_ms=90)
        opts=dict(count=2,profiles=[direct.pk,slow.pk],csv_data="sku\nfruit-27\nfruit-61",steps=[
            {"name":"Lookup","url":self.url+"/lookup","query":{"sku":"{{sku}}"},
             "extract":{"item":"json:item.id","session":"header:X-Session"}},
            {"name":"Read item","url":self.url+"/items/{{item}}","headers":{"X-Session":"{{session}}"}}])
        run=self.run_case(self.exercise(options=opts))
        self.assertEqual(run.state,"Completed")
        self.assertTrue(run.result["passed"])
        self.assertEqual(run.result["count"],8)
        self.assertEqual([p["count"] for p in run.result["profiles"]],[4,4])
        self.assertGreater(run.result["profiles"][1]["mean"],run.result["profiles"][0]["mean"]+45)
        self.assertEqual(sum(h["count"] for h in run.result["histogram"]),8)
        self.assertEqual(set(run.result["phases"]),{"dns","tcp","tls","ttfb","download"})
        self.assertEqual([p for p in self.observed if p[0].startswith("/items")],
            [("/items/fruit-27","live-secret-not-in-samples"),("/items/fruit-61","live-secret-not-in-samples")]*2)
        self.client.force_login(get_user_model().objects.create_user("reader"))
        report=self.client.get(f"/runs/{run.pk}");self.assertContains(report,"Network profile comparison")
        self.assertContains(report,"Request timeline")
        exported=self.client.get(f"/runs/{run.pk}/csv/").content.decode()
        self.assertEqual(len(list(csv.reader(io.StringIO(exported)))),9)
        for needle in ("fruit-27","live-secret-not-in-samples"):
            self.assertNotIn(needle, json.dumps([run.config,run.result,run.samples,run.series])+exported)

    def test_runtime_rechecks_variable_host_and_rejects_redirect(self):
        var=Variable(name="BASE_URL",environment="qa");var.set_value(self.url);var.save()
        form=ExerciseForm({"name":"Variable host","url":"{{BASE_URL}}/redirect","environment":"qa",
            "pattern":"latency","rate":1,"seconds":1,"max_p95":0,"max_error_pct":0,"repeat_minutes":0})
        self.assertTrue(form.is_valid(),form.errors)
        test=form.save();run=self.run_case(test)
        self.assertEqual([r[2] for r in run.samples],[302]);self.assertEqual(len(self.observed),1)
        frozen=enqueue(test)
        var.set_value("http://outside.invalid");var.save()
        asyncio.run(benchmark(frozen));frozen.refresh_from_db()
        self.assertFalse(frozen.result["passed"])
        self.assertEqual(frozen.result["errors"],1);self.assertEqual(len(self.observed),1)

    def test_budget_stages_and_scenario_errors(self):
        with self.assertRaises(ValidationError):
            validate_options({"steps":[{"url":"http://unapproved.invalid"}]},self.url)
        with self.assertRaises(ValidationError):
            validate_options({"csv_data":"token\nsecret"},self.url)
        opts=validate_options({"stages":[{"seconds":1,"target":2},{"seconds":2,"target":4}]},self.url)
        validate_budget(opts,"ramp",4,3)
        self.assertEqual([target({"rate":4,"seconds":3,"pattern":"ramp"},opts,n) for n in range(3)],[2,4,4])
        with self.assertRaises(ValidationError): validate_budget(opts,"fixed",4,4)
        opts=validate_options({"count":10000,"steps":[{"name":str(i)} for i in range(4)]},self.url)
        with self.assertRaises(ValidationError): validate_budget(opts,"latency",1,1)
        run=self.run_case(self.exercise(options={"steps":[{"url":self.url+"/items/a","extract":{"missing":"json:id"}},
            {"url":self.url+"/items/unreachable"}]}))
        self.assertEqual(run.result["failures"],{"Extraction failed":1})
        self.assertFalse(any(p[0].endswith("unreachable") for p in self.observed))

    def test_users_stress_and_stop(self):
        stress=self.exercise(url=self.url+"/unhealthy",pattern="stress",rate=6,seconds=4,options={
            "model":"users","users":3,"stages":[{"seconds":2,"target":2},{"seconds":2,"target":3}]})
        run=self.run_case(stress)
        self.assertEqual(run.state,"Threshold reached")
        self.assertEqual(run.result["breakpoint"]["reason"],"Error rate exceeded")
        self.assertLess(run.result["count"],100)
        latency=self.exercise(url=self.url+"/items/ok",options={"count":50,"delay_ms":1000})
        frozen=enqueue(latency)
        async def stop():
            work=asyncio.create_task(benchmark(frozen))
            await asyncio.sleep(.35)
            await type(frozen).objects.filter(pk=frozen.pk).aupdate(stop=True)
            await asyncio.wait_for(work,3)
        asyncio.run(stop());frozen.refresh_from_db()
        self.assertEqual(frozen.state,"Stopped");self.assertLess(frozen.result["count"],3)

    def test_form_upload_random_fixtures_daily_and_status_gate(self):
        import random
        from datetime import datetime,timezone
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .benchconfig import next_schedule
        user=get_user_model().objects.create_superuser("operator")
        self.client.force_login(user)
        self.assertContains(self.client.get("/benchmarks/new/"),"bench_editor.js")
        data={"name":"Daily redirects","url":self.url+"/redirect","pattern":"latency","rate":2,"seconds":2,
              "max_p95":0,"max_error_pct":0,"repeat_minutes":0,"daily_at":"04:30","csv_order":"random",
              "csv_file":SimpleUploadedFile("ids.csv",b"sku\n21\n42\n63"),"statuses":"200-201,302",
              "count":5, "steps":json.dumps([{"name":"Load","url":self.url+"/items/{{sku}}"}])}
        res=self.client.post("/benchmarks/new/",data)
        self.assertEqual(res.status_code,302)
        test=Exercise.objects.get(name="Daily redirects")
        self.assertEqual(test.options["statuses"],[200,201,302])
        self.assertIsNotNone(test.next_at)
        self.assertEqual(next_schedule(test,datetime(2026,2,1,4,30,tzinfo=timezone.utc)),
                         datetime(2026,2,2,4,30,tzinfo=timezone.utc))
        run=self.run_case(test);rng=random.Random(run.pk)
        self.assertEqual([p[0].split("/")[-1] for p in self.observed],[rng.choice(["21","42","63"]) for _ in range(5)])
        self.assertTrue(run.result["passed"])
        test.options.update(steps=[],count=1,statuses=[200,201],min_rps=50)
        test.save()
        denied=self.run_case(test)
        self.assertFalse(denied.result["passed"])
        self.assertEqual(denied.result["failures"],{"HTTP 302":1})

    def test_throughput_gate_and_plateau(self):
        from .benchrunner import DIRECT
        from types import SimpleNamespace
        run=self.run_case(self.exercise(url=self.url+"/items/x",options={"count":2,"delay_ms":120,
            "min_rps":50}))
        self.assertEqual(run.result["errors"],0)
        self.assertFalse(run.result["passed"])
        cfg={"rate":4,"pattern":"stress","max_p95":0,"max_error_pct":0,"seconds":3}
        opts=validate_options({"plateau_windows":1},self.url)
        driver=Driver(SimpleNamespace(pk=1,config=cfg),{"url":self.url,"steps":[],"options":opts,"profiles":[DIRECT]}, {})
        for level in (1,2,3):
            driver.assess({"count":5,"errors":0,"error_pct":0,"p95":8},level)
        self.assertEqual(driver.breakpoint,{"reason":"Throughput plateau","target":2,"profile":"Direct"})
