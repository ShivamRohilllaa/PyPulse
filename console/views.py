from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from .audit import activity_page, record
from .report_summary import explain
from .forms import ServiceForm, MonitorForm
from .models import Monitor, Run, Service
from .telemetry import WINDOWS, digest, issue_token, report, summary


def window(request):
    try:
        hours=int(request.GET.get("hours",1))
    except ValueError:
        return 1
    return hours if hours in WINDOWS else 1

def can_write(request, codename):
    if not request.user.has_perm("console."+codename):
        raise PermissionDenied

@login_required
def traffic(request):
    services=Service.objects.order_by("name")
    choice=request.GET.get("service")
    service=get_object_or_404(Service,pk=choice) if choice and choice.isdecimal() else services.first()
    hours=window(request)
    metrics=report(service,hours) if service else {}
    context={"section":"Traffic","services":services,"service":service,"hours":hours,"m":metrics}
    template="console/traffic_metrics.html" if request.GET.get("partial") else "console/traffic.html"
    return render(request,template,context)

@login_required
@never_cache
def connect(request):
    can_write(request,"add_service")
    form=ServiceForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        token=issue_token()
        service=form.save(commit=False)
        service.token_hash=digest(token)
        try:
            with transaction.atomic():
                service.save()
                record(request, "Connected Python service", service, details={"name":service.name,"framework":service.framework})
        except IntegrityError:
            form.add_error("name","This service name is already in use.")
        else:
            return render(request,"console/token.html",{"service":service,"token":token,"section":"Traffic"})
    return render(request,"console/form.html",{"title":"Connect service","section":"Traffic","form":form,"button":"Create service"})

@login_required
@require_POST
@never_cache
def rotate(request,pk):
    can_write(request,"change_service")
    service=get_object_or_404(Service,pk=pk)
    token=issue_token()
    service.token_hash=digest(token)
    with transaction.atomic():
        service.save(update_fields=["token_hash"])
        record(request, "Rotated service ingest token", service)
    return render(request,"console/token.html",{"service":service,"token":token,"section":"Traffic"})

@login_required
@require_http_methods(["GET","POST"])
def edit_target(request, pk):
    can_write(request, "change_service")
    service = get_object_or_404(Service, pk=pk)
    from django import forms
    class TargetForm(forms.Form):
        target = forms.FloatField(min_value=1, max_value=99.999, label="Availability target (%)")
    form = TargetForm(request.POST if request.method == "POST" else None, initial={"target": service.target})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            service.target = form.cleaned_data["target"]
            service.save(update_fields=["target"])
            record(request, "Updated service availability target", service, details={"target": service.target})
        return redirect(f"/?service={service.pk}")
    return render(request, "console/form.html", {"section": "Traffic", "form": form,
                  "title": f"Availability target for {service.name}", "button": "Save target"})


def api_report(request,pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error":"Sign in"},status=401)
    service=get_object_or_404(Service,pk=pk)
    return JsonResponse({"service":summary(service),**report(service,window(request))})

from .uptime_views import acknowledge, incidents, monitoring, monitor_detail, status

@login_required
def configure(request,pk=None):
    can_write(request, "change_monitor" if pk else "add_monitor")
    item=get_object_or_404(Monitor,pk=pk) if pk else None
    form=MonitorForm(request.POST if request.method == "POST" else None, instance=item)
    if request.method=="POST" and form.is_valid():
        obj=form.save(commit=False)
        with transaction.atomic():
            obj.save()
            form.save_m2m()
            record(request, "Saved monitor", obj, details={"name": obj.name})
        return redirect("monitors")
    title=("Edit " if pk else "New ")+"monitor"
    template="console/monitor_form.html"
    return render(request,template,{"title":title,"section":"Uptime","form":form,"button":"Save"})

@login_required
@require_POST
def check_now(request,pk):
    can_write(request,"change_monitor")
    monitor=get_object_or_404(Monitor,pk=pk)
    if not monitor.paused:
        Monitor.objects.filter(pk=pk).update(next_at=timezone.now())
        messages.success(request,"Check queued for the worker.")
        record(request, "Queued monitor check", monitor, details={"name": monitor.name})
    return redirect("monitor",pk=pk)

@login_required
@require_POST
def stop(request,pk):
    can_write(request,"change_run")
    from .lab import stop_run
    try:
        stop_run(pk, request)
    except Run.DoesNotExist:
        raise Http404 from None
    return redirect("run",pk=pk)

@login_required
def run_detail(request,pk):
    item=get_object_or_404(Run.objects.select_related("exercise"),pk=pk)
    other=request.GET.get("compare","")
    previous=Run.objects.filter(pk=int(other),exercise=item.exercise).exclude(pk=pk).only("pk","result").first() if other.isascii() and other.isdecimal() and len(other)<19 else None
    sample_filter = request.GET.get("samples", "all")
    from .benchstats import reason
    samples = (item.samples if sample_filter not in {"failed","slowest"} else
               ([r for r in item.samples if reason(r)] if sample_filter == "failed" else sorted(item.samples, key=lambda r: r[1], reverse=True)))
    timeline=Paginator(samples,40).get_page(request.GET.get("page") or 1)
    template="console/run_metrics.html" if request.GET.get("partial") else "console/run.html"
    return render(request,template,{"section":"Benchmarks","run":item,"m":item.result,"previous":previous,
        "timeline":timeline, "sample_filter":sample_filter, "summary":explain(item),
        "history": Run.objects.filter(exercise_id=item.exercise_id,finished__isnull=False).exclude(pk=pk).only("pk","started").order_by("-pk")[:20] if item.exercise_id else []})

@login_required
def export(request,pk):
    from .lab_views import write_sample_rows
    run = get_object_or_404(Run.objects.only("samples"), pk=pk)
    response = HttpResponse(content_type="text/csv", headers={"Content-Disposition": f'attachment; filename="pypulse-run-{pk}.csv"'})
    write_sample_rows(run, response)
    return response


@login_required
def activity(request):
    query = request.GET.get("q", "").strip()[:120]
    rows = activity_page(request.GET)
    return render(request, "console/activity.html", {"section": "Activity", "rows": rows, "q": query})

def health(request):
    return JsonResponse({"status":"ok"})
