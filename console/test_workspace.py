import io
import json
import sqlite3
import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AlertChannel, Exercise, Maintenance, Monitor, NetworkProfile, Run, Variable, WorkspaceSettings
from .probes import approved_url, seal
from .workspace_ci import issue_token
from .workspace_config import ConfigError, encode, export_config, import_config, parse_file
from .workspace_settings import normalize_hosts, retention


class TransferTests(TestCase):
    def setUp(self):
        self.profile = NetworkProfile.objects.create(name="Mobile", latency_ms=100)
        self.secret = Variable(name="API_KEY", environment="qa", secret=True)
        self.secret.set_value("SECRET_VALUE")
        self.secret.save()
        self.channel = AlertChannel.objects.create(name="Ops", kind="webhook", config_secret=seal(json.dumps({
            "url": "https://queue.example.com/hook", "secret": "WEBHOOK_SECRET"})))
        self.monitor = Monitor.objects.create(name="Readiness", url="https://api.example.com/ready", group="qa",
            alert_events=["Opened"], network_profile=self.profile,
            request_secret=seal(json.dumps({"headers":{"Authorization":"SECRET_HEADER"}, "statuses":[200,204],
                                            "json_equals":{"ready": True}})))
        self.monitor.channels.add(self.channel)
        start = timezone.now()
        Maintenance.objects.create(monitor=self.monitor, start=start, end=start+timedelta(hours=1), recurrence="weekly")
        self.test = Exercise.objects.create(name="Smoke", url="http://localhost:8000/health",
            rate=1, seconds=2, options={"environment":"qa","profiles":[self.profile.pk],
                                       "steps":[{"name":"Read", "headers":{"X-Custom":"RAW_VALUE"}}],
                                       "csv_data":"item\nFIXTURE\n"})

    def test_masked_formats_restore_in_both_modes(self):
        for kind, mode in (("json", "merge"), ("yaml", "replace")):
            doc = export_config()
            text = encode(doc, kind)
            for marker in ("SECRET_VALUE", "WEBHOOK_SECRET", "RAW_VALUE", "FIXTURE", "SECRET_HEADER"):
                self.assertNotIn(marker, text)
            self.assertEqual(doc["sections"]["benchmarks"][0]["options"]["profiles"], ["Mobile"])
            self.assertEqual(doc["sections"]["monitors"][0]["channels"], ["Ops"])
            self.assertEqual(doc["sections"]["monitors"][0]["request"]["statuses"], [200, 204])
            restored = import_config(parse_file(text.encode()), mode)
            self.assertEqual(restored["monitors"], 1)
            clear = export_config(secrets=True)["sections"]
            self.assertEqual(clear["variables"][0]["value"], "SECRET_VALUE")
            self.assertEqual(clear["channels"][0]["config"]["secret"], "WEBHOOK_SECRET")
            self.assertEqual(clear["monitors"][0]["request"]["headers"]["Authorization"], "SECRET_HEADER")
            self.assertEqual(clear["benchmarks"][0]["options"]["steps"][0]["headers"]["X-Custom"], "RAW_VALUE")

    def test_clear_transfer_into_new_workspace(self):
        doc = export_config(secrets=True)
        Monitor.objects.all().delete()
        Exercise.objects.all().delete()
        NetworkProfile.objects.filter(preset=False).delete()
        AlertChannel.objects.all().delete()
        Variable.objects.all().delete()
        import_config(doc)
        monitor = Monitor.objects.get(name="Readiness")
        self.assertEqual(monitor.channels.get().name, "Ops")
        self.assertEqual(monitor.network_profile.name, "Mobile")
        self.assertEqual(Exercise.objects.get(name="Smoke").options["profiles"], [monitor.network_profile.pk])
        self.assertEqual(Maintenance.objects.get().recurrence, "weekly")

    def test_maintenance_scopes_restore_without_workspace_data(self):
        legacy = Maintenance.objects.get()
        second = Monitor.objects.create(name="Second", url="https://api.example.com/health")
        common = dict(start=legacy.start, end=legacy.end)
        Maintenance.objects.create(name="Holiday", all_monitors=True, **common)
        window = Maintenance.objects.create(name="Service update", recurrence="daily", **common)
        window.targets.add(self.monitor, second)
        Maintenance.objects.create(name="Retired group", **common)
        doc = export_config(["maintenance"])
        Maintenance.objects.all().delete()
        # Legacy files carry only monitor, start/end and recurrence.
        doc["sections"]["maintenance"][0].pop("name")
        for _ in range(2):
            import_config(parse_file(encode(doc, "yaml").encode()))
        self.assertEqual(Maintenance.objects.count(), 4)
        self.assertEqual(Maintenance.objects.get(monitor=self.monitor).recurrence, "weekly")
        self.assertTrue(Maintenance.objects.get(name="Holiday").all_monitors)
        self.assertSetEqual(set(Maintenance.objects.get(name="Service update").targets.all()), {self.monitor, second})
        inert = Maintenance.objects.get(name="Retired group")
        self.assertFalse(inert.all_monitors)
        self.assertFalse(inert.targets.exists())

    def test_maintenance_missing_or_conflicting_targets_roll_back(self):
        single = export_config(["maintenance"])["sections"]["maintenance"][0]
        section = {"maintenance": [dict(single, name="New window"), dict(single, all_monitors=True)]}
        doc = {"schema": "pypulse.workspace/v1", "sections": section}
        with self.assertRaises(ConfigError):
            import_config(doc)
        self.assertEqual(Maintenance.objects.count(), 1)
        section["maintenance"][1] = dict(single, monitor=None, targets=["Missing monitor"])
        with self.assertRaises(ConfigError):
            import_config(doc)
        self.assertEqual(Maintenance.objects.count(), 1)

    def test_replace_preserves_shared_window_references(self):
        Maintenance.objects.all().delete()
        window = Maintenance.objects.create(start=timezone.now(), end=timezone.now()+timedelta(days=1))
        window.targets.add(self.monitor)
        doc = export_config(["monitors", "maintenance"])
        doc["sections"]["monitors"] = []
        for scopes in (["monitors"], ["monitors", "maintenance"]):
            with self.assertRaises(ConfigError):
                import_config(doc, "replace", scopes)
        self.assertEqual(window.targets.get(), self.monitor)

    def test_daily_schedule_with_template_url_survives_repeated_import(self):
        base = Variable(name="BASE_URL", environment="qa", secret=False)
        base.set_value("http://localhost:8000")
        base.save()
        doc = export_config(["benchmarks"], secrets=True)
        test = doc["sections"]["benchmarks"][0]
        test["url"] = "{{BASE_URL}}/health"
        test["options"]["daily_at"] = "16:30"
        import_config(doc)
        self.test.refresh_from_db()
        first = self.test.next_at
        self.assertEqual(first.strftime("%H:%M"), "16:30")
        import_config(doc)
        self.test.refresh_from_db()
        self.assertEqual(self.test.next_at, first)

    def test_invalid_file_rolls_back_entire_import(self):
        doc = export_config(secrets=True)
        doc["sections"]["variables"][0]["value"] = "new"
        doc["sections"]["benchmarks"][0]["rate"] = 900
        with self.assertRaises(ConfigError):
            import_config(doc)
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.reveal(), "SECRET_VALUE")
        self.assertFalse(WorkspaceSettings.objects.exists())

    def test_replace_is_scoped_and_protects_history(self):
        Run.objects.create(exercise=self.test)
        doc = export_config()
        doc["sections"]["variables"] = []
        doc["sections"]["benchmarks"] = []
        with self.assertRaises(ConfigError):
            import_config(doc, "replace", ["benchmarks","variables"])
        self.assertTrue(Variable.objects.exists())
        import_config(doc, "replace", ["variables"])
        self.assertFalse(Variable.objects.exists())
        self.assertTrue(Monitor.objects.exists())

    def test_unmatched_secret_or_reference_rolls_back(self):
        doc = export_config(["variables"])
        doc["sections"]["variables"][0]["name"] = "NEW"
        with self.assertRaises(ConfigError):
            import_config(doc)
        self.assertFalse(Variable.objects.filter(name="NEW").exists())
        doc = export_config(["monitors"])
        doc["sections"]["monitors"][0]["channels"] = ["missing"]
        with self.assertRaises(ConfigError):
            import_config(doc)

    def test_masked_file_cannot_expose_a_secret_variable(self):
        doc = export_config(["variables"])
        doc["sections"]["variables"][0]["secret"] = False
        with self.assertRaises(ConfigError):
            import_config(doc)
        self.assertTrue(Variable.objects.get().secret)

    def test_parse_rejects_yaml_aliases_duplicate_keys_and_oversized_files(self):
        for payload in (b"foo: 1\nfoo: 2", b"foo: &a [1]\nbar: *a", b"x" * 1048577):
            with self.assertRaises(ConfigError):
                parse_file(payload)

    def test_host_patterns_and_port_validation(self):
        WorkspaceSettings.objects.create(pk=1, allowed_hosts=normalize_hosts("*.STAGING.example:443\napi.example:8080"))
        approved_url("https://api.staging.example/health", allowed=True)
        with self.assertRaises(ValidationError):
            approved_url("https://hostile-staging.example/health", allowed=True)
        with self.assertRaises(ValidationError):
            approved_url("https://api.staging.example:8080/", allowed=True)
        for raw in ("*.com:443", "http://api.example", "foo:65536", "foo#fragment", "admin@host"):
            with self.assertRaises(ValidationError):
                normalize_hosts(raw)
        self.assertEqual(retention()["raw_days"], 30)


class WorkspaceViewsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("owner")
        self.reader = get_user_model().objects.create_user("reader")
        self.exercise = Exercise.objects.create(name="Allowed", url="http://localhost:8000/health", seconds=1)

    def test_workspace_pages_and_roles(self):
        self.client.force_login(self.admin)
        for page in ("", "settings/", "members/", "tokens/", "config/"):
            self.assertEqual(self.client.get("/workspace/" + page).status_code, 200)
        self.assertEqual(self.client.post(f"/workspace/members/{self.admin.pk}/", {"role":"viewer"}).status_code, 400)
        self.client.post(f"/workspace/members/{self.reader.pk}/", {"role":"editor", "active":"on"})
        reader = get_user_model().objects.get(pk=self.reader.pk)
        self.assertTrue(reader.has_perm("console.add_run"))
        self.assertFalse(reader.has_perm("console.add_service"))
        self.assertFalse(reader.has_perm("console.change_variable"))
        self.client.force_login(reader)
        for page in ("settings/", "members/", "config/"):
            self.assertEqual(self.client.get("/workspace/" + page).status_code, 403)

    def test_admin_can_reach_password_reset_and_last_login(self):
        self.client.force_login(self.admin)
        reset = reverse("admin:auth_user_password_change", args=[self.reader.pk])
        members = self.client.get("/workspace/members/")
        self.assertContains(members, reset)
        self.assertContains(members, "Last sign-in")
        self.assertContains(members, "Hasn’t signed in")
        self.assertContains(self.client.get(reset), 'name="password1"')
        self.assertContains(self.client.get("/workspace/"), reverse("admin:index"))
        self.client.force_login(self.reader)
        self.assertNotContains(self.client.get("/workspace/"), "Django administration")
        self.assertEqual(self.client.get(reset).status_code, 302)

    def test_config_upload_download_and_confirmation(self):
        self.client.force_login(self.admin)
        downloaded = self.client.post("/workspace/config/export/", {"sections":["profiles","settings"], "format":"yaml"})
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("no-store", downloaded["Cache-Control"])
        args = {"mode":"replace", "sections":["settings"]}
        def file():
            value = io.BytesIO(downloaded.content)
            value.name = "workspace.yaml"
            return value
        self.assertEqual(self.client.post("/workspace/config/import/", {**args, "config":file()}).status_code, 400)
        self.assertEqual(self.client.post("/workspace/config/import/", {**args, "config":file(), "confirm":"on"}).status_code, 302)

    def test_token_scope_ownership_revocation_and_gate(self):
        token, raw = issue_token(self.admin, "Deploy", "run")
        run_url = f"/api/ci/tests/{self.exercise.pk}/run"
        headers = {"HTTP_AUTHORIZATION":"Bearer " + raw}
        self.assertNotEqual(token.digest, raw)
        self.assertEqual(self.client.post(run_url + "?wait=22", **headers).status_code, 400)
        queued = self.client.post(run_url, **headers)
        self.assertEqual(queued.status_code, 202)
        self.assertIsNone(queued.json()["passed"])
        run = Run.objects.get(pk=queued.json()["id"])
        run.state, run.finished, run.result = "Completed", timezone.now(), {"passed":False}
        run.save()
        self.assertIs(self.client.get(queued["Location"], **headers).json()["passed"], False)
        self.client.force_login(self.reader)
        self.assertEqual(self.client.post(f"/workspace/tokens/{token.pk}/revoke/").status_code, 404)
        self.assertEqual(self.client.post("/workspace/tokens/", {"name":"Runner","scope":"run"}).status_code, 403)
        _, key = issue_token(self.reader, "Reader", "read")
        self.assertEqual(self.client.post(run_url, HTTP_AUTHORIZATION="Bearer "+key).status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(f"/workspace/tokens/{token.pk}/revoke/")
        self.assertEqual(self.client.get(queued["Location"], **headers).status_code, 401)
        self.reader.is_active = False
        self.reader.save()
        self.assertEqual(self.client.get(queued["Location"], HTTP_AUTHORIZATION="Bearer "+key).status_code, 401)

    def test_completed_wait_returns_criteria_once(self):
        _, raw = issue_token(self.admin, "Gate", "run")
        def finish(_):
            Run.objects.update(finished=timezone.now(), state="Completed", result={"passed":True})
        with patch("console.workspace_ci.time.sleep", side_effect=finish):
            reply = self.client.post(f"/api/ci/tests/{self.exercise.pk}/run?wait=1", HTTP_AUTHORIZATION="Bearer "+raw)
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json()["passed"])
        self.assertEqual(Run.objects.count(), 1)


class LegacyRoleTests(TestCase):
    def test_initial_import_preserves_roles_and_repeat_does_not_change_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prior.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE users (email TEXT, name TEXT, disabled INTEGER, role TEXT, password_hash TEXT);
                    INSERT INTO users VALUES ('old-editor', '', 0, 'editor', 'hash');
                    INSERT INTO users VALUES ('old-viewer', '', 0, 'viewer', 'hash');
                    CREATE TABLE python_services (id INTEGER);
                    CREATE TABLE python_minutes (id INTEGER);
                    CREATE TABLE test_runs (id INTEGER, status TEXT, kind TEXT);
                """)
            call_command("import_workspace", path, stdout=io.StringIO())
            User = get_user_model()
            editor = User.objects.get(username="old-editor")
            self.assertTrue(editor.has_perm("console.add_run"))
            self.assertFalse(editor.has_perm("console.change_service"))
            self.assertFalse(User.objects.get(username="old-viewer").has_perm("console.add_run"))
            editor.groups.clear()
            call_command("import_workspace", path, stdout=io.StringIO())
            self.assertFalse(User.objects.get(pk=editor.pk).has_perm("console.add_run"))
            self.assertEqual(User.objects.count(), 2)
