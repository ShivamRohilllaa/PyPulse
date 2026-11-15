import asyncio
import csv
import gzip
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
            protocol_version="HTTP/1.1"
            def do_GET(req):
                path=urlsplit(req.path)
                observed.append((path.path,req.headers.get("X-Session")))
                if path.path=="/lookup":
                    item=parse_qs(path.query)["sku"][0]
                    code,body=200,json.dumps({"item":{"id":item}}).encode()
                elif path.path.startswith("/items/"):
                    code,body=200,b"ok"
                elif path.path=="/compressed":
                    code,body=200,gzip.compress(b"product=42")
                elif path.path=="/redirect":
                    code,body=302,b""
                else: code,body=503,b"no"
                req.send_response(code)
                if path.path=="/compressed": req.send_header("Content-Encoding","gzip")
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
        self.assertCountEqual([p for p in self.observed if p[0].startswith("/items")],
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
        driver.stage_level=2
        driver.best={"target":1,"rps":5}
        stats={"count":5,"answered":5,"errors":0,"error_pct":0,"rps":5,"p95":8}
        self.assertEqual(driver.stage_violation(stats,final=True),"Throughput plateau")

    def test_compression_connection_reuse_and_regex_extraction(self):
        from .benchconfig import statuses
        self.assertEqual(statuses("2XX, 304, 500 - 501"),[*range(200,300),304,500,501])
        test = self.exercise(options={"count":2, "steps":[
            {"name":"Product", "url":self.url+"/compressed", "extract":{"product":"regex:product=(\\d+)"}},
            {"name":"Detail", "url":self.url+"/items/{{product}}"}]})
        run = self.run_case(test)
        self.assertTrue(run.result["passed"], run.result["gate_reasons"])
        self.assertEqual(run.result["profiles"][0]["compressed"],2)
        self.assertGreaterEqual(run.result["profiles"][0]["reused"],2)
        self.assertEqual([path for path,_ in self.observed],["/compressed","/items/42"]*2)
        with self.assertRaises(ValidationError):
            validate_options({"steps":[{"extract":{"value":"regex:["}}]},self.url)

    def test_parallel_latency_profiles_each_get_their_own_gate(self):
        fast = NetworkProfile.objects.create(name="Fast test")
        slow = NetworkProfile.objects.create(name="Slow test")
        options={"count":1,"profiles":[fast.pk,slow.pk],"max_avg_ms":60,"max_p99_ms":95}
        barrier = threading.Barrier(2, timeout=3)
        def request(_spec, _values, profile, **kwargs):
            barrier.wait()
            return dict(status=200, ms=80 if profile["name"]==slow.name else 10, sent=0, received=5,
                        error="", phases={"dns":1}, body=b"", headers={}, reused=False, compressed=False)
        with patch("console.benchrunner.probe", request):
            run = self.run_case(self.exercise(options=options))
        self.assertEqual(run.result["mean"],45)
        self.assertEqual([(c["profile"], c["label"]) for c in run.result["checks"] if not c["passed"]],
                         [(slow.name, "Mean latency (ms)")])
        self.assertFalse(run.result["passed"])
        self.assertEqual(run.result["answered"],2)
        self.assertEqual(run.result["stddev"],35)

    def test_transport_failures_do_not_mask_slow_answered_requests(self):
        from .benchstats import full_report
        rows = [[0,13,200,"Step","Direct","",{"dns":1},0,10,{"answered":True}],
                [.1,5000,0,"Step","Direct","Timeout",{},0,0,{"answered":False}],
                [.2,120,503,"Step","Direct","HTTP 503",{"dns":2},0,4,{"answered":True}]]
        result = full_report(rows, 2, 0, [], {"Direct":2})
        self.assertEqual((result["count"],result["answered"],result["p95"],result["errors"]),(3,2,120,2))
        self.assertEqual(result["profiles"][0]["phases"]["dns"],1.5)
        self.assertEqual(sum(h["count"] for h in result["histogram"]),2)

    def test_shaped_workloads_and_passing_stress_stage(self):
        from .benchstats import stage_plan
        cfg = {"pattern":"spike","seconds":30,"rate":6}
        opts = validate_options({"base_load":2,"base_seconds":3,"peak_seconds":5,"recovery_seconds":2},self.url)
        self.assertEqual(stage_plan(cfg,opts),[{"target":2,"seconds":3},{"target":6,"seconds":5},{"target":2,"seconds":2}])
        cfg["pattern"]="ramp"
        opts = validate_options({"start_load":1,"step_size":2,"stage_seconds":3,"hold_seconds":4},self.url)
        self.assertEqual([s["seconds"] for s in stage_plan(cfg,opts)],[3,3,3,4])
        options={"stress_policy":"independent","stop_error_pct":10,"stages":[{"seconds":1,"target":5},{"seconds":1,"target":25}]}
        sent=0
        def request(_spec, _values, _profile, **kwargs):
            nonlocal sent
            sent+=1
            return dict(status=200 if sent<=5 else 503, ms=11, sent=0, received=5,
                        error="", phases={}, body=b"", headers={}, reused=False,compressed=False)
        with patch("console.benchrunner.probe",request):
            run=self.run_case(self.exercise(pattern="stress",rate=25,seconds=2,options=options))
        self.assertEqual(run.state,"Threshold reached")
        self.assertEqual(run.result["max_sustainable"]["target"],5)
        self.assertEqual(run.result["breakpoint"]["target"],25)
        self.assertEqual(run.result["breakpoint"]["error_pct"],100)
        self.assertEqual([s["count"] for s in run.result["stages"]],[5,25])
        self.assertEqual(run.result["iterations"],30)
        self.assertEqual(sum(t["count"] for t in run.series),30)
        self.assertIn("host_cpu_peak_pct", run.result["generator"])
        self.assertTrue(all("loop_lag_ms" in tick for tick in run.series))

    def test_measurements_only_and_display_snapshot_do_not_expose_request_data(self):
        raw_fixture = "keep-this-fixture-private"
        test = self.exercise(url=self.url+"/unhealthy", options={"gate_policy":"measure",
            "statuses":"2xx, 304", "csv_data":"region\n"+raw_fixture,
            "steps":[{"name":"Availability","url":self.url+"/unhealthy?region={{region}}",
                      "headers":{"X-Client":"private-header-value"}}]})
        run = self.run_case(test)
        self.assertIsNone(run.result["passed"])
        self.assertEqual(run.result["checks"],[])
        self.assertEqual(run.config["settings"]["statuses"],"200–299, 304")
        self.assertEqual(run.config["settings"]["steps"],[{"name":"Availability","method":"GET"}])
        safe = json.dumps(run.config["settings"])
        for value in (raw_fixture,"region","private-header-value",self.url):
            self.assertNotIn(value,safe)

    def test_json_bracket_extraction_matches_request_editor(self):
        from .benchrunner import extract
        item={"body":b'{"items":[{"id":"42"}]}', "headers":{}}
        self.assertEqual(extract({"id":"json:$.items[0].id","item":"json:items.0"},item),
                         {"id":"42","item":'{"id":"42"}'})

    def test_dynamic_placeholders_share_one_request_scope(self):
        import uuid
        from datetime import datetime, timezone
        from .probes import _prepare
        spec={"url":self.url+"/items/{{$uuid}}","method":"POST",
              "headers":{"X-Request":"{{ $uuid }}","X-Second":"{{$timestamp}}","X-Random":"{{$randomInt}}"},
              "query":{"ms":"{{$timestampMs}}","iso":"{{$isoTimestamp}}","again":"{{$uuid}}"},
              "auth":{"type":"bearer","token":"{{$uuid}}"},
              "body":{"request":"{{$uuid}}","number":"{{$randomInt}}","input":"{{text}}"}}
        variables={"text":'Hello "world"\n', "$uuid":"cannot-override-builtins"}
        with patch("console.probes.time.time_ns",return_value=1700000000123000000):
            url,method,headers,raw=_prepare(spec,variables,True)
        parts=urlsplit(url)
        uid=parts.path.split("/")[-1]
        self.assertEqual(uuid.UUID(uid).version,4)
        query=parse_qs(parts.query)
        self.assertEqual(headers["Authorization"],"Bearer "+uid)
        self.assertEqual((headers["X-Request"],query["again"][0]),(uid,uid))
        self.assertEqual(json.loads(raw),{"request":uid,"number":headers["X-Random"],"input":variables["text"]})
        self.assertTrue(0<=int(headers["X-Random"])<1000000)
        self.assertEqual((headers["X-Second"], query["ms"]),("1700000000",["1700000000123"]))
        self.assertEqual(query["iso"],["2023-11-14T22:13:20.123Z"])
        self.assertNotEqual(_prepare(spec,variables,True)[2]["X-Request"],uid)
        self.assertTrue(validate_options({"steps":[spec]},self.url))
        for invalid in ("{{$unknown}}","{{$uuid","{{ $uuid:4 }}"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_options({"steps":[{"query":{"v":invalid}}]},self.url)
        form_spec=validate_options({"steps":[{"method":"POST","form":{"id":"{{$uuid}}","copy":"{{$uuid}}"}}]},self.url)["steps"][0]
        fields=parse_qs(_prepare(form_spec,{},True)[3].decode())
        self.assertEqual(fields["id"],fields["copy"])
