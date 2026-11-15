import math
from collections import Counter
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, FilteredRelation, Q, Sum, Value, When
from django.db.models.functions import StrIndex, Substr, TruncDay, TruncHour, TruncMinute
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .models import AlertChannel, AlertDelivery, Audit, Incident, Maintenance, Monitor
from .uptime_engine import in_maintenance, occurrence
from .uptime_forms import ChannelForm, MaintenanceForm, SharedMaintenanceForm


def require_permission(request, code):
    if not request.user.has_perm("console." + code):
        raise PermissionDenied


def audit(request, message):
    Audit.objects.create(actor=request.user.get_username(), action=message[:200])


def list_stats(rows):
    now = timezone.now()
    measured = Q(checks__at__gte=now-timedelta(hours=24), checks__at__lte=now)
    rows = list(rows.annotate(recent=FilteredRelation("checks", condition=measured)).annotate(
        n=Count("recent"), good=Count("recent",filter=Q(recent__ok=True))))
    for row in rows:
        row.availability = round(100 * row.good / row.n, 2) if row.n else None
    return rows


RANGES = {"1h": (1, TruncMinute, 60), "24h": (24, TruncHour, 3600),
          "7d": (168, TruncHour, 3600), "30d": (720, TruncDay, 86400), "90d": (2160, TruncDay, 86400)}


def measurement_context(monitor, period):
    hours, trunc, width = RANGES[period]
    now = timezone.now()
    start = now - timedelta(hours=hours)
    checks = monitor.checks.filter(at__gte=start, at__lte=now)
    grouped = checks.annotate(bucket=trunc("at")).values("bucket").annotate(
        n=Count("id"), passed=Count("id", filter=Q(ok=True)), slow=Count("id", filter=Q(slow=True)), elapsed=Sum("ms")).order_by("bucket")
    raw = list(grouped)
    raw_count = sum(row["n"] for row in raw)
    def percentile(fraction):
        if not raw_count:
            return None
        offset = math.ceil(fraction * raw_count) - 1
        return next(iter(checks.order_by("ms").values_list("ms", flat=True)[offset:offset+1]), None)
    p95, p99 = percentile(.95), percentile(.99)
    rolled = monitor.hours.filter(hour__gte=start,hour__lt=now).order_by("hour")
    slow_count = sum(row["slow"] for row in raw)
    buckets = {}
    passed, count, total_ms = 0, 0, 0
    def include(at, n, successes, elapsed):
        nonlocal passed, count, total_ms
        bucket = int(at.timestamp()) // width
        old_n, old_ms = buckets.get(bucket, (0,0))
        buckets[bucket] = (old_n + n, old_ms + elapsed)
        passed, count, total_ms = passed + successes, count + n, total_ms + elapsed
    for row in raw:
        include(row["bucket"],row["n"],row["passed"],row["elapsed"])
    for row in rolled:
        include(row.hour,row.total,row.passed,row.elapsed_sum_ms)
        slow_count += row.slow_count
    origin = int(start.timestamp()) // width
    stop = int(now.timestamp()) // width
    means = {bucket: elapsed/n for bucket,(n,elapsed) in buckets.items() if n}
    peak = max(means.values(), default=0)
    segments, points, previous = [], [], None
    for bucket, ms in sorted(means.items()):
        x = round(1000 * (bucket - origin) / max(1, stop-origin), 2)
        y = round(95 - 90 * ms / max(peak, 1), 2)
        if previous is None or bucket != previous + 1:
            segments.append([])
        segments[-1].append(f"{x},{y}")
        points.append((x, y))
        previous = bucket
    return {"period": period, "periods": RANGES.keys(), "checks": checks.order_by("-at","-id")[:80],
            "count": count, "p99": p99, "slow_count": slow_count, "incident_count":monitor.incidents.filter(opened__gte=start, opened__lte=now).count(), "raw_count":raw_count, "rollups":list(rolled)[-48:][::-1], "mean": total_ms/count if count else None, "p95": p95, "availability": 100 * passed / count if count else None,
            "peak": peak, "points": points, "segments": [" ".join(g) for g in segments], "start": start, "end": now}


