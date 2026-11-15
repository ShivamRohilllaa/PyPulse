import csv
import gzip
import io
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .audit import record
from .benchconfig import next_schedule
from .engine import enqueue
from .forms import ExerciseForm
from .lab import delete_finished, filtered_runs, preflight, remove_template
from .models import Exercise, Run
from .report_summary import explain, text_report


@login_required
@require_http_methods(["GET", "POST"])
def editor(request, pk=None, pattern=None):
    action = request.POST.get("action", "save")
    permission = "change_exercise" if pk else "add_exercise"
    if not request.user.has_perm("console." + permission):
        raise PermissionDenied
    item = get_object_or_404(Exercise, pk=pk) if pk else None
    old_name = item.name if item else ""
    form = ExerciseForm(request.POST if request.method == "POST" else None, request.FILES or None,
                        instance=item, initial={"pattern": pattern} if pattern else None)
    preview = None
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if action == "preview":
            preview = preflight(obj)
            if request.FILES:
                form.data = form.data.copy()
                form.data["csv_data"] = obj.options.get("csv_data", "")
        elif action == "once":
            if not request.user.has_perm("console.add_run"):
                raise PermissionDenied
            with transaction.atomic():
                run = enqueue(obj, actor=request.user.get_username(), saved=False)
                record(request, "Queued one-off benchmark", run, details={"name": run.name})
            return redirect("run", pk=run.pk)
        elif action in {"save", "copy"}:
            if action == "copy":
                if not request.user.has_perm("console.add_exercise"):
                    raise PermissionDenied
                obj.pk = None
                if obj.name == old_name:
                    obj.name = obj.name[:113] + " (copy)"
                obj.repeat_minutes = 0
                obj.options.pop("daily_at", None)
            obj.next_at = next_schedule(obj, timezone.now())
            with transaction.atomic():
                obj.save()
                record(request, "Copied benchmark" if action == "copy" else "Saved benchmark", obj,
                       details={"name": obj.name, "pattern": obj.pattern})
            return redirect("benchmarks")
        else:
            form.add_error(None, "Choose a benchmark action.")
    return render(request, "console/bench_form.html", {"section": "Benchmarks", "form": form, "button": "Save",
                  "title": "Edit benchmark" if pk else "New benchmark", "preview": preview, "existing": bool(pk)})


@login_required
def index(request):
    recent = Run.objects.filter(exercise_id=OuterRef("pk")).order_by("-pk")
    tests = Exercise.objects.order_by("name").annotate(last_run=Subquery(recent.values("pk")[:1]),
                last_state=Subquery(recent.values("state")[:1]))
    runs = Paginator(filtered_runs(request.GET), 40).get_page(request.GET.get("page"))
    preserved = request.GET.copy()
    preserved.pop("page", None)
    return render(request, "console/benchmarks.html", {"section":"Benchmarks", "tests":tests,
                  "runs":runs, "triggers":("manual","ci","scheduled","unknown"), "filters":request.GET, "query":preserved.urlencode()})


@login_required
@permission_required("console.add_run", raise_exception=True)
@require_http_methods(["GET", "POST"])
def start(request, pk):
    exercise = get_object_or_404(Exercise, pk=pk)
    try:
        if request.method == "GET":
            return render(request, "console/launch.html", {"section":"Benchmarks", "exercise":exercise,
                          "preview":preflight(exercise)})
        with transaction.atomic():
            run = enqueue(exercise, actor=request.user.get_username())
            record(request, "Queued benchmark", run, details={"test_id":pk, "name":exercise.name})
    except ValidationError as err:
        messages.error(request, err.messages[0])
        return redirect("benchmarks")
    return redirect("run", pk=run.pk)


@login_required
@require_POST
def remove(request, pk, kind):
    if not request.user.has_perm("console.delete_" + kind):
        raise PermissionDenied
    if kind == "exercise":
        try:
            remove_template(pk, request)
        except Exercise.DoesNotExist:
            raise Http404 from None
    else:
        get_object_or_404(Run.objects.only("pk"), pk=pk)
        try:
            delete_finished(pk, request)
        except ValidationError as error:
            messages.error(request, error.messages[0])
            return redirect("run", pk=pk)
    return redirect("benchmarks")


@login_required
def summary_text(request, pk):
    run = get_object_or_404(Run.objects.select_related("exercise").defer("samples","series"), pk=pk)
    summary = explain(run)
    if summary is None:
        return HttpResponse("This run is still in progress.", status=409, content_type="text/plain")
    return HttpResponse(text_report(run, summary), content_type="text/plain; charset=utf-8")


def write_sample_rows(run, stream):
    from .benchconfig import PHASES
    from .benchstats import reason

    def cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    writer = csv.writer(stream)
    writer.writerow(["offset_seconds","elapsed_ms","http_status","step","profile","failure",*PHASES,
                     "bytes_sent","bytes_received","stage","iteration","connection_reused","compressed","answered"])
    for row in run.samples:
        # Old measurements store only offset, duration and status. Export just the public sample fields.
        phases = row[6] if len(row) > 6 and isinstance(row[6], dict) else {}
        meta = row[9] if len(row) > 9 and isinstance(row[9], dict) else {}
        writer.writerow([*row[:3], *map(cell, row[3:5] if len(row) > 4 else ("Endpoint","Direct")),
                         cell(reason(row)), *(phases.get(k, "") for k in PHASES),
                         row[7] if len(row) > 7 else "", row[8] if len(row) > 8 else "",
                         *(meta.get(k, "") for k in ("stage","iteration","reused","compressed","answered"))])


@require_GET
@never_cache
def raw_csv(request, pk):
    from .workspace_ci import authenticate
    if request.headers.get("Authorization"):
        authorized = authenticate(request) is not None
    else:
        authorized = request.user.is_authenticated
    if not authorized:
        return JsonResponse({"error":"Sign in or supply a CI token."}, status=401)
    run = Run.objects.only("samples").filter(pk=pk).first()
    if run is None:
        return JsonResponse({"error":"Run not found."}, status=404)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as zipped:
        with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as stream:
            write_sample_rows(run, stream)
    return HttpResponse(out.getvalue(), content_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="pypulse-run-{pk}.csv.gz"'})
