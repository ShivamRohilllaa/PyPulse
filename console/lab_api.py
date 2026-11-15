"""JSON control surface for saved benchmarks and measured runs."""
import json

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from . import workspace_ci
from .audit import record
from .benchconfig import KEYS, next_schedule
from .engine import enqueue
from .forms import ExerciseForm
from .lab import delete_finished, filtered_runs, preflight, remove_template, stop_run
from .models import Exercise, Run
from .report_summary import explain


def fail(message, status=400):
    return JsonResponse({"error": message}, status=status)


def form_from_json(request, instance=None):
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError("Supply a JSON object.")
    if not isinstance(body, dict):
        raise ValidationError("Supply a JSON object.")
    allowed = {"name", "url", "pattern", "rate", "seconds", "max_p95", "max_error_pct", "repeat_minutes", "options", "confirm"}
    opts = body.get("options", {})
    if not set(body) <= allowed or not isinstance(opts, dict) or not set(opts) <= KEYS:
        raise ValidationError("Unknown benchmark field. Use the documented PyPulse JSON schema.")
    defaults = dict(pattern="fixed", seconds=15, rate=5, max_p95=0, max_error_pct=0, repeat_minutes=0)
    if instance:
        defaults.update({field: getattr(instance, field) for field in
                         ("name","url","pattern","seconds","rate","max_p95","max_error_pct","repeat_minutes")})
        opts = {**instance.options, **opts}
    data = {**defaults, **{k:v for k,v in body.items() if k not in {"options","confirm"}}, **opts}
    if isinstance(data.get("keep_alive"), bool):
        data["keep_alive"] = "yes" if data["keep_alive"] else "no"
    if isinstance(data.get("statuses"), list):
        data["statuses"] = ",".join(map(str, data["statuses"]))
    form = ExerciseForm(data, instance=instance)
    if not form.is_valid():
        # Some Django field errors quote invalid input. Return field names only.
        raise ValidationError("Review benchmark fields: " + ", ".join(form.errors))
    return form.save(commit=False), body.get("confirm") is True


def identity(request, permission=None):
    if not request.user.is_authenticated:
        return fail("Sign in required.", 401)
    if permission and not request.user.has_perm("console." + permission):
        return fail("Permission required.", 403)
    return None


def template_info(test):
    return {"id":test.pk, "name":test.name, "pattern":test.pattern, "rate":test.rate, "seconds":test.seconds,
            "max_p95":test.max_p95, "max_error_pct":test.max_error_pct, "repeat_minutes":test.repeat_minutes,
            "next_at":test.next_at, "step_count":max(1,len(test.options.get("steps",[]))),
            "profiles":test.options.get("profiles",[])}


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST", "PUT", "DELETE"])
def templates(request, pk=None):
    permission = {"POST":"add_exercise","PUT":"change_exercise","DELETE":"delete_exercise"}.get(request.method)
    denied = identity(request, permission)
    if denied is not None: return denied
    if (request.method == "POST" and pk is not None) or (request.method in {"PUT","DELETE"} and pk is None):
        return fail("Method unavailable at this URL.", 405)
    if request.method == "DELETE":
        try:
            remove_template(pk, request)
        except Exercise.DoesNotExist:
            return fail("Saved test not found.", 404)
        return JsonResponse({"deleted":True})
    test = Exercise.objects.filter(pk=pk).first() if pk else None
    if pk and not test:
        return fail("Saved test not found.", 404)
    if request.method == "GET":
        if test: return JsonResponse(template_info(test))
        return JsonResponse([template_info(t) for t in Exercise.objects.order_by("name")], safe=False)
    try:
        obj, _ = form_from_json(request, test)
        obj.next_at = next_schedule(obj, timezone.now())
        with transaction.atomic():
            obj.save()
            record(request,"Saved benchmark",obj,details={"name":obj.name,"pattern":obj.pattern})
    except ValidationError as error:
        return fail(error.messages)
    return JsonResponse(template_info(obj), status=201 if request.method == "POST" else 200)


@never_cache
@csrf_protect
@require_http_methods(["POST"])
def adhoc(request, preview=False):
    denied = identity(request,"add_run")
    if denied is not None: return denied
    try:
        exercise, confirmed = form_from_json(request)
        if preview:
            return JsonResponse(preflight(exercise))
        if not confirmed:
            return fail("Set confirm to true to start an approved workload.", 428)
        with transaction.atomic():
            run = enqueue(exercise, trigger="manual", actor=request.user.get_username(), saved=False)
            record(request,"Queued one-off benchmark",run,details={"name":run.name})
    except ValidationError as error:
        return fail(error.messages)
    return workspace_ci.response(run)


def run_info(run):
    return {"id":run.pk,"template_id":run.exercise_id,"name":run.display_name,"state":run.state,
            "trigger":run.trigger,"actor":run.actor,"started":run.started,"finished":run.finished,
            "passed":run.result.get("passed"),"report":f"/runs/{run.pk}"}


@never_cache
@csrf_protect
@require_http_methods(["GET","DELETE"])
def runs(request, pk=None, active=False):
    # Scoped bearer tokens may read one result, as on the CI polling endpoint.
    if pk and request.method == "GET" and request.headers.get("Authorization"):
        return workspace_ci.ci(request, pk)
    denied = identity(request,"delete_run" if request.method == "DELETE" else None)
    if denied is not None: return denied
    if pk is None:
        if request.method != "GET": return fail("Select a report ID.", 405)
        queryset = filtered_runs(request.GET)
        if active:
            queryset = queryset.filter(finished__isnull=True)
        try:
            limit = min(500, max(1, int(request.GET.get("limit", 100))))
        except ValueError:
            return fail("Invalid limit.")
        page = Paginator(queryset, limit).get_page(request.GET.get("page"))
        return JsonResponse([run_info(item) for item in page], safe=False)
    run = Run.objects.select_related("exercise").defer("samples").filter(pk=pk).first()
    if run is None:
        return fail("Run not found.", 404)
    if request.method == "DELETE":
        try:
            delete_finished(run.pk,request)
        except ValidationError as error:
            return fail(error.messages, 409)
        return JsonResponse({"deleted":True})
    return JsonResponse({**run_info(run), "result":run.result,"series":run.series,"summary":explain(run)})


@never_cache
@csrf_protect
@require_http_methods(["POST"])
def stop(request, pk):
    if request.headers.get("Authorization"):
        return fail("Stopping runs requires a signed-in operator session.", 403)
    denied = identity(request, "change_run")
    if denied is not None:
        return denied
    try:
        run = stop_run(pk, request)
    except Run.DoesNotExist:
        return fail("Run not found.", 404)
    return JsonResponse({"id":run.pk, "state":run.state, "stop_requested":run.stop, "finished":run.finished is not None})
