"""Benchmark history, template operations, and preflight estimates."""
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .audit import record
from .benchconfig import validate_budget, validate_options
from .benchstats import stage_plan
from .models import Exercise, Run
from .probes import interpolate, workspace_variables


def preflight(exercise):
    opts = validate_options(exercise.options or {}, exercise.url)
    validate_budget(opts, exercise.pattern, exercise.rate, exercise.seconds)
    steps = opts.get("steps") or [{"url": exercise.url}]
    variables = workspace_variables(opts.get("environment", ""))
    hosts = set()
    for step in steps:
        url = step.get("url") or exercise.url
        if url.startswith("{{"):
            pos = url.find("}}") + 2
            url = interpolate(url[:pos], variables) + url[pos:]
        authority = interpolate(urlsplit(url).netloc, variables)
        hosts.add(authority)
    rounds = max(1, len(opts.get("profiles", [])))
    step_count = len(steps)
    seconds = exercise.seconds
    if exercise.pattern == "latency":
        count = (opts.get("count") or exercise.seconds) * rounds * step_count
    else:
        cfg = {"seconds": exercise.seconds, "pattern": exercise.pattern, "rate": exercise.rate}
        stages = stage_plan(cfg, opts)
        seconds = sum(stage["seconds"] for stage in stages)
        count = (sum(stage["target"] * stage["seconds"] for stage in stages) * rounds * step_count
                 if opts["model"] == "arrival" else None)
    return {"hosts": sorted(hosts), "profiles": rounds, "steps": step_count, "estimated_requests": count,
            "model": opts["model"], "seconds_per_profile": seconds, "pattern": exercise.pattern}


def filtered_runs(params):
    rows = Run.objects.select_related("exercise").defer("samples", "series", "config")
    pattern = params.get("kind")
    if pattern == "latency":
        rows = rows.filter(config__pattern="latency")
    elif pattern == "load":
        rows = rows.exclude(config__pattern="latency")
    if params.get("status"):
        rows = rows.filter(state__iexact=params["status"][:24])
    template = params.get("templateId") or params.get("template")
    if template:
        if not template.isascii() or not template.isdecimal() or len(template) > 18:
            return rows.none()
        rows = rows.filter(exercise_id=int(template))
    if params.get("trigger"):
        rows = rows.filter(trigger=params["trigger"][:12])
    if params.get("q"):
        term = params["q"].strip()[:120]
        rows = rows.filter(Q(name__icontains=term) | Q(name="", exercise__name__icontains=term))
    return rows.order_by("-pk")


@transaction.atomic
def delete_finished(run_id, actor):
    run = Run.objects.select_for_update().get(pk=run_id)
    if run.finished is None:
        raise ValidationError("Stop the run and wait for it to finish before deleting it.")
    record(actor, "Deleted benchmark report", run, details={"name": run.display_name})
    run.delete()


@transaction.atomic
def remove_template(test_id, actor):
    test = Exercise.objects.select_for_update().get(pk=test_id)
    Run.objects.filter(exercise=test, name="").update(name=test.name)
    record(actor, "Removed saved benchmark", test, details={"name": test.name})
    test.delete()


@transaction.atomic
def stop_run(run_id, actor):
    run = Run.objects.select_for_update().only("state", "stop", "finished").get(pk=run_id)
    if run.finished is not None or run.state not in ("Queued", "Running"):
        return run
    if run.state == "Running" and run.stop:
        return run
    run.stop = True
    fields = ["stop"]
    if run.state == "Queued":
        run.state = "Stopped"
        run.finished = timezone.now()
        fields.extend(("state", "finished"))
    run.save(update_fields=fields)
    record(actor, "Requested benchmark stop", run)
    return run
