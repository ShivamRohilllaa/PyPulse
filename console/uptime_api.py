"""Session-authenticated JSON API for the operational console."""
from datetime import timedelta
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .audit import record
from .models import AlertChannel, Incident, Maintenance, Monitor, WorkspaceSettings
from .uptime_engine import in_maintenance, measure_monitor
from .uptime_forms import ChannelForm, MonitorForm, SharedMaintenanceForm
from .uptime_history import public_days
from . import uptime_views as ui
from .workspace_auth import json_data, json_view, signed_in


def api(func):
    @json_view
    @signed_in
    @wraps(func)
    def wrapped(request,*args,**kwargs):
        try:
            return func(request,*args,**kwargs)
        except PermissionDenied:
            return JsonResponse({"error":"Permission denied."},status=403)
        except Http404:
            return JsonResponse({"error":"Resource not found."},status=404)
    return wrapped


def invalid(form):
    # Return field names, never the failed value (possibly a credential).
    return JsonResponse({"error":"Invalid configuration.", "fields":list(form.errors)},status=400)


def edit_form(cls,instance,data):
    initial = cls(instance=instance)
    unknown = set(data) - set(initial.fields)
    if unknown:
        raise ValueError("Unknown configuration field.")
    values = {field.name:field.value() for field in initial if field.name not in ("options","configuration","options_patch","config_patch")}
    return cls({**values, **data},instance=instance)


def monitor_data(m):
    return {"id":m.pk, "name":m.name, "method":m.method, "url":ui.display_endpoint(m.url), "group":m.group,
            "environment":m.environment,"tags":m.tags,"state":m.state,"paused":m.paused,"public":m.public,
            "interval":m.interval,"timeout_ms":m.timeout_ms,"expected_status":m.expected_status,"status_expression":m.status_expression,
            "warn_ms":m.warn_ms,"failure_threshold":m.failure_threshold,"failures":m.failures,"request_configured":bool(m.request_secret),
            "channels":[c.pk for c in m.channels.all()],"escalation_channels":[c.pk for c in m.escalation_channels.all()],
            "escalation_channel":m.escalation_channel_id, "network_profile":m.network_profile_id, "alert_events":m.alert_events,
            "reminder_minutes":m.reminder_minutes,"escalation_minutes":m.escalation_minutes}


@api
@require_http_methods(["GET","POST","PUT","PATCH","DELETE"])
def monitors(request,pk=None):
    if request.method=="GET":
        rows = Monitor.objects.prefetch_related("channels","escalation_channels")
        if pk is not None:
            return JsonResponse(monitor_data(get_object_or_404(rows,pk=pk)))
        items = ui.list_stats(ui.latest_checks(rows.order_by("name","pk")))
        return JsonResponse([{**monitor_data(m),"last_check":m.last_check,"last_ms":m.last_ms,"last_reason":m.last_reason,
                              "uptime24h":m.availability} for m in items],safe=False)
    if request.method=="DELETE" and pk is not None:
        ui.monitor_action(request,pk,"delete")
        return JsonResponse({"deleted":True})
    if not ((pk is None and request.method=="POST") or (pk is not None and request.method in ("PUT","PATCH"))):
        return JsonResponse({"error":"Unsupported method."},status=405)
    ui.require_permission(request,"add_monitor" if pk is None else "change_monitor")
    with transaction.atomic():
        m = get_object_or_404(Monitor.objects.select_for_update(),pk=pk) if pk else Monitor()
        form = edit_form(MonitorForm,m,json_data(request))
        if not form.is_valid():
            return invalid(form)
        m = form.save()
        record(request,"Saved monitor",m)
    return JsonResponse(monitor_data(m),status=200 if pk else 201)


@api
@require_POST
def preview(request):
    data = dict(json_data(request))
    pk = data.pop("id",None)
    ui.require_permission(request,"change_monitor" if pk else "add_monitor")
    if pk is not None and (type(pk) is not int or pk<=0):
        raise ValueError("Invalid monitor ID.")
    m = get_object_or_404(Monitor,pk=pk) if pk else Monitor()
    form = edit_form(MonitorForm,m,data)
    if not form.is_valid():
        return invalid(form)
    candidate = form.save(commit=False)
    sample = measure_monitor(candidate,timeout_ms=min(5000,candidate.timeout_ms))
    return JsonResponse({"http":sample["status"],"ms":sample["ms"],"result":sample["reason"] or
                         ("Slow" if candidate.warn_ms and sample["ms"]>candidate.warn_ms else "Passed")})


