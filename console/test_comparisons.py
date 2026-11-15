from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Exercise, Run


class ReportComparisonTests(TestCase):
    def test_cross_template_and_legacy_samples(self):
        self.client.force_login(User.objects.create_user("reader"))
        def run(name, pattern, p95):
            e = Exercise.objects.create(name=name, pattern=pattern, url="http://127.0.0.1:8000/")
            return Run.objects.create(exercise=e, state="Completed", finished=timezone.now(),
                config={"pattern": pattern}, result={"p95": p95, "errors": 0, "error_pct": 0.0},
                series=[{"second": 2, "count": 4, "p95": p95}])
        a, b, lat = run("Baseline", "fixed", 30), run("Candidate", "ramp", 45), run("Latency", "latency", 5)
        url = "/benchmarks/compare/"
        res = self.client.get(url, {"a": a.pk, "b": b.pk})
        self.assertContains(res, "+50.0%")
        self.assertContains(res, "Throughput (requests / second)")
        self.assertContains(self.client.get(url, {"a": a.pk, "b": lat.pk}), "Compare two latency")
        self.assertContains(self.client.get(url, {"a": -1, "b": b.pk}), "Choose two finished")

    def test_invalid_schedule_does_not_block_next_test(self):
        from .management.commands.pulseworker import schedule_runs
        now = timezone.now()
        for name, url in [("Revoked host", "https://outside.invalid/"), ("Orders", "http://127.0.0.1:8000/")]:
            Exercise.objects.create(name=name, url=url, next_at=now, repeat_minutes=60)
        schedule_runs(now)
        schedule_runs(now)
        self.assertEqual(list(Run.objects.order_by("pk").values_list("state", flat=True)), ["Failed", "Queued"])
