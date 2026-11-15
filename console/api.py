import json
import os
import secrets
from functools import wraps
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from .engine import enqueue
from .forms import ServiceForm
from .models import Audit, Exercise, Run, Service
from .telemetry import digest, issue_token, summary


def account(permission=None):
    def wrap(fn):
        @wraps(fn)
        def endpoint(request,*args,**kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error":"Sign in required"},status=401)
            if permission and not request.user.has_perm("console."+permission):
                return JsonResponse({"error":"Permission required"},status=403)
            return fn(request,*args,**kwargs)
        return endpoint
    return wrap

@never_cache
@account()
@require_http_methods(["GET","POST"])
def services(request):
    if request.method=="GET":
        return JsonResponse([summary(s) for s in Service.objects.order_by("name")],safe=False)
    if not request.user.has_perm("console.add_service"):
        return JsonResponse({"error":"Permission required"},status=403)
    try:
        form=ServiceForm(json.loads(request.body))
    except (ValueError,TypeError):
        return JsonResponse({"error":"Invalid JSON"},status=400)
    if not form.is_valid():
        return JsonResponse({"error":form.errors},status=400)
    token=issue_token()
    service=form.save(commit=False)
    service.token_hash=digest(token)
    service.save()
    Audit.objects.create(actor=request.user.get_username(),action="Connected "+service.name)
    return JsonResponse({"service":summary(service),"token":token},status=201)

@account("change_service")
@require_http_methods(["PUT"])
def goal(request,pk):
    item=get_object_or_404(Service,pk=pk)
    try:
        target=float(json.loads(request.body)["target"])
        if not 1<=target<=99.999: raise ValueError
    except (ValueError,KeyError,TypeError):
        return JsonResponse({"error":"Use a target between 1 and 99.999"},status=400)
    item.target=target
    item.save(update_fields=["target"])
    return JsonResponse(summary(item))

@never_cache
@account("change_service")
@require_POST
def rotate(request,pk):
    item=get_object_or_404(Service,pk=pk)
    token=issue_token()
    item.token_hash=digest(token)
    item.save(update_fields=["token_hash"])
    return JsonResponse({"token":token,"service":summary(item)})

@account("delete_service")
@require_http_methods(["DELETE"])
def remove(request,pk):
    get_object_or_404(Service,pk=pk).delete()
    return JsonResponse({"deleted":True})

@csrf_exempt
@require_http_methods(["GET","POST"])
def ci(request,pk=None):
    key=os.environ.get("PYPULSE_CI_TOKEN")
    if not key or not secrets.compare_digest(request.headers.get("Authorization","").encode(),("Bearer "+key).encode()):
        return JsonResponse({"error":"CI token required"},status=401)
    if request.method=="POST":
        exercise=get_object_or_404(Exercise,pk=pk)
        from django.core.exceptions import ValidationError
        try: run=enqueue(exercise)
        except ValidationError as e: return JsonResponse({"error":e.messages},status=400)
        Audit.objects.create(actor="CI",action="Started "+exercise.name)
        return JsonResponse({"id":run.pk,"report":f"/api/ci/runs/{run.pk}"},status=202)
    run=get_object_or_404(Run,pk=pk)
    return JsonResponse({"id":run.pk,"state":run.state,"result":run.result})
