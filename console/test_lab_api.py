import csv
import gzip
import io
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .engine import enqueue
from .models import Audit, Exercise, Run, Service
from .workspace_ci import issue_token
from .workspace_roles import apply_role


@override_settings(TEST_HOSTS={"localhost:8000"}, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LabApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.viewer = User.objects.create_user("reader")
        self.editor = User.objects.create_user("engineer")
        apply_role(self.editor, "editor", True)
        self.owner = User.objects.create_superuser("owner")
        self.payload = {"name":"Checkout", "url":"http://localhost:8000/health", "seconds":2, "rate":1}
        self.client.force_login(self.editor)

    def test_adhoc_preflight_permissions_and_no_saved_template(self):
        self.client.force_login(self.viewer)
        for route in ("/api/tests/preflight", "/api/tests/run"):
            self.assertEqual(self.client.post(route, self.payload, content_type="application/json").status_code, 403)
        self.client.force_login(self.editor)
        plan = self.client.post("/api/tests/preflight", self.payload, content_type="application/json")
        self.assertEqual(plan.status_code, 200, plan.content)
        self.assertEqual(plan.json()["hosts"], ["localhost:8000"])
        self.assertEqual(plan.json()["estimated_requests"], 2)
        self.assertFalse(Run.objects.exists())
        self.assertEqual(self.client.post("/api/tests/run", self.payload, content_type="application/json").status_code, 428)
        denied = self.client.post("/api/tests/run", {**self.payload, "url":"http://not-approved.example.org/",
                                  "confirm":True}, content_type="application/json")
        self.assertEqual(denied.status_code, 400)
        self.assertFalse(Run.objects.exists())
        accepted = self.client.post("/api/tests/run", {**self.payload, "confirm":True}, content_type="application/json")
        self.assertEqual(accepted.status_code, 202)
        run = Run.objects.get()
        self.assertEqual((run.actor, run.name, run.exercise), ("engineer","Checkout",None))
        self.assertFalse(Exercise.objects.exists())

    def test_template_api_and_html_delete_preserve_frozen_names(self):
        for via_html in (False, True):
            created = self.client.post("/api/templates", self.payload, content_type="application/json")
            self.assertEqual(created.status_code, 201)
            test = Exercise.objects.get(pk=created.json()["id"])
            run = enqueue(test)
            changed = self.client.put(f"/api/templates/{test.pk}", {"name":"Renamed"}, content_type="application/json")
            self.assertEqual(changed.status_code, 200)
            self.assertEqual((changed.json()["rate"], changed.json()["seconds"]), (1,2))
            legacy = Run.objects.create(exercise=test, state="Completed", finished=timezone.now())
            if via_html:
                response = self.client.post(f"/benchmarks/{test.pk}/delete/")
                self.assertEqual(response.status_code, 302)
            else:
                self.assertEqual(self.client.delete(f"/api/templates/{test.pk}").status_code, 200)
            frozen = self.client.get(f"/api/runs/{run.pk}").json()
            self.assertEqual((frozen["name"], frozen["template_id"]), ("Checkout", None))
            legacy.refresh_from_db()
            self.assertEqual(legacy.display_name, "Renamed")
            self.assertTrue(Audit.objects.filter(object_type="exercise",object_id=str(test.pk),action="Removed saved benchmark").exists())

    def test_session_stop_handles_queued_running_and_finished_idempotently(self):
        test = Exercise.objects.create(name="Stop", url=self.payload["url"])
        queued = enqueue(test)
        url = f"/api/runs/{queued.pk}/stop"
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.editor)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        _, read = issue_token(self.viewer, "Read reports", "read")
        self.assertEqual(self.client.post(url, HTTP_AUTHORIZATION="Bearer "+read).status_code, 403)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(url).json()["state"], "Stopped")
        self.assertEqual(self.client.post(url).json()["finished"], True)
        self.assertEqual(Audit.objects.filter(action="Requested benchmark stop",object_id=str(queued.pk)).count(), 1)
        active = Run.objects.create(state="Running", name="Active")
        self.client.post(f"/runs/{active.pk}/stop/")
        active.refresh_from_db()
        self.assertIsNone(active.finished)
        self.assertTrue(active.stop)
        self.assertEqual(self.client.post(f"/api/runs/{active.pk}/stop").json()["state"], "Running")
        self.assertEqual(Audit.objects.filter(action="Requested benchmark stop",object_id=str(active.pk)).count(), 1)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post("/api/runs/999999/stop").status_code, 404)

    def test_gzip_raw_report_is_only_the_csv_measurements(self):
        run = Run.objects.create(name="Export", config={"private":"do-not-export-credential"},
            samples=[[0,9.2,200], [.8,14,503,"Orders","4G","HTTP 503",{"tcp":2},5,18,
                     {"stage":2,"iteration":3,"private":"private-raw-header"}]])
        url = f"/api/runs/{run.pk}/raw"
        self.client.logout()
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied["Content-Type"], "application/json")
        token, read = issue_token(self.viewer, "Build reports", "read")
        _, runner = issue_token(self.editor, "Build tests", "run")
        for raw in (read, runner):
            response = self.client.get(url, HTTP_AUTHORIZATION="Bearer "+raw)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/gzip")
            self.assertIn("no-store", response["Cache-Control"])
            text = gzip.decompress(response.content).decode()
            lines = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual([row["step"] for row in lines], ["Endpoint","Orders"])
            self.assertNotIn("do-not-export-credential", text)
            self.assertNotIn("private-raw-header", text)
        token.revoked = timezone.now()
        token.save()
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION="Bearer "+read).status_code, 401)
        self.assertEqual(gzip.decompress(self.client.get(url).content), self.client.get(f"/runs/{run.pk}/csv/").content)
        self.assertEqual(self.client.get("/api/runs/999999/raw").status_code, 404)

    def test_python_service_api_actions_record_scoped_metadata(self):
        body = {"name":"Shop", "framework":"FastAPI", "target":99.9}
        self.assertEqual(self.client.post("/api/python/services", body, content_type="application/json").status_code, 403)
        self.client.force_login(self.owner)
        created = self.client.post("/api/python/services", body, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        service = Service.objects.get()
        url = f"/api/python/services/{service.pk}"
        self.assertEqual(self.client.put(url+"/goal", {"target":99}, content_type="application/json").status_code, 200)
        rotated = self.client.post(url+"/rotate")
        self.assertEqual(rotated.status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 200)
        rows = list(Audit.objects.filter(actor="owner",object_type="service",object_id=str(service.pk)).order_by("pk"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1].details["target"], 99)
        log = json.dumps([(r.action,r.details) for r in rows])
        for value in (created.json()["token"], rotated.json()["token"], service.token_hash):
            self.assertNotIn(value, log)
