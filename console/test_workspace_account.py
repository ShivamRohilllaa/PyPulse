import io
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import ApiToken, Exercise, LoginThrottle, NetworkProfile, Run, Variable
from .workspace_ci import issue_token
from .workspace_config import export_config
from .workspace_roles import apply_role

User = get_user_model()
PASSWORD = "a-strong-test-password"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"], ALLOWED_HOSTS=["localhost","dashboard.example","testserver"])
class WorkspaceAuthenticationTests(TestCase):
    def setUp(self):
        self.payload = {"email":"owner@example.org", "password": PASSWORD, "confirm": PASSWORD}
        self.local = {"HTTP_HOST":"localhost", "REMOTE_ADDR":"127.0.0.1"}

    def test_first_run_requires_verified_location_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        blocked = client.get("/login/", HTTP_HOST="dashboard.example", REMOTE_ADDR="192.0.2.40")
        self.assertContains(blocked, "createsuperuser")
        self.assertEqual(client.get("/login/", **self.local).url, "/setup/")
        form = client.get("/setup/", **self.local)
        self.assertContains(form, "Create administrator")
        self.assertEqual(client.post("/setup/", self.payload, **self.local).status_code, 403)
        self.assertEqual(client.post("/setup/", {**self.payload, "csrfmiddlewaretoken":client.cookies["csrftoken"].value},
                                     **self.local).status_code, 302)
        self.assertTrue(User.objects.get().is_superuser)
        self.assertEqual(self.client.post("/api/auth/setup", self.payload, content_type="application/json", **self.local).status_code, 409)

    @patch.dict(os.environ, {"PYPULSE_SETUP_SECRET": "server-issued-secret"})
    def test_remote_setup_requires_tls_and_proof_even_behind_loopback_proxy(self):
        remote = {"HTTP_HOST":"dashboard.example", "REMOTE_ADDR":"127.0.0.1", "HTTP_X_FORWARDED_FOR":"198.51.100.8"}
        post = lambda data, secure: self.client.post("/api/auth/setup", data, content_type="application/json", secure=secure, **remote)
        self.assertEqual(post({**self.payload, "setup_key":"server-issued-secret"}, False).status_code, 403)
        self.assertEqual(post(self.payload, True).status_code, 400)
        self.assertFalse(User.objects.exists())
        self.assertEqual(post({**self.payload, "setup_key":"server-issued-secret"}, True).status_code, 201)

    def test_shared_failed_login_limit_expires(self):
        User.objects.create_superuser("owner", password=PASSWORD)
        for _ in range(10):
            response = self.client.post("/api/auth/login", {"username":"owner","password":"bad"}, content_type="application/json")
            self.assertEqual(response.status_code, 401)
        for url in ("/login/", "/admin/login/"):
            self.assertEqual(self.client.post(url, {"username":"owner", "password":PASSWORD}).status_code, 429)
        LoginThrottle.objects.update(since=timezone.now()-timedelta(minutes=16))
        self.assertEqual(self.client.post("/login/", {"username":"owner", "password":PASSWORD}).status_code, 302)
        self.assertFalse(LoginThrottle.objects.exists())

    def test_session_api_csrf_and_own_password_change(self):
        member = User.objects.create_user("dev", password=PASSWORD)
        client = Client(enforce_csrf_checks=True)
        csrf = client.get("/api/auth/status").json()["csrfToken"]
        creds = {"username":"dev","password":PASSWORD}
        self.assertEqual(client.post("/api/auth/login", creds, content_type="application/json").status_code, 403)
        login = client.post("/api/auth/login", creds, content_type="application/json", HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(client.get("/api/auth/me").json()["user"]["id"], member.pk)
        change = {"currentPassword":PASSWORD,"newPassword":"new-Test-password-12"}
        changed = client.post("/api/auth/password", change, content_type="application/json", HTTP_X_CSRFTOKEN=login.json()["csrfToken"])
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        member.refresh_from_db()
        self.assertTrue(member.check_password(change["newPassword"]))
        self.assertEqual(client.post("/api/auth/logout", HTTP_X_CSRFTOKEN=changed.json()["csrfToken"]).status_code, 200)
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_bootstrap_is_idempotent_and_preserves_existing_password(self):
        with tempfile.TemporaryDirectory() as location:
            path = Path(location) / "workspace.json"
            doc = export_config(["variables"])
            doc["sections"]["variables"] = [{"name":"APP", "secret":False, "value":"demo"}]
            path.write_text(json.dumps(doc))
            env = {"PYPULSE_ADMIN_EMAIL":"deployer@example.com","PYPULSE_ADMIN_PASSWORD":PASSWORD,"PYPULSE_CONFIG_FILE":str(path)}
            with patch.dict(os.environ, env):
                call_command("bootstrap_workspace", stdout=io.StringIO())
            with patch.dict(os.environ, {**env, "PYPULSE_ADMIN_PASSWORD":"replacement-password"}):
                call_command("bootstrap_workspace", stdout=io.StringIO())
            self.assertEqual(User.objects.count(), 1)
            self.assertTrue(User.objects.get().check_password(PASSWORD))
            self.assertEqual(Variable.objects.get().reveal(), "demo")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkspaceJsonTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner")
        self.editor = User.objects.create_user("editor")
        apply_role(self.editor, "editor", True)
        self.viewer = User.objects.create_user("viewer")
        self.client.force_login(self.owner)

    def send(self, method, path, data):
        return getattr(self.client, method)(path, data, content_type="application/json")

    def test_member_and_token_lifecycle_with_immediate_session_invalidation(self):
        created = self.send("post", "/api/users", {"email":"dev@example.org", "name":"Dev", "role":"editor", "password":PASSWORD})
        self.assertEqual(created.status_code, 201)
        pk = created.json()["id"]
        secondary = Client()
        secondary.force_login(User.objects.get(pk=pk))
        token = secondary.post("/api/tokens", {"name":"nightly"}, content_type="application/json")
        self.assertEqual(token.status_code, 201)
        self.assertNotContains(secondary.get("/api/tokens"), token.json()["token"])
        self.assertEqual(self.send("put", f"/api/users/{pk}", {"disabled":True}).status_code, 200)
        self.assertEqual(self.send("put", f"/api/users/{pk}", {"disabled":False,"role":"viewer"}).status_code, 200)
        self.assertEqual(secondary.get("/api/auth/me").status_code, 401)
        secondary.force_login(User.objects.get(pk=pk))
        self.assertEqual(secondary.post("/api/tokens", {"name":"x","scope":"run"}, content_type="application/json").status_code, 403)
        self.assertEqual(secondary.delete(f"/api/tokens/{token.json()['id']}").status_code, 200)
        self.assertIsNotNone(ApiToken.objects.get().revoked)
        self.assertEqual(self.client.delete(f"/api/users/{self.owner.pk}").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/users/{pk}").status_code, 200)
        self.assertEqual(ApiToken.objects.count(), 0)

    def test_settings_and_masked_config_permissions(self):
        result = self.send("put", "/api/settings", {"defaults":{"failureThreshold":4},"statusPage":{"title":"Checkout"}})
        self.assertEqual(result.json()["defaults"]["failureThreshold"], 4)
        system = self.client.get("/api/system").json()
        self.assertIn("memoryMb", system)
        self.assertIn("dataDir", system)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get("/api/config/export?format=yaml").status_code, 200)
        self.assertEqual(self.client.get("/api/config/export?secrets=true").status_code, 403)
        self.assertEqual(self.send("post", "/api/config/import", {}).status_code, 403)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/api/settings").json()["statusPage"]["title"], "Checkout")
        self.assertEqual(self.send("put", "/api/settings", {}).status_code, 403)
        self.assertEqual(self.client.get("/api/config/export").status_code, 403)
        self.assertNotIn("dataDir", self.client.get("/api/system").json())

    def test_resources_mask_secrets_and_allow_editor_to_duplicate_profile(self):
        self.client.force_login(self.editor)
        result = self.send("post", "/api/variables", {"name":"AUTH","description":"QA credential", "value":"never-expose-this-test-credential"})
        self.assertEqual(result.status_code, 201)
        var = result.json()
        self.assertNotEqual(var["value"], "never-expose-this-test-credential")
        updated = self.send("put", f"/api/variables/{var['id']}", {"description":"Sample", "value":var["value"]})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(Variable.objects.get().reveal(), "never-expose-this-test-credential")
        self.assertEqual(self.send("put", f"/api/variables/{var['id']}", {"isSecret":False}).status_code, 400)
        original = NetworkProfile.objects.filter(preset=True).first()
        page = self.client.get(f"/networks/{original.pk}/duplicate/")
        self.assertContains(page, original.name + " (custom)")
        custom = self.send("post", "/api/profiles", {"name":"Test", "description":"Local testing", "latencyMs":100})
        self.assertEqual(custom.status_code, 201)
        self.assertEqual(self.client.delete(f"/api/profiles/{original.pk}").status_code, 403)
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get("/api/variables"), "never-expose-this-test-credential")
        self.assertNotContains(self.client.get("/variables/"), "never-expose-this-test-credential")
        self.assertEqual(self.client.delete(f"/api/profiles/{custom.json()['id']}").status_code, 403)

    def test_saved_ci_route_separates_bearer_and_csrf_checked_session(self):
        ex = Exercise.objects.create(name="Local", url="http://localhost:8000/")
        _, raw = issue_token(self.editor, "Deployment", "run")
        path = f"/api/templates/{ex.pk}/run?wait=false"
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(path).status_code, 403)
        self.assertEqual(client.post(path, HTTP_AUTHORIZATION="Bearer "+raw).status_code, 202)
        run = Run.objects.get()
        self.assertEqual((run.trigger, run.actor), ("ci","editor"))
        run.result, run.finished, run.state = {"passed":None}, timezone.now(), "Completed"
        run.save()
        polled = client.get(f"/api/ci/runs/{run.pk}", HTTP_AUTHORIZATION="Bearer "+raw)
        self.assertTrue(polled.json()["finished"])
        self.assertIsNone(polled.json()["passed"])
        csrf = client.get("/api/auth/status").json()["csrfToken"]
        self.assertEqual(client.post(path, HTTP_X_CSRFTOKEN=csrf).status_code, 202)
