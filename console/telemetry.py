import hashlib
import json
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone as tz
from django.db import transaction
from django.db.models import IntegerField, Sum
from django.db.models.functions import TruncHour, TruncMinute
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import Batch, Service, Slice

EDGES = (5, 25, 100, 250, 500, 1000, 2500, 10000)
WINDOWS = (1, 24, 168)

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def issue_token():
    return "py_" + secrets.token_urlsafe(32)

def percentile(histogram, fraction):
    rank = math.ceil(sum(histogram) * fraction)
    cumulative = 0
    for edge, count in zip(EDGES, histogram):
        cumulative += count
        if cumulative >= rank:
            return edge
    return None

def parse_row(row):
    fields = ("count","clientErrors","serverErrors","sqlSamples","sqlQueries")
    counts = [row[k] for k in fields]
    bins = row["buckets"]
    if not isinstance(bins, list) or len(bins) != len(EDGES)+1:
        raise ValueError("Invalid histogram")
    if any(type(n) != int or not 0 <= n <= 2**31-1 for n in counts + bins):
        raise ValueError("Invalid counters")
    count, client, server, samples, queries = counts
    if not count or sum(bins) != count or client+server > count or samples > count or (not samples and queries):
        raise ValueError("Inconsistent counters")
    route, method = row["route"], row["method"]
    if not isinstance(route,str) or not 0 < len(route) <= 256 or not isinstance(method,str) or method not in {"GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS","TRACE","CONNECT","OTHER"}:
        raise ValueError("Invalid route")
    return Slice(route=route, method=method,count=count,client_errors=client,server_errors=server,
                 sql_samples=samples,sql_queries=queries,histogram=bins)

@csrf_exempt
@require_POST
def ingest(request):
    auth = request.headers.get("Authorization","")
    if not auth.startswith("Bearer py_"):
        return JsonResponse({"error":"Service token required"}, status=401)
    token_hash = digest(auth[7:])
    if not Service.objects.filter(token_hash=token_hash).exists():
        return JsonResponse({"error":"Invalid service token"}, status=401)
    try:
        body = json.loads(request.body)
        batch_id = uuid.UUID(body["id"])
        stamp = datetime.fromtimestamp(body["timestamp"]/1000, tz.utc)
        now = timezone.now()
        if not now-timedelta(days=1) <= stamp <= now+timedelta(minutes=5):
            raise ValueError("Timestamp outside ingestion window")
        if not isinstance(body["routes"],list) or not 1 <= len(body["routes"]) <= 256:
            raise ValueError("Invalid number of routes")
        rows = [parse_row(row) for row in body["routes"]]
    except (ValueError, OverflowError, OSError, TypeError, KeyError, AttributeError):
        return JsonResponse({"error":"Invalid telemetry batch"}, status=400)
    with transaction.atomic():
        service = Service.objects.filter(token_hash=token_hash).first()
        if not service:
            return JsonResponse({"error":"Invalid service token"}, status=401)
        batch, created = Batch.objects.get_or_create(service=service,batch_id=batch_id,defaults={"at":stamp})
        if created:
            for row in rows:
                row.batch = batch
            Slice.objects.bulk_create(rows)
            Service.objects.filter(pk=service.pk).update(last_seen=now)
    return JsonResponse({"accepted": True},status=202)

def report(service, hours):
    since = timezone.now()-timedelta(hours=hours)
    rows = Slice.objects.filter(batch__service=service, batch__at__gte=since)
    bins = {f"b{i}":Sum(f"histogram__{i}",output_field=IntegerField()) for i in range(len(EDGES)+1)}
    groups = rows.values("method","route").annotate(n=Sum("count"), e=Sum("server_errors"),
            c=Sum("client_errors"),q=Sum("sql_queries"),s=Sum("sql_samples"),**bins).order_by("-n","route","method")
    count = errors = 0
    total_bins = [0]*(len(EDGES)+1)
    routes = []
    for g in groups:
        count += g["n"]
        errors += g["e"]
        hist = [g[f"b{i}"] for i in range(len(total_bins))]
        total_bins = [a+b for a,b in zip(total_bins,hist)]
        routes.append({"method":g["method"],"route":g["route"],"count":g["n"],"clientErrors":g["c"],
                       "serverErrors":g["e"],"p95UpperMs":percentile(hist,.95),"sqlSamples":g["s"],
                       "sqlQueriesPerRequest":g["q"]/g["s"] if g["s"] else None})
    grouping = TruncMinute if hours == 1 else TruncHour
    trend = list(rows.annotate(t=grouping("batch__at")).values("t").annotate(n=Sum("count"),errors=Sum("server_errors")).order_by("t"))
    return {"count":count,"errors":errors,"routes":routes,"trend":trend,
            "p95":percentile(total_bins,.95) if count else None,
            "availability":100*(count-errors)/count if count else None,
            "budgetUsed":errors*100/(count*(1-service.target/100)) if count and service.target < 100 else None}

def summary(service):
    return {"id":service.pk,"name":service.name,"framework":service.framework,"target":service.target,"lastSeen":service.last_seen}
