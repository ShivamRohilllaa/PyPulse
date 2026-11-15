from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import Run


METRICS = (("Requests", "count"), ("Requests / second", "rps"), ("Median (ms)", "p50"),
           ("p95 (ms)", "p95"), ("p99 (ms)", "p99"), ("Error rate (%)", "error_pct"),
           ("Dropped iterations", "dropped"))


def rows(baseline, candidate):
    for label, key in METRICS:
        first, last = baseline.get(key), candidate.get(key)
        delta = None
        if first and last is not None:
            delta = f"{(last-first)/first*100:+.1f}%"
        elif first == last == 0:
            delta = "0.0%"
        yield label, first, last, delta


def trend(first, last, field):
    """Align both runs on elapsed time and retain local peaks in 150 time buckets."""
    lines = []
    end = max(1, max((p.get("second", 0) for p in [*first, *last]), default=0))
    for series in (first, last):
        buckets = {}
        for sample in series:
            value = sample.get(field)
            if value is not None:
                bucket = int(150 * sample.get("second", 0) / end)
                buckets[bucket] = max(value, buckets.get(bucket, 0))
        lines.append(sorted(buckets.items()))
    peak = max(1, max((v for line in lines for _, v in line), default=0))
    return {"lines": [" ".join(f"{x*4:.1f},{60-55*v/peak:.1f}" for x, v in line) for line in lines],
            "seconds": round(end, 1), "peak": round(peak, 2)}


@login_required
@require_GET
def compare(request):
    finished = Run.objects.filter(finished__isnull=False)
    options = finished.values("pk", "exercise__name").order_by("-pk")
    ids = [request.GET.get(key, "") for key in ("a", "b")]
    # Allow older reports to be selected from any history page by ID.
    picked = finished.select_related("exercise").in_bulk([int(i) for i in ids if i.isdecimal() and len(i) <= 18])
    first, last = [picked.get(int(i)) if i.isdecimal() and len(i) <= 18 else None for i in ids]
    context = {"section": "Benchmarks", "title": "Compare runs", "a": first, "b": last,
               "choices": options[:100], "history": Paginator(options, 50).get_page(request.GET.get("page"))}
    if all(ids):
        if first is None or last is None:
            context["problem"] = "Choose two finished reports from this workspace."
        elif first.pk == last.pk:
            context["problem"] = "Choose two different reports."
        elif (first.config.get("pattern") == "latency") != (last.config.get("pattern") == "latency"):
            context["problem"] = "Compare two latency runs or two load runs."
        else:
            groups = [{p["label"]: p for p in run.result.get("profiles", [])} for run in (first, last)]
            context.update(metrics=list(rows(first.result, last.result)),
                charts=[("Throughput (requests / second)", trend(first.series, last.series, "count")),
                        ("p95 response time (ms)", trend(first.series, last.series, "p95"))],
                profiles=[(name, list(rows(groups[0][name], groups[1][name]))) for name in groups[0] if name in groups[1]])
    return render(request, "console/compare.html", context)
