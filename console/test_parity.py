from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import AsyncClient, TestCase
from django.utils import timezone

from .audit import record
from .models import Exercise, Run
from .report_summary import explain, text_report


class DashboardEntryTests(TestCase):
    def test_signed_in_root_and_login_return_to_traffic(self):
        user = User.objects.create_user("viewer")
        self.assertRedirects(self.client.get("/"), "/login/?next=/", fetch_redirect_response=False)
        self.client.force_login(user)
        page = self.client.get("/")
        self.assertContains(page, "Request traffic")
        self.assertNotContains(page, "Your API, under load.")
        self.assertEqual(self.client.get("/docs/index.html").status_code, 404)

    def test_audit_api_privileges_and_search(self):
        viewer = User.objects.create_user("viewer")
        admin = User.objects.create_superuser("admin")
        record(admin, "Opened workspace", details={"item": "Report"})
        self.assertEqual(self.client.get("/api/audit").status_code, 401)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get("/api/audit").status_code, 403)
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/api/audit", {"q": "Report"}).json()[0]["action"], "Opened workspace")
        self.assertEqual(self.client.get("/api/audit", {"q": "Absent"}).json(), [])

    async def test_live_events_require_session_and_stream_only_revisions(self):
        browser = AsyncClient()
        denied = await browser.get("/api/events")
        self.assertEqual(denied.status_code, 401)
        viewer = await User.objects.acreate(username="signed-in")
        await browser.aforce_login(viewer)
        with patch("console.live_events.database_revision", return_value="snapshot-1"):
            response = await browser.get("/api/stream")
            self.assertEqual(response["Content-Type"], "text/event-stream")
            self.assertEqual(response["X-Accel-Buffering"], "no")
            frames = response.streaming_content
            self.assertIn(b"retry:", await anext(frames))
            self.assertIn(b"event: change\ndata: snapshot-1", await anext(frames))
            await frames.aclose()
            response.close()

    def test_wsgi_dashboard_falls_back_without_open_stream(self):
        self.client.force_login(User.objects.create_user("reader"))
        self.assertEqual(self.client.get("/api/events").status_code, 204)
        self.assertNotContains(self.client.get("/"), "<body data-events>")


class ReportWorkflowTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("maintainer"))
        self.exercise = Exercise.objects.create(name="Order health", url="http://127.0.0.1:8000/orders/42")

    def test_observations_and_text_export_with_historical_rows(self):
        run = Run.objects.create(exercise=self.exercise, state="Completed", finished=timezone.now(),
            result={"count":2,"errors":1,"p50":8,"p95":245,"p99":245,"passed":False,
                    "codes":{"200":1,"503":1},"failures":{"HTTP 503":1},"phases":{"ttfb":210}},
            config={"plan":"sealed-private-value","pattern":"fixed"}, samples=[[0,8,200],[1,245,503]])
        response = self.client.get(f"/runs/{run.pk}", {"samples":"failed"})
        self.assertContains(response, "Recommended checks")
        self.assertEqual(response.context["timeline"].object_list, [[1,245,503]])
        slow = self.client.get(f"/runs/{run.pk}", {"samples":"slowest"})
        self.assertEqual(slow.context["timeline"].object_list[0][2], 503)
        report = self.client.get(f"/runs/{run.pk}/summary.txt").content.decode()
        self.assertIn("release gate failed", report)
        self.assertIn("application and reverse proxy logs", report)
        self.assertIn("first byte", report)
        self.assertNotIn("sealed-private-value", report)

    def test_accepted_http_error_does_not_receive_an_unrelated_recommendation(self):
        run = Run(state="Completed", finished=timezone.now(), result={"count":2,"errors":1,"passed":None,
                  "failures":{"Timeout":1},"codes":{"429":1,"network":1}})
        report = text_report(run, explain(run))
        self.assertNotIn("rate limit", report)
        self.assertNotIn("release gate", report)
        run.finished = None
        self.assertIsNone(explain(run))

    def test_copy_does_not_schedule_or_overwrite_the_original(self):
        soon = timezone.now() + timedelta(minutes=10)
        self.exercise.next_at = soon
        self.exercise.repeat_minutes = 10
        self.exercise.save()
        response = self.client.post(f"/benchmarks/{self.exercise.pk}/edit/", {
            "action":"copy","name":self.exercise.name,"url":self.exercise.url,"rate":1,"seconds":5,
            "max_p95":0,"max_error_pct":0,"pattern":"fixed","repeat_minutes":10})
        self.assertEqual(response.status_code, 302)
        clone = Exercise.objects.exclude(pk=self.exercise.pk).get()
        self.assertIsNone(clone.next_at)
        self.assertEqual(clone.repeat_minutes, 0)
        self.assertEqual(clone.name, "Order health (copy)")
        self.exercise.refresh_from_db()
        self.assertEqual(self.exercise.next_at, soon)