@login_required
def monitoring(request):
    chosen = request.GET.get("group","")
    monitors = Monitor.objects.order_by("group","name")
    groups = monitors.exclude(group="").order_by("group").values_list("group",flat=True).distinct()
    environment = request.GET.get("environment", "")
    tag = request.GET.get("tag", "").strip().lower()
    environments = monitors.exclude(environment="").order_by("environment").values_list("environment",flat=True).distinct()
    tags = sorted({t.strip() for raw in monitors.exclude(tags="").values_list("tags",flat=True).distinct() for t in raw.split(",") if t.strip()})
    search = request.GET.get("q", "").strip()[:200]
    state_values = ("Up", "Slow", "Down", "Pending", "Paused")
    selected_state = next((s for s in state_values if s.lower() == request.GET.get("state", "").lower()), "")
    if search:
        endpoint = Case(When(url__contains="?", then=Substr("url",1,StrIndex("url",Value("?"))-1)), default=F("url"))
        monitors = monitors.alias(safe_endpoint=endpoint).filter(Q(name__icontains=search) | Q(safe_endpoint__icontains=search))
    if selected_state:
        monitors = monitors.filter(paused=True) if selected_state == "Paused" else monitors.filter(state=selected_state, paused=False)
    if tag:
        monitors = monitors.filter(Q(tags=tag) | Q(tags__startswith=tag+", ") | Q(tags__endswith=", "+tag) | Q(tags__contains=", "+tag+", "))
    if environment:
        monitors = monitors.filter(environment=environment)
    if chosen:
        monitors = monitors.filter(group=chosen)
    monitors = list_stats(monitors)
    states = Counter(m.state for m in monitors if not m.paused)
    template = "console/uptime_list.html" if request.GET.get("partial") else "console/monitors.html"
    return render(request, template, {"section":"Uptime", "monitors":monitors, "up":states["Up"], "slow":states["Slow"], "down":states["Down"], "states":state_values, "selected_state":selected_state, "q":search, "groups":groups, "group":chosen, "environments":environments,"environment":environment,"tags":tags,"tag":tag})


@login_required
def monitor_detail(request, pk):
    item = get_object_or_404(Monitor, pk=pk)
    period = request.GET.get("range","24h")
    if period not in RANGES:
        period = "24h"
    context = measurement_context(item,period)
    # Legacy URLs might embed query credentials; never render them in detail or status pages.
    endpoint = urlsplit(item.url)
    context.update(section="Uptime", monitor=item, endpoint=item.url.split("?")[0] if "{{" in item.url else f"{endpoint.scheme}://{endpoint.netloc.split('@')[-1]}{endpoint.path}",
                   maintenance=in_maintenance(item, timezone.now()), windows=Maintenance.for_monitor(item).order_by("start"),
                   maintenance_form=MaintenanceForm(), recent_incidents=Paginator(item.incidents.order_by("-opened","-pk"),10).get_page(request.GET.get("ipage")))
    template = "console/uptime_measurements.html" if request.GET.get("partial") else "console/monitor.html"
    return render(request,template,context)


@login_required
@require_POST
@transaction.atomic
def monitor_action(request, pk, action):
    perms = {"clone":"add_monitor", "pause":"change_monitor", "resume":"change_monitor", "delete":"delete_monitor"}
    if action not in perms:
        raise Http404
    require_permission(request, perms[action])
    monitor = get_object_or_404(Monitor, pk=pk)
    if action == "clone":
        channel_ids = list(monitor.channels.values_list("pk",flat=True))
        monitor.pk = None
        monitor.name = monitor.name[:112] + " (copy)"
        monitor.public, monitor.paused = False, True
        monitor.state, monitor.failures, monitor.next_at = "Pending", 0, timezone.now()
        monitor.save(force_insert=True)
        monitor.channels.set(channel_ids)
        audit(request, "Cloned monitor " + monitor.name)
        return redirect("edit_monitor", pk=monitor.pk)
    if action == "delete":
        monitor.delete()
        audit(request, "Deleted monitor " + monitor.name)
        return redirect("monitors")
    Monitor.objects.filter(pk=pk).update(paused=action == "pause", next_at=timezone.now())
    audit(request, f"{action.title()} monitor {monitor.name}")
    return redirect("monitor", pk=pk)


@login_required
@require_http_methods(["GET","POST"])
def maintenance(request):
    form = SharedMaintenanceForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        require_permission(request, "add_maintenance")
        if form.is_valid():
            with transaction.atomic():
                window = form.save_window()
                audit(request, "Scheduled maintenance " + window.name)
            return redirect("maintenance")
    now = timezone.now()
    rows = list(Maintenance.objects.select_related("monitor").prefetch_related("targets"))
    for row in rows:
        row.occurs, row.until = occurrence(row, now)
        row.stage = "Active" if row.occurs <= now < row.until else "Upcoming" if row.occurs > now else "Ended"
    rows.sort(key=lambda row: ({"Active":0,"Upcoming":1,"Ended":2}[row.stage],
                               -row.until.timestamp() if row.stage == "Ended" else row.occurs.timestamp(), row.pk))
    page = Paginator(rows,25).get_page(request.GET.get("page"))
    template = "console/maintenance_rows.html" if request.GET.get("partial") and request.method == "GET" else "console/maintenance.html"
    return render(request, template, {"section":"Uptime", "form":form, "windows":page}, status=400 if form.is_bound else 200)