@api
@require_POST
def action(request,pk,action):
    if action=="check":
        ui.require_permission(request,"change_monitor")
        m=get_object_or_404(Monitor,pk=pk)
        if m.paused:
            return JsonResponse({"error":"Monitor is paused."},status=409)
        from .views import check_now
        check_now(request,pk)
        return JsonResponse({"queued":True},status=202)
    if action=="clone":
        ui.require_permission(request,"add_monitor")
        source=get_object_or_404(Monitor,pk=pk)
        clone=ui.clone_monitor(request,source)
        return JsonResponse(monitor_data(clone),status=201)
    if action in ("pause","resume"):
        ui.monitor_action(request,pk,action)
        return JsonResponse(monitor_data(get_object_or_404(Monitor,pk=pk)))
    raise Http404


def issue_data(item):
    return {"id":item.pk,"monitor":item.monitor_id,"kind":item.kind,"reason":item.reason,
            "opened":item.opened,"closed":item.closed,"resolved_by":item.resolved_by,"acknowledged":item.acknowledged,
            "acknowledged_by":item.acknowledged_by,"acknowledged_at":item.acknowledged_at,"suppressed":item.suppressed,
            "escalated":item.escalated,"last_reminder":item.reminder_at}


def paginated(request,rows,serialize):
    limit = request.GET.get("limit","25")
    if not limit.isdecimal() or len(limit)>3 or not 1<=int(limit)<=100:
        raise ValueError("Limit must be 1–100.")
    page = Paginator(rows,int(limit)).get_page(request.GET.get("page"))
    return JsonResponse({"items":[serialize(row) for row in page],"page":page.number,
                         "pages":page.paginator.num_pages,"total":page.paginator.count})


@api
@require_http_methods(["GET"])
def history(request,pk,section):
    monitor=get_object_or_404(Monitor,pk=pk)
    if section=="alerts":
        return paginated(request,monitor.incidents.order_by("-opened","-pk"),issue_data)
    period=request.GET.get("range","24h")
    if period not in ui.RANGES:
        raise ValueError("Choose 1h, 24h, 7d, 30d or 90d.")
    if section=="checks":
        hours=ui.RANGES[period][0]
        rows=monitor.checks.filter(at__gte=timezone.now()-timedelta(hours=hours)).order_by("-at","-pk")
        result=request.GET.get("result","all")
        if result=="failed": rows=rows.filter(ok=False)
        if result=="slow": rows=rows.filter(slow=True)
        if result=="issues": rows=rows.filter(Q(ok=False)|Q(slow=True))
        return paginated(request,rows,lambda c:{"at":c.at,"status":c.status,"ms":c.ms,"passed":c.ok,"slow":c.slow,"reason":c.reason})
    ctx=ui.measurement_context(monitor,period)
    return JsonResponse({k:ctx[k] for k in ("period","count","availability","mean","p95","p99","slow_count","incident_count","coverage","approximate","series")})


@api
@require_http_methods(["GET"])
def alerts(request):
    rows=Incident.objects.order_by("-opened","-pk")
    scope=request.GET.get("status","all")
    filters={"open":{"closed":None},"active":{"closed":None,"acknowledged":False},"acknowledged":{"closed":None,"acknowledged":True},"resolved":{"closed__isnull":False}}
    if scope in filters: rows=rows.filter(**filters[scope])
    return paginated(request,rows,issue_data)


@api
@require_POST
def alert_action(request,pk,verb):
    (ui.acknowledge if verb=="ack" else ui.resolve)(request,pk)
    return JsonResponse(issue_data(get_object_or_404(Incident,pk=pk)))


@api
@require_http_methods(["GET"])
def notifications(request,pk):
    rows=get_object_or_404(Incident,pk=pk).alertdelivery_set.order_by("-at","-pk")
    return paginated(request,rows,lambda d:{"id":d.pk,"at":d.at,"channel":d.channel_id,"event":d.event,"result":d.disposition,"attempts":d.attempts})


