import math
from collections import Counter
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, F, FilteredRelation, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import StrIndex, Substr, TruncDay, TruncHour, TruncMinute
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from .models import AlertChannel, AlertDelivery, Audit, Check, Incident, Maintenance, Monitor
from .audit import record
from .latency_buckets import quantile
from .uptime_history import history_charts, public_days
from .uptime_engine import in_maintenance, measure_monitor, occurrence
from .uptime_forms import ChannelForm, MaintenanceForm, MonitorForm, SharedMaintenanceForm


def require_permission(request, code):
    if not request.user.has_perm("console." + code):
        raise PermissionDenied


def audit(request, message, obj=None):
    record(request, message, obj)


def display_endpoint(value):
    if "{{" in value:
        return value.split("?")[0].split("#")[0]
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc.split('@')[-1]}{parts.path}"


def latest_checks(rows):
    latest = Check.objects.filter(monitor_id=OuterRef("pk")).order_by("-at","-pk")
    return rows.annotate(last_check=Subquery(latest.values("at")[:1]),
                         last_ms=Subquery(latest.values("ms")[:1]),
                         last_reason=Subquery(latest.values("reason")[:1]))


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


def measurement_context(monitor, period, check_filter="all", page=None):
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
    rolled = list(rolled)
    slow_count = sum(r["slow"] for r in raw) + sum(r.slow_count for r in rolled)
    count = raw_count + sum(r.total for r in rolled)
    passed = sum(r["passed"] for r in raw) + sum(r.passed for r in rolled)
    total_ms = sum(r["elapsed"] for r in raw) + sum(r.elapsed_sum_ms for r in rolled)
    chart = history_charts(checks,raw,rolled,start,now,trunc,width)
    mixed = chart.pop("merged_histogram")
    coverage = raw_count
    if mixed:
        p95, p99 = quantile(mixed,.95), quantile(mixed,.99)
        coverage = sum(mixed.values())
    filters = {"failed": Q(ok=False), "slow": Q(slow=True), "issues": Q(ok=False) | Q(slow=True)}
    history = checks.filter(filters[check_filter]) if check_filter in filters else checks
    history_page = Paginator(history.order_by("-at","-pk"), 25).get_page(page)
    return {**chart, "coverage":coverage, "approximate":bool(mixed), "period": period, "periods": RANGES.keys(), "checks": history_page, "check_filter":check_filter if check_filter in filters else "all",
            "count": count, "p99": p99, "slow_count": slow_count, "incident_count":monitor.incidents.filter(opened__gte=start, opened__lte=now).count(), "raw_count":raw_count, "rollups":list(rolled)[-48:][::-1], "mean": total_ms/count if count else None, "p95": p95, "availability": 100 * passed / count if count else None,
            "start": start, "end": now}


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
    monitors = list_stats(latest_checks(monitors))
    states = Counter(m.state for m in monitors if not m.paused)
    grouping = request.GET.get("by","")
    if grouping not in ("group","environment"):
        grouping = ""
    severity = {"Down":0,"Slow":1,"Pending":2,"Up":3}
    monitors.sort(key=lambda m: (getattr(m,grouping,"").casefold(), 4 if m.paused else severity.get(m.state,4),m.name.casefold(),m.pk))
    grouped = {}
    for m in monitors:
        m.display_url = display_endpoint(m.url)
        group = getattr(m,grouping,"") or ("Unassigned" if grouping else "Endpoints")
        grouped.setdefault(group,[]).append(m)
    open_issues = Incident.objects.filter(closed=None,monitor__in=monitors,monitor__paused=False).select_related("monitor").order_by("-opened")[:12]
    template = "console/uptime_list.html" if request.GET.get("partial") else "console/monitors.html"
    return render(request, template, {"section":"Uptime", "monitors":monitors, "grouped": grouped, "by":grouping, "active_issues":open_issues, "up":states["Up"], "slow":states["Slow"], "down":states["Down"], "states":state_values, "selected_state":selected_state, "q":search, "groups":groups, "group":chosen, "environments":environments,"environment":environment,"tags":tags,"tag":tag})


@login_required
def monitor_detail(request, pk):
    item = get_object_or_404(Monitor, pk=pk)
    period = request.GET.get("range","24h")
    if period not in RANGES:
        period = "24h"
    context = measurement_context(item,period,request.GET.get("result","all"),request.GET.get("checks_page"))
    # Legacy URLs might embed query credentials; never render them in detail or status pages.
    context.update(section="Uptime", monitor=item, endpoint=display_endpoint(item.url),
                   maintenance=in_maintenance(item, timezone.now()), windows=Maintenance.for_monitor(item).order_by("start"),
                   maintenance_form=MaintenanceForm(), recent_incidents=Paginator(item.incidents.order_by("-opened","-pk"),10).get_page(request.GET.get("ipage")))
    template = "console/uptime_measurements.html" if request.GET.get("partial") else "console/monitor.html"
    return render(request,template,context)


@login_required
@require_POST
@csrf_protect
@never_cache
def preview_monitor(request, pk=None):
    require_permission(request,"change_monitor" if pk else "add_monitor")
    item = get_object_or_404(Monitor,pk=pk) if pk else None
    form = MonitorForm(request.POST,instance=item)
    if form.is_valid():
        candidate = form.save(commit=False)
        result = measure_monitor(candidate, timeout_ms=min(5000, candidate.timeout_ms))
        slow = candidate.warn_ms and result["ms"] > candidate.warn_ms
        payload = {"result":result["reason"] or ("Slow" if slow else "Passed"),
                   "http":result["status"], "ms":round(result["ms"],1)}
        status_code = 200
    else:
        labels = [form.fields[field].label or field.replace("_"," ") for field in form.errors if field in form.fields]
        payload = {"result":"Review " + ", ".join(labels or ["monitor settings"]), "http":None, "ms":None}
        status_code = 400
    if request.headers.get("Accept") == "application/json":
        return JsonResponse(payload,status=status_code)
    return render(request,"console/monitor_preview.html",{"section":"Uptime", "preview":payload},status=status_code)