@login_required
@require_POST
def remove_shared_window(request, window_pk):
    require_permission(request, "delete_maintenance")
    window = get_object_or_404(Maintenance, pk=window_pk)
    with transaction.atomic():
        audit(request, "Removed maintenance " + (window.name or str(window.pk)))
        window.delete()
    return redirect("maintenance")


@login_required
@require_POST
def add_window(request, pk):
    require_permission(request, "add_maintenance")
    monitor = get_object_or_404(Monitor,pk=pk)
    form = MaintenanceForm(request.POST)
    if form.is_valid():
        window = form.save(commit=False)
        window.monitor = monitor
        window.save()
        audit(request,"Scheduled maintenance for " + monitor.name)
        return redirect("monitor",pk=pk)
    return render(request,"console/form.html",{"section":"Uptime","title":"Schedule maintenance", "button":"Save window","form":form},status=400)


@login_required
@require_POST
def delete_window(request, pk, window_pk):
    require_permission(request, "delete_maintenance")
    window = get_object_or_404(Maintenance, pk=window_pk, monitor_id=pk)
    audit(request,"Removed maintenance for " + window.monitor.name)
    window.delete()
    return redirect("monitor",pk=pk)


@login_required
def incidents(request):
    scope = request.GET.get("state","all")
    rows = Incident.objects.select_related("monitor").order_by("-opened")
    filters = {"open":{"closed__isnull":True},"acknowledged":{"closed__isnull":True,"acknowledged":True},"closed":{"closed__isnull":False}}
    if scope in filters:
        rows = rows.filter(**filters[scope])
    else:
        scope = "all"
    template = "console/incident_rows.html" if request.GET.get("partial") else "console/incidents.html"
    return render(request, template, {"section":"Incidents", "scope":scope, "items":Paginator(rows.order_by("-opened","-pk"),25).get_page(request.GET.get("page"))})


@login_required
@require_POST
def acknowledge(request,pk):
    require_permission(request, "change_incident")
    item = get_object_or_404(Incident,pk=pk)
    updated = Incident.objects.filter(pk=item.pk,closed=None,acknowledged=False).update(
        acknowledged=True, acknowledged_by=request.user.get_username(), acknowledged_at=timezone.now(), next_reminder=None, next_escalation=None)
    if updated:
        audit(request, f"Acknowledged incident {pk}")
    return redirect("incidents")


def status(request):
    from .models import WorkspaceSettings
    workspace = WorkspaceSettings.current()
    if not workspace.status_enabled:
        raise Http404
    monitors = list_stats(Monitor.objects.filter(public=True,paused=False).order_by("group","name"))
    for monitor in monitors:
        monitor.maintenance_active = in_maintenance(monitor,timezone.now())
    template = "console/status_rows.html" if request.GET.get("partial") else "console/status.html"
    return render(request,template,{"monitors":monitors, "status_title":workspace.status_title})


@login_required
def channels(request):
    require_permission(request,"view_alertchannel")
    template = "console/channel_deliveries.html" if request.GET.get("partial") else "console/channels.html"
    return render(request,template,{"section":"Incidents", "channels":AlertChannel.objects.order_by("name"),
                  "deliveries":AlertDelivery.objects.select_related("channel","monitor").order_by("-pk")[:50]})


@login_required
@never_cache
@require_http_methods(["GET","POST"])
def channel_form(request,pk=None):
    require_permission(request, "change_alertchannel" if pk else "add_alertchannel")
    channel = get_object_or_404(AlertChannel,pk=pk) if pk else None
    form = ChannelForm(request.POST if request.method == "POST" else None,instance=channel)
    if request.method == "POST" and form.is_valid():
        channel = form.save()
        audit(request,"Saved alert channel " + channel.name)
        return redirect("channels")
    return render(request,"console/channel_form.html",{"section":"Incidents","title":"Edit channel" if pk else "New channel","form":form})


@login_required
@require_POST
def channel_action(request,pk,action):
    perms = {"delete":"delete_alertchannel", "test":"change_alertchannel"}
    if action not in perms:
        raise Http404
    require_permission(request,perms[action])
    channel = get_object_or_404(AlertChannel,pk=pk)
    if action == "delete":
        audit(request,"Deleted alert channel " + channel.name)
        channel.delete()
    elif channel.enabled and channel.config_secret:
        AlertDelivery.objects.create(channel=channel,event="Test",state="Test")
        audit(request,"Queued channel test for " + channel.name)
        messages.success(request,"Test queued. Delivery history updates when the worker sends it.")
    else:
        messages.error(request,"Enable and configure this channel before sending a test.")
    return redirect("channels")