def channel_data(c):
    return {"id":c.pk,"name":c.name,"kind":c.kind,"enabled":c.enabled,"escalation_only":c.escalation_only,"configured":bool(c.config_secret)}


@api
@require_http_methods(["GET","POST","PUT","PATCH","DELETE"])
def channels(request,pk=None):
    if request.method=="GET":
        ui.require_permission(request,"view_alertchannel")
        if pk: return JsonResponse(channel_data(get_object_or_404(AlertChannel,pk=pk)))
        return JsonResponse([channel_data(c) for c in AlertChannel.objects.order_by("name")],safe=False)
    if request.method=="DELETE" and pk:
        ui.channel_action(request,pk,"delete")
        return JsonResponse({"deleted":True})
    if not ((pk is None and request.method=="POST") or (pk and request.method in ("PUT","PATCH"))):
        return JsonResponse({"error":"Unsupported method."},status=405)
    ui.require_permission(request,"change_alertchannel" if pk else "add_alertchannel")
    with transaction.atomic():
        obj=get_object_or_404(AlertChannel.objects.select_for_update(),pk=pk) if pk else AlertChannel()
        form=edit_form(ChannelForm,obj,json_data(request))
        if not form.is_valid(): return invalid(form)
        obj=form.save()
        record(request,"Saved alert channel",obj)
    return JsonResponse(channel_data(obj),status=200 if pk else 201)


@api
@require_POST
def test_channel(request,pk):
    ui.require_permission(request,"change_alertchannel")
    channel=get_object_or_404(AlertChannel,pk=pk)
    if not channel.enabled or not channel.config_secret:
        return JsonResponse({"error":"Enable and configure the channel first."},status=409)
    ui.channel_action(request,pk,"test")
    return JsonResponse({"queued":True},status=202)


def window_data(w):
    return {"id":w.pk,"name":w.name,"start":w.start,"end":w.end,"recurrence":w.recurrence,
            "all_monitors":w.all_monitors,"targets":[w.monitor_id] if w.monitor_id else [m.pk for m in w.targets.all()]}


@api
@require_http_methods(["GET","POST","PUT","PATCH","DELETE"])
def windows(request,pk=None):
    if request.method=="GET":
        if pk: return JsonResponse(window_data(get_object_or_404(Maintenance,pk=pk)))
        return JsonResponse([window_data(w) for w in Maintenance.objects.prefetch_related("targets").order_by("start","pk")],safe=False)
    if request.method=="DELETE" and pk:
        ui.remove_shared_window(request,pk)
        return JsonResponse({"deleted":True})
    if not ((pk is None and request.method=="POST") or (pk and request.method in ("PUT","PATCH"))):
        return JsonResponse({"error":"Unsupported method."},status=405)
    ui.require_permission(request,"change_maintenance" if pk else "add_maintenance")
    with transaction.atomic():
        obj=get_object_or_404(Maintenance.objects.select_for_update(),pk=pk) if pk else Maintenance()
        data=json_data(request)
        if obj.monitor_id: data.setdefault("targets",[obj.monitor_id])
        form=edit_form(SharedMaintenanceForm,obj,data)
        if not form.is_valid(): return invalid(form)
        obj=form.save_window()
        record(request,"Saved maintenance",obj)
    return JsonResponse(window_data(obj),status=200 if pk else 201)


@never_cache
@require_http_methods(["GET"])
def public_status(request):
    settings=WorkspaceSettings.current()
    if not settings.status_enabled: return JsonResponse({"error":"Not published."},status=404)
    monitors=ui.list_stats(Monitor.objects.filter(public=True,paused=False).order_by("name"))
    now=timezone.now()
    public_days(monitors,now)
    for m in monitors:
        m.maintenance_active=in_maintenance(m,now)
    items=[{"name":m.name,"state":"Maintenance" if m.maintenance_active else m.state,"uptime24h":m.availability,
            "uptime90d":m.uptime90,"days":m.days} for m in monitors]
    active=Incident.objects.filter(closed=None,monitor__in=monitors).select_related("monitor").order_by("-opened")
    return JsonResponse({"title":settings.status_title,"summary":ui.status_summary(monitors),"monitors":items,"incidents":[{"name":i.monitor.name,"kind":i.kind,"since":i.opened} for i in active]})
