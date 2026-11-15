"""Bearer authentication and bounded waits for a CI performance gate."""
import hashlib
import hmac
import os
import secrets
import time

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Audit, Exercise, Run


def issue_token(owner, name, scope):
    raw = "ppci_" + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(owner=owner, name=name, prefix=raw[:12],
                                    digest=hashlib.sha256(raw.encode()).hexdigest(), scope=scope)
    return token, raw


def authenticate(request):
    prefix, _, raw = request.headers.get("Authorization", "").partition(" ")
    if prefix.lower() != "bearer" or not raw or len(raw) > 4096:
        return None
    legacy = os.environ.get("PYPULSE_CI_TOKEN", "")
    if legacy and hmac.compare_digest(raw.encode(), legacy.encode()):
        return "CI", True
    digest = hashlib.sha256(raw.encode()).hexdigest()
    token = ApiToken.objects.select_related("owner").filter(digest=digest, revoked=None, owner__is_active=True).first()
    if token is None:
        return None
    if not ApiToken.objects.filter(pk=token.pk, revoked=None, owner__is_active=True).update(last_used=timezone.now()):
        return None
    return token.owner.get_username(), token.scope == "run" and token.owner.has_perm("console.add_run")


def response(run, *, polled=False):
    result = run.result or {}
    finished = run.finished is not None
    report = f"/api/ci/runs/{run.pk}"
    payload = {"id": run.pk, "state": run.state, "result": result, "report": report,
               "finished": finished, "passed": bool(result.get("passed")) if finished else None,
               "dashboard": reverse("run", args=[run.pk])}
    reply = JsonResponse(payload, status=200 if finished or polled else 202)
    reply["Location"] = report
    if not finished:
        reply["Retry-After"] = "2"
    return reply


@csrf_exempt
@never_cache
@require_http_methods(["POST", "GET"])
def ci(request, pk):
    authenticated = authenticate(request)
    if authenticated is None:
        return JsonResponse({"error": "CI token required"}, status=401)
    if request.method == "GET":
        run = Run.objects.filter(pk=pk).first()
        return response(run, polled=True) if run else JsonResponse({"error": "Run not found"}, status=404)
    actor, may_run = authenticated
    if not may_run:
        return JsonResponse({"error": "Run scope and permission required"}, status=403)
    try:
        wait = int(request.GET.get("wait", "0"))
        if not 0 <= wait <= 20:
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "wait must be an integer from 0 to 20 seconds"}, status=400)
    exercise = Exercise.objects.filter(pk=pk).first()
    if exercise is None:
        return JsonResponse({"error": "Test not found"}, status=404)
    from .engine import enqueue
    try:
        run = enqueue(exercise)
    except ValidationError as error:
        return JsonResponse({"error": error.messages}, status=400)
    Audit.objects.create(actor=actor, action=f"CI started test {exercise.pk}, run {run.pk}")
    deadline = time.monotonic() + wait
    while wait and run.finished is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(.25, remaining))
        run.refresh_from_db()
    return response(run)
