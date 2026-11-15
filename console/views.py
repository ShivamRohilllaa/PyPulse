import csv
import json
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from .engine import enqueue
from .forms import ServiceForm, ExerciseForm, MonitorForm
from .models import Audit, Exercise, Incident, Monitor, Run, Service
from .telemetry import WINDOWS, digest, issue_token, report, summary


def log_action(request, text):
    Audit.objects.create(actor=request.user.get_username(),action=text[:200])

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
            service.save()
        except IntegrityError:
            form.add_error("name","This service name is already in use.")
        else:
            log_action(request,"Connected "+service.name)
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
    service.save(update_fields=["token_hash"])
    log_action(request,"Rotated token for "+service.name)
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
            log_action(request, "Updated availability target for " + service.name)
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
def configure(request,kind,pk=None,pattern=None):
    forms={"monitor":(Monitor,MonitorForm,"Uptime"),"test":(Exercise,ExerciseForm,"Benchmarks")}
    model,form_class,section=forms[kind]
    can_write(request,("change_" if pk else "add_")+model._meta.model_name)
    item=get_object_or_404(model,pk=pk) if pk else None
    form=form_class(request.POST if request.method == "POST" else None, request.FILES or None, instance=item, initial={"pattern":pattern} if pattern else None)
    if request.method=="POST" and form.is_valid():
        obj=form.save(commit=False)
        if kind=="test":
            from .benchconfig import next_schedule
            obj.next_at=next_schedule(obj, timezone.now())
        with transaction.atomic():
            obj.save()
            form.save_m2m()
            log_action(request,"Saved "+kind+" "+obj.name)
        return redirect("benchmarks" if kind=="test" else "monitors")
    title=("Edit " if pk else "New ")+("benchmark" if kind=="test" else "monitor")
    template="console/bench_form.html" if kind=="test" else "console/monitor_form.html"
    return render(request,template,{"title":title,"section":section,"form":form,"button":"Save"})

@login_required
@require_POST
def check_now(request,pk):
    can_write(request,"change_monitor")
    monitor=get_object_or_404(Monitor,pk=pk)
    if not monitor.paused:
        Monitor.objects.filter(pk=pk).update(next_at=timezone.now())
        messages.success(request,"Check queued for the worker.")
        log_action(request,"Queued check for "+monitor.name)
    return redirect("monitor",pk=pk)

@login_required
def benchmarks(request):
    return render(request,"console/benchmarks.html",{"section":"Benchmarks","tests":Exercise.objects.order_by("name"),"runs":Run.objects.select_related("exercise").defer("config","series","samples").order_by("-id")[:60]})

@login_required
@require_POST
def start(request,pk):
    can_write(request,"add_run")
    exercise=get_object_or_404(Exercise,pk=pk)
    try:
        run=enqueue(exercise)
    except ValidationError as err:
        messages.error(request,err.messages[0])
        return redirect("benchmarks")
    log_action(request,"Started "+exercise.name)
    return redirect("run",pk=run.pk)

@login_required
@require_POST
def stop(request,pk):
    can_write(request,"change_run")
    with transaction.atomic():
        item=get_object_or_404(Run,pk=pk)
        if item.state in ("Queued","Running"):
            item.stop=True
            if item.state=="Queued":
                item.state="Stopped"
                item.finished=timezone.now()
            item.save(update_fields=["stop","state","finished"])
    return redirect("run",pk=pk)

@login_required
def run_detail(request,pk):
    item=get_object_or_404(Run.objects.select_related("exercise"),pk=pk)
    other=request.GET.get("compare","")
    previous=Run.objects.filter(pk=int(other),exercise=item.exercise).exclude(pk=pk).only("pk","result").first() if other.isascii() and other.isdecimal() and len(other)<19 else None
    timeline=Paginator(item.samples,40).get_page(request.GET.get("page") or 1)
    template="console/run_metrics.html" if request.GET.get("partial") else "console/run.html"
    return render(request,template,{"section":"Benchmarks","run":item,"m":item.result,"previous":previous,
        "timeline":timeline,"history":Run.objects.filter(exercise=item.exercise,finished__isnull=False).exclude(pk=pk).only("pk","started").order_by("-pk")[:20]})

@login_required
def export(request,pk):
    from .benchconfig import PHASES
    from .benchstats import reason
    item=get_object_or_404(Run.objects.only("samples"),pk=pk)
    response=HttpResponse(content_type="text/csv",headers={"Content-Disposition":f'attachment; filename="pypulse-run-{pk}.csv"'})
    writer=csv.writer(response)
    writer.writerow(["offset_seconds","elapsed_ms","http_status","step","profile","failure",*PHASES,"bytes_sent","bytes_received"])
    def cell(text):
        # Neutralize spreadsheet formula prefixes in operator-chosen labels.
        return "'"+text if isinstance(text,str) and text.lstrip().startswith(("=","+","-","@")) else text
    for row in item.samples:
        phases=row[6] if len(row)>6 else {}
        writer.writerow([*row[:3],*map(cell,row[3:5] if len(row)>4 else ["Endpoint","Direct"]),cell(reason(row)),
            *(phases.get(k,"") for k in PHASES),*(row[7:9] or ["",""])])
    return response

@login_required
def activity(request):
    query = request.GET.get("q", "").strip()[:120]
    events = Audit.objects.order_by("-at", "-pk")
    if query:
        events = events.filter(Q(actor__icontains=query) | Q(action__icontains=query))
    rows = Paginator(events, 50).get_page(request.GET.get("page"))
    return render(request, "console/activity.html", {"section": "Activity", "rows": rows, "q": query})

def health(request):
    return JsonResponse({"status":"ok"})