@transaction.atomic
def clone_monitor(request,monitor):
    escalation_ids = list(monitor.escalation_channels.values_list("pk",flat=True))
    channel_ids = list(monitor.channels.values_list("pk",flat=True))
    monitor.pk = None
    monitor.name = monitor.name[:112] + " (copy)"
    monitor.public, monitor.paused = False, True
    monitor.state, monitor.failures, monitor.next_at = "Pending", 0, timezone.now()
    monitor.save(force_insert=True)
    monitor.channels.set(channel_ids)
    monitor.escalation_channels.set(escalation_ids)
    audit(request, "Cloned monitor " + monitor.name,monitor)
    return monitor


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
        clone = clone_monitor(request,monitor)
        return redirect("edit_monitor",pk=clone.pk)
    if action == "delete":
        audit(request, "Deleted monitor " + monitor.name,monitor)
        monitor.delete()
        return redirect("monitors")
    Monitor.objects.filter(pk=pk).update(paused=action == "pause", next_at=timezone.now())
    audit(request, f"{action.title()} monitor {monitor.name}", monitor)
    return redirect("monitor", pk=pk)


@login_required
@require_http_methods(["GET","POST"])
def maintenance(request, pk=None):
    window = get_object_or_404(Maintenance,pk=pk) if pk else None
    if pk:
        require_permission(request,"change_maintenance")
    initial = {"targets":[window.monitor_id]} if window and window.monitor_id else {}
    form = SharedMaintenanceForm(request.POST if request.method == "POST" else None, instance=window, initial=initial)
    if request.method == "POST":
        require_permission(request, "change_maintenance" if pk else "add_maintenance")
        if form.is_valid():
            with transaction.atomic():
                window = form.save_window()
                audit(request, "Saved maintenance " + window.name, window)
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
    return render(request, template, {"section":"Uptime", "form":form, "editing":pk, "windows":page}, status=400 if form.is_bound else 200)


@login_required
@require_POST
def remove_shared_window(request, window_pk):
    require_permission(request, "delete_maintenance")
    window = get_object_or_404(Maintenance, pk=window_pk)
    with transaction.atomic():
        audit(request, "Removed maintenance " + (window.name or str(window.pk)), window)
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
        audit(request,"Scheduled maintenance for " + monitor.name, window)
        return redirect("monitor",pk=pk)
    return render(request,"console/form.html",{"section":"Uptime","title":"Schedule maintenance", "button":"Save window","form":form},status=400)


@login_required
@require_POST
def delete_window(request, pk, window_pk):
    require_permission(request, "delete_maintenance")
    window = get_object_or_404(Maintenance, pk=window_pk, monitor_id=pk)
    audit(request,"Removed maintenance for " + window.monitor.name, window)
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
        audit(request, f"Acknowledged incident {pk}", item)
    return redirect("incidents")


@login_required
@require_POST
def resolve(request, pk):
    require_permission(request,"change_incident")
    item = get_object_or_404(Incident, pk=pk)
    with transaction.atomic():
        changed = Incident.objects.filter(pk=pk,closed=None).update(closed=timezone.now(),resolved_by=request.user.get_username(),
            next_reminder=None,next_escalation=None)
        if changed:
            audit(request,"Resolved incident",item)
    return redirect("incidents")


@login_required
def incident_detail(request,pk):
    item = get_object_or_404(Incident.objects.select_related("monitor"),pk=pk)
    deliveries = item.alertdelivery_set.select_related("channel").order_by("-at","-pk")
    return render(request, "console/incident_detail.html", {"section":"Incidents","item":item,
        "deliveries":Paginator(deliveries,25).get_page(request.GET.get("page"))})


def status_summary(monitors):
    if any(m.state == "Down" and not m.maintenance_active for m in monitors):
        return "Service disruption"
    if any(m.state == "Slow" and not m.maintenance_active for m in monitors):
        return "Degraded performance"
    if any(m.maintenance_active for m in monitors):
        return "Scheduled maintenance"
    if not monitors or any(m.state == "Pending" for m in monitors):
        return "Awaiting observations"
    return "All services operational"


def status(request):
    from .models import WorkspaceSettings
    workspace = WorkspaceSettings.current()
    if not workspace.status_enabled:
        raise Http404
    monitors = list_stats(Monitor.objects.filter(public=True,paused=False).order_by("group","name"))
    for monitor in monitors:
        monitor.maintenance_active = in_maintenance(monitor,timezone.now())
    public_days(monitors,timezone.now())
    active = Incident.objects.filter(closed=None,monitor__in=monitors).select_related("monitor").order_by("-opened")
    template = "console/status_rows.html" if request.GET.get("partial") else "console/status.html"
    return render(request,template,{"monitors":monitors, "active":active, "summary":status_summary(monitors), "status_title":workspace.status_title})


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
        audit(request,"Saved alert channel " + channel.name, channel)
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
        audit(request,"Deleted alert channel " + channel.name, channel)
        channel.delete()
    elif channel.enabled and channel.config_secret:
        AlertDelivery.objects.create(channel=channel,event="Test",state="Test")
        audit(request,"Queued channel test for " + channel.name, channel)
        messages.success(request,"Test queued. Delivery history updates when the worker sends it.")
    else:
        messages.error(request,"Enable and configure this channel before sending a test.")
    return redirect("channels")
