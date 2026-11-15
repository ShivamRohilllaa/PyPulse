"""Compact expired raw checks into UTC-hour summaries before pruning history."""
from collections import Counter, defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncHour
from django.utils import timezone

from . import latency_buckets
from .models import Batch, Check, CheckHour, Run
from .workspace_settings import retention


def trim(now=None):
    now = now or timezone.now()
    policy = retention()
    cutoff = (now - timedelta(days=policy["raw_days"])).replace(minute=0, second=0, microsecond=0)
    # Bound each write transaction so the collector can continue ingesting traffic.
    while True:
        with transaction.atomic():
            ids = list(Check.objects.filter(at__lt=cutoff).order_by("at").values_list("pk", flat=True)[:500])
            if not ids:
                break
            raw = Check.objects.filter(pk__in=ids)
            buckets = raw.annotate(hour=TruncHour("at")).values("monitor_id", "hour").annotate(
                total=Count("pk"), passed=Count("pk", filter=Q(ok=True)), slow_count=Count("pk", filter=Q(slow=True)), elapsed_sum_ms=Sum("ms"),
                min_ms=Min("ms"), max_ms=Max("ms"))
            histograms = defaultdict(Counter)
            for monitor_id, at, ms in raw.values_list("monitor_id", "at", "ms"):
                hour = at.replace(minute=0, second=0, microsecond=0)
                histograms[(monitor_id, hour)][latency_buckets.bucket(ms)] += 1
            for row in buckets:
                histogram = histograms[(row["monitor_id"], row["hour"])]
                bucket, created = CheckHour.objects.get_or_create(
                    monitor_id=row["monitor_id"], hour=row["hour"],
                    defaults={**{key: row[key] for key in ("total", "passed", "slow_count", "elapsed_sum_ms", "min_ms", "max_ms")},
                              "latency_histogram": dict(histogram)})
                if not created:
                    bucket.latency_histogram = latency_buckets.merged(bucket.latency_histogram, histogram)
                    bucket.total += row["total"]
                    bucket.passed += row["passed"]
                    bucket.slow_count += row["slow_count"]
                    bucket.elapsed_sum_ms += row["elapsed_sum_ms"]
                    bucket.min_ms = min(bucket.min_ms, row["min_ms"])
                    bucket.max_ms = max(bucket.max_ms, row["max_ms"])
                    bucket.save()
            raw.delete()

    CheckHour.objects.filter(hour__lt=now - timedelta(days=policy["rollup_days"])).delete()
    Batch.objects.filter(at__lt=now - timedelta(days=policy["telemetry_days"])).delete()
    Run.objects.filter(finished__lt=now - timedelta(days=policy["run_days"])).delete()
