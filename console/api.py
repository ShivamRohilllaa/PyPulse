import json
from functools import wraps
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from .workspace_ci import ci
from .forms import ServiceForm
from .audit import record
from .models import Service
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
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
        form = ServiceForm(data)
    except (ValueError,TypeError):
        return JsonResponse({"error":"Invalid JSON"},status=400)
    if not form.is_valid():
        return JsonResponse({"error":form.errors},status=400)
    token=issue_token()
    service=form.save(commit=False)
    service.token_hash=digest(token)
    try:
        with transaction.atomic():
            service.save()
            record(request, "Connected Python service", service, details={"name":service.name, "framework":service.framework})
    except IntegrityError:
        return JsonResponse({"error":"A service with that name already exists."}, status=409)
    return JsonResponse({"service":summary(service),"token":token},status=201)

@account("change_service")
@require_http_methods(["PUT"])
def goal(request,pk):
    item=get_object_or_404(Service,pk=pk)
    try:
        raw = json.loads(request.body)["target"]
        if isinstance(raw, bool):
            raise ValueError
        target = float(raw)
        if not 1<=target<=99.999: raise ValueError
    except (ValueError,KeyError,TypeError):
        return JsonResponse({"error":"Use a target between 1 and 99.999"},status=400)
    item.target=target
    with transaction.atomic():
        item.save(update_fields=["target"])
        record(request, "Updated service availability target", item, details={"target": target})
    return JsonResponse(summary(item))

@never_cache
@account("change_service")
@require_POST
def rotate(request,pk):
    item=get_object_or_404(Service,pk=pk)
    token=issue_token()
    item.token_hash=digest(token)
    with transaction.atomic():
        item.save(update_fields=["token_hash"])
        record(request, "Rotated service ingest token", item)
    return JsonResponse({"token":token,"service":summary(item)})

@account("delete_service")
@require_http_methods(["DELETE"])
def remove(request,pk):
    with transaction.atomic():
        item = get_object_or_404(Service, pk=pk)
        record(request, "Removed Python service", item, details={"name": item.name})
        item.delete()
    return JsonResponse({"deleted":True})
