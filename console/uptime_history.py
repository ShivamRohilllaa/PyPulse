"""Time-bucket charts and public availability; all availability is sample weighted."""
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from django.db.models import Count, F, Q, Sum, Window
from django.db.models.functions import Ceil, RowNumber, TruncDay

from . import latency_buckets
from .models import Check, CheckHour


def utc_bucket(at, width):
    return int(at.timestamp()) // width


def history_charts(checks, raw, archived, start, now, trunc, width):
    origin, end = utc_bucket(start,width), utc_bucket(now,width)
    total, good, elapsed = Counter(), Counter(), Counter()
    histograms = defaultdict(Counter)
    old = Counter()
    for r in archived:
        key = utc_bucket(r.hour,width)
        total[key] += r.total
        good[key] += r.passed
        elapsed[key] += r.elapsed_sum_ms
        if r.latency_histogram:
            histograms[key].update(r.latency_histogram)
            old.update(r.latency_histogram)
    for r in raw:
        key = utc_bucket(r["bucket"],width)
        total[key] += r["n"]
        good[key] += r["passed"]
        elapsed[key] += r["elapsed"]
    means = {k:elapsed[k]/n for k,n in total.items() if n}
    by_bucket = [trunc("at")]
    ranks = checks.annotate(at_bucket=trunc("at"),rank=Window(RowNumber(),partition_by=by_bucket,order_by=[F("ms").asc(),F("pk").asc()]),
                            bucket_count=Window(Count("pk"),partition_by=by_bucket))
    exact = ranks.filter(rank=Ceil(F("bucket_count")*.95)).values_list("at_bucket","ms")
    tails = {utc_bucket(at,width): ms for at,ms in exact}
    if old:
        # Archived counts contain upper bounds. Combine them with matching raw samples before selecting ranks.
        for at,ms in checks.values_list("at","ms").iterator(chunk_size=2000):
            key = utc_bucket(at,width)
            bound = latency_buckets.bucket(ms)
            old[bound] += 1
            if key in histograms:
                histograms[key][bound] += 1
        tails.update({k:latency_buckets.quantile(hist,.95) for k,hist in histograms.items()})
    peak = max([0,*means.values(),*tails.values()])
    def lines(values):
        segments, points = [], []
        previous = None
        for key,y in sorted(values.items()):
            x = round(1000*(key-origin)/max(1,end-origin),2)
            y = round(95-90*y/max(peak,1),2)
            if previous != key-1:
                segments.append([])
            segments[-1].append(f"{x},{y}")
            points.append((x,y))
            previous = key
        return [" ".join(s) for s in segments],points
    mean_lines,mean_points = lines(means)
    p95_lines,p95_points = lines(tails)
    history = [cell(datetime.fromtimestamp(key*width,timezone.utc),total[key],good[key]) for key in range(origin,end+1)]
    series = [{**c,"mean":means.get(origin+i),"p95":tails.get(origin+i)} for i,c in enumerate(history)]
    return {"series":series, "peak":peak, "segments":mean_lines,"points":mean_points,"tail_segments":p95_lines,"tail_points":p95_points,
            "history":history, "merged_histogram":old}


def cell(at,n,passed):
    percent = 100*passed/n if n else None
    state = "empty" if not n else "good" if percent>=99.9 else "warn" if percent>=95 else "bad"
    return {"at":at, "count":n, "percent":percent, "state":state}


def public_days(monitors, now):
    start = now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=89)
    ids = [m.pk for m in monitors]
    totals, passed = Counter(), Counter()
    raw = Check.objects.filter(monitor_id__in=ids,at__range=(start,now)).annotate(day=TruncDay("at")).values(
        "monitor_id","day").annotate(n=Count("pk"),ok=Count("pk",filter=Q(ok=True)))
    old = CheckHour.objects.filter(monitor_id__in=ids,hour__range=(start,now)).annotate(day=TruncDay("hour")).values(
        "monitor_id","day").annotate(n=Sum("total"),ok=Sum("passed"))
    for group in (raw,old):
        for row in group:
            key = row["monitor_id"],row["day"]
            totals[key] += row["n"]
            passed[key] += row["ok"]
    dates = [start+timedelta(days=d) for d in range(90)]
    for monitor in monitors:
        monitor.days = [cell(day,totals[monitor.pk,day],passed[monitor.pk,day]) for day in dates]
        count = sum(totals[monitor.pk,day] for day in dates)
        monitor.uptime90 = 100*sum(passed[monitor.pk,day] for day in dates)/count if count else None
