"""Record actions using explicit metadata. Never serialize requests, model fields or credentials."""
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Audit


def record(actor_or_request, action, obj=None, *, details=None):
    actor = getattr(actor_or_request, "user", actor_or_request)
    username = actor.get_username() if hasattr(actor, "get_username") else str(actor)
    return Audit.objects.create(
        actor=username[:150], action=action[:200],
        object_type=obj._meta.model_name if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        details=details or {},
    )

def activity_page(params, page_size=50):
    query = params.get("q", "").strip()[:120]
    rows = Audit.objects.order_by("-at", "-pk")
    if params.get("entityType"):
        rows = rows.filter(object_type=params["entityType"][:60])
    if params.get("before"):
        before = params["before"]
        if not before.isascii() or not before.isdecimal() or len(before) > 18:
            rows = rows.none()
        else:
            rows = rows.filter(pk__lt=int(before))
    if query:
        rows = rows.filter(Q(actor__icontains=query) | Q(action__icontains=query) | Q(object_type__icontains=query)
                           | Q(object_id=query) | Q(details__icontains=query))
    return Paginator(rows, page_size).get_page(params.get("page"))


@never_cache
@require_GET
def audit_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Sign in required."}, status=401)
    if not request.user.has_perm("console.view_audit"):
        return JsonResponse({"error": "Audit permission required."}, status=403)
    try:
        limit = min(500, max(1, int(request.GET.get("limit", 50))))
    except ValueError:
        return JsonResponse({"error": "Invalid limit."}, status=400)
    page = activity_page(request.GET, limit)
    fields = ("id", "at", "actor", "action", "object_type", "object_id", "details")
    return JsonResponse(list(page.object_list.values(*fields)), safe=False)
