import csv
import json
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
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

def api_report(request,pk):
    if not request.user.is_authenticated:
        return JsonResponse({"error":"Sign in"},status=401)
    service=get_object_or_404(Service,pk=pk)
    return JsonResponse({"service":summary(service),**report(service,window(request))})

@login_required
def monitoring(request):
    return render(request,"console/monitors.html",{"section":"Uptime","monitors":Monitor.objects.order_by("name")})

@login_required
def monitor_detail(request,pk):
    monitor=get_object_or_404(Monitor,pk=pk)
    return render(request,"console/monitor.html",{"section":"Uptime","monitor":monitor,"checks":monitor.checks.order_by("-at")[:80]})

@login_required
def configure(request,kind,pk=None):
    forms={"monitor":(Monitor,MonitorForm,"Uptime"),"test":(Exercise,ExerciseForm,"Benchmarks")}
    model,form_class,section=forms[kind]
    can_write(request,("change_" if pk else "add_")+model._meta.model_name)
    item=get_object_or_404(model,pk=pk) if pk else None
    form=form_class(request.POST or None,instance=item)
    if request.method=="POST" and form.is_valid():
        obj=form.save(commit=False)
        if kind=="test":
            obj.next_at=timezone.now()+timedelta(minutes=obj.repeat_minutes) if obj.repeat_minutes else None
        obj.save()
        log_action(request,"Saved "+kind+" "+obj.name)
        return redirect("benchmarks" if kind=="test" else "monitors")
    title=("Edit " if pk else "New ")+("benchmark" if kind=="test" else "monitor")
    return render(request,"console/form.html",{"title":title,"section":section,"form":form,"button":"Save"})

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
def incidents(request):
    return render(request,"console/incidents.html",{"section":"Incidents","items":Incident.objects.select_related("monitor").order_by("-opened")[:100]})

@login_required
@require_POST
def acknowledge(request,pk):
    can_write(request,"change_incident")
    item=get_object_or_404(Incident,pk=pk)
    item.acknowledged=True
    item.save(update_fields=["acknowledged"])
    log_action(request,"Acknowledged incident "+str(pk))
    return redirect("incidents")

def status(request):
    monitors=Monitor.objects.filter(public=True,paused=False).order_by("name")
    return render(request,"console/status.html",{"monitors":monitors})

@login_required
def benchmarks(request):
    return render(request,"console/benchmarks.html",{"section":"Benchmarks","tests":Exercise.objects.order_by("name"),"runs":Run.objects.select_related("exercise").order_by("-id")[:60]})

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
    item=get_object_or_404(Run,pk=pk)
    other=request.GET.get("compare","")
    previous=Run.objects.filter(pk=int(other),exercise=item.exercise).exclude(pk=pk).first() if other.isdecimal() else None
    template="console/run_metrics.html" if request.GET.get("partial") else "console/run.html"
    return render(request,template,{"section":"Benchmarks","run":item,"m":item.result,"previous":previous,
        "history":Run.objects.filter(exercise=item.exercise,finished__isnull=False).exclude(pk=pk).order_by("-pk")[:20]})

@login_required
def export(request,pk):
    item=get_object_or_404(Run,pk=pk)
    response=HttpResponse(content_type="text/csv",headers={"Content-Disposition":f'attachment; filename="pypulse-run-{pk}.csv"'})
    writer=csv.writer(response)
    writer.writerow(["offset_seconds","elapsed_ms","http_status"])
    writer.writerows(item.samples)
    return response

@login_required
def activity(request):
    return render(request,"console/activity.html",{"section":"Activity","rows":Audit.objects.order_by("-at")[:100]})

def health(request):
    return JsonResponse({"status":"ok"})
