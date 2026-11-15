import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .models import Check, CheckHour, Monitor, NetworkProfile, Variable, WorkspaceSettings
from .probes import host_allowed, probe, workspace_variables
from .retention import trim


class ProfileAndVariableTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("owner"))

    def test_presets_and_credentials(self):
        self.assertEqual(NetworkProfile.objects.filter(preset=True).count(), 7)
        preset = NetworkProfile.objects.filter(preset=True).first()
        self.assertEqual(self.client.post(f"/networks/{preset.pk}/delete/").status_code, 403)
        self.assertEqual(self.client.post("/variables/new/", {"name": "TOKEN", "secret": "on", "value": "global123"}).status_code, 302)
        self.client.post("/variables/new/", {"name": "TOKEN", "environment": "test", "secret": "on", "value": "test456"})
        self.assertEqual(workspace_variables("test")["TOKEN"], "test456")
        self.assertEqual(workspace_variables()["TOKEN"], "global123")
        row = Variable.objects.get(environment="")
        self.assertNotIn("global123", row.ciphertext)
        self.assertNotContains(self.client.get("/variables/"), "global123")
        url = f"/variables/{row.pk}/edit/"
        self.assertNotContains(self.client.get(url), "global123")
        self.assertContains(self.client.post(url, {"name": "TOKEN", "value": ""}), "Enter a new value")
        row.refresh_from_db()
        self.assertTrue(row.secret)
        self.client.force_login(User.objects.create_user("reader"))
        self.assertEqual(self.client.post(url, {"name": "TOKEN", "value": "new"}).status_code, 403)

    def test_retention_compacts_once_and_preserves_recent_checks(self):
        monitor = Monitor.objects.create(name="Demo", url="http://localhost/")
        now = timezone.now().replace(minute=10, second=0, microsecond=0)
        WorkspaceSettings.objects.create(pk=1, raw_days=1)
        for days, ms, ok in [(2, 9, True), (2, 11, False), (0, 2, True)]:
            Check.objects.create(monitor=monitor, at=now-timedelta(days=days), ms=ms, ok=ok)
        trim(now)
        trim(now)
        summary = CheckHour.objects.get()
        self.assertEqual((summary.total, summary.passed, summary.min_ms, summary.max_ms, summary.elapsed_sum_ms),
                         (2, 1, 9, 11, 20))
        self.assertEqual(Check.objects.get().ms, 2)


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        data = json.dumps([self.path, self.headers.get("Authorization"), body]).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.end_headers()


class MeasureLocalHTTP(SimpleTestCase):
    def test_probe_http_and_profile(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        authority = f"127.0.0.1:{server.server_port}"
        url = f"http://{authority}/path?signed=a%20b"
        with override_settings(TEST_HOSTS={authority}):
            result = probe({"url": url, "method": "POST", "body": {"id": "{{ID}}"}, "query": {"tag": "two words"},
                            "auth": {"type": "basic", "username": "demo", "password": "{{PW}}"}},
                           {"ID": "42", "PW": "test"}, allowed=True)
            self.assertEqual(result["status"], 201, result["error"])
            path, auth, body = json.loads(result["body"])
            self.assertEqual((path, auth, json.loads(body)), ("/path?signed=a%20b&tag=two+words", "Basic ZGVtbzp0ZXN0", {"id": "42"}))
            self.assertGreater(result["received"], 0)
            self.assertGreater(result["sent"], 0)
            self.assertIn("ttfb", result["phases"])
            self.assertEqual(probe({"url": url})["status"], 302)
            self.assertEqual(probe({"url": url}, profile={"loss_pct": 100})["error"], "Simulated connection loss")
        self.assertFalse(host_allowed("https://demo.example.com:8001/", ["*.example.com:8000"]))
