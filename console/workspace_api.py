"""Session-authenticated JSON endpoints for workspace administration and developer tools."""
import os
import platform
import time
from importlib.metadata import PackageNotFoundError, version

import django
import psutil
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .audit import record
from .models import ApiToken, Monitor, NetworkProfile, Run, Variable, WorkspaceSettings
from .resources import NetworkForm, VariableForm
from .workspace_auth import User, json_data, json_view, profile, signed_in
from .workspace_ci import issue_token
from .workspace_config import SECTIONS, encode, export_config, import_config, import_settings, parse_file
from .workspace_roles import apply_role, role_of, update_access
from .workspace_views import MemberForm, ROLES, can_export


def error(text, status):
    return JsonResponse({"error": text}, status=status)


@json_view
@signed_in
@require_http_methods(["GET", "POST", "PUT", "DELETE"])
@transaction.atomic
def members(request, pk=None):
    if not request.user.is_superuser:
        return error("Administrator role required.", 403)
    if pk is None:
        if request.method == "GET":
            return JsonResponse([profile(u) for u in User.objects.prefetch_related("groups").order_by("username")], safe=False)
        if request.method != "POST":
            return error("Use GET or POST.", 405)
        data = json_data(request)
        form = MemberForm({"username": data.get("username") or data.get("email"), "email": data.get("email", ""),
                           "first_name": data.get("name", ""), "password1": data.get("password"),
                           "password2": data.get("password"), "role": data.get("role", "viewer")})
        if not form.is_valid():
            return error(form.errors, 400)
        user = form.save(commit=False)
        apply_role(user, form.cleaned_data["role"], True)
        record(request, "Created workspace member", user, details={"role": form.cleaned_data["role"]})
        return JsonResponse(profile(user), status=201)
    user = User.objects.filter(pk=pk).first()
    if not user:
        return error("Member not found.", 404)
    if request.method == "GET":
        return JsonResponse(profile(user))
    if request.method == "DELETE":
        if user.pk == request.user.pk:
            return error("You cannot delete your own account.", 400)
        record(request, "Deleted workspace member", user)
        user.delete()
        return JsonResponse({"ok": True})
    if request.method != "PUT":
        return error("Use GET, PUT or DELETE.", 405)
    data = json_data(request)
    role = data.get("role", role_of(user))
    disabled = data.get("disabled", not user.is_active)
    name = data.get("name", user.first_name)
    if not isinstance(role, str) or role not in dict(ROLES) or type(disabled) is not bool:
        raise ValueError("Supply a valid role and boolean disabled flag.")
    if not isinstance(name, str) or len(name) > 150:
        raise ValueError("Display name is limited to 150 characters.")
    if "password" in data:
        password = data["password"]
        if not isinstance(password, str) or len(password) < 12:
            raise ValueError("Use a password of at least 12 characters.")
        user.set_password(password)
    user.first_name = name
    update_access(request.user, user, role, not disabled)
    record(request, "Updated member access", user, details={"role":role, "active":user.is_active, "password_reset":"password" in data})
    return JsonResponse(profile(user))


def token_info(token):
    return {"id": token.pk, "name": token.name, "prefix": token.prefix, "scope": token.scope,
            "createdAt": token.created, "lastUsedAt": token.last_used, "revokedAt": token.revoked}


@json_view
@signed_in
@require_http_methods(["GET", "POST", "DELETE"])
@transaction.atomic
def tokens(request, pk=None):
    if request.headers.get("Authorization"):
        return error("Use your signed-in account to manage CI credentials.", 403)
    own = ApiToken.objects.filter(owner=request.user)
    if pk is not None:
        if request.method != "DELETE":
            return error("Use DELETE to revoke this token.", 405)
        token = own.filter(pk=pk).first()
        if not token:
            return error("Token not found.", 404)
        if own.filter(pk=pk, revoked=None).update(revoked=timezone.now()):
            record(request, "Revoked CI token", token)
        return JsonResponse({"ok": True})
    if request.method == "GET":
        return JsonResponse([token_info(t) for t in own.order_by("-created")], safe=False)
    if request.method != "POST":
        return error("Use GET or POST.", 405)
    data = json_data(request)
    may_run = request.user.has_perm("console.add_run")
    name, scope = data.get("name"), data.get("scope", "run" if may_run else "read")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or scope not in ("read", "run"):
        raise ValueError("Supply a token name and choose read or run scope.")
    if scope == "run" and not may_run:
        return error("Run permission required.", 403)
    token, raw = issue_token(request.user, name.strip(), scope)
    record(request, "Issued CI token", token, details={"scope": scope})
    return JsonResponse({**token_info(token), "token": raw}, status=201)


SETTINGS_KEYS = {
    "defaults": {"intervalSec": "interval", "timeoutMs": "timeout_ms", "failureThreshold": "failure_threshold", "degradedMs": "warn_ms"},
    "retention": {"rawDays":"raw_days", "rollupDays":"rollup_days", "runDays":"run_days", "telemetryDays":"telemetry_days"},
    "statusPage": {"enabled":"status_enabled", "title":"status_title"},
}


@json_view
@signed_in
@require_http_methods(["GET", "PUT"])
@transaction.atomic
def settings_api(request):
    if request.method == "PUT":
        if not request.user.is_superuser:
            return error("Administrator role required.", 403)
        body = json_data(request)
        if set(body) - {*SETTINGS_KEYS, "loadAllowlist"}:
            raise ValueError("Unknown settings section.")
        fields = {}
        for group, mapping in SETTINGS_KEYS.items():
            part = body.get(group, {})
            if not isinstance(part, dict) or set(part) - set(mapping):
                raise ValueError(f"Invalid {group} settings.")
            fields.update((mapping[k], v) for k, v in part.items())
        if "loadAllowlist" in body:
            hosts = body["loadAllowlist"]
            if not isinstance(hosts, list) or any(not isinstance(h, str) for h in hosts):
                raise ValueError("loadAllowlist must be an array of host names.")
            fields["allowed_hosts"] = "\n".join(hosts)
        import_settings(fields)
        record(request, "Updated workspace defaults", WorkspaceSettings.current())
    item = WorkspaceSettings.current()
    response = {group: {api: getattr(item, db) for api, db in mapping.items()} for group, mapping in SETTINGS_KEYS.items()}
    return JsonResponse({**response, "loadAllowlist": item.allowed_hosts.splitlines()})


@json_view
@signed_in
@require_GET
def export_api(request):
    clear = request.GET.get("secrets") == "true"
    if not can_export(request.user) or (clear and not request.user.is_superuser):
        return error("Export permission required.", 403)
    fmt = request.GET.get("format", "yaml")
    sections = request.GET.getlist("section") or SECTIONS
    doc = encode(export_config(sections, clear), fmt)
    record(request, "Exported workspace configuration", details={"secrets_included": clear})
    reply = HttpResponse(doc, content_type="application/json" if fmt == "json" else "application/yaml")
    reply["Content-Disposition"] = f'attachment; filename="pypulse-workspace.{fmt}"'
    return reply


@json_view
@signed_in
@require_POST
@transaction.atomic
def import_api(request):
    if not request.user.is_superuser:
        return error("Administrator role required.", 403)
    mode = request.GET.get("mode", "merge")
    report = import_config(parse_file(request.body), mode, request.GET.getlist("section") or SECTIONS)
    record(request, "Imported workspace configuration", details={"mode": mode, "counts": report})
    return JsonResponse({"counts": report})


PROFILE_KEYS = {"name":"name", "description":"description", "downloadKbps":"download_kbps", "uploadKbps":"upload_kbps",
                "latencyMs":"latency_ms", "jitterMs":"jitter_ms", "packetLoss":"loss_pct"}
VARIABLE_KEYS = {"name":"name", "description":"description", "environment":"environment", "isSecret":"secret"}


def resource_info(kind, obj):
    keys = PROFILE_KEYS if kind == "profiles" else VARIABLE_KEYS
    info = {k: getattr(obj, v) for k, v in keys.items()}
    if kind == "profiles":
        info["builtin"] = obj.preset
    else:
        info["value"] = "••••••••" if obj.secret else obj.reveal()
    return {**info, "id": obj.pk}


@json_view
@signed_in
@require_http_methods(["GET", "POST", "PUT", "DELETE"])
@transaction.atomic
def resource(request, kind, pk=None):
    model, form_type, mapping = ((NetworkProfile, NetworkForm, PROFILE_KEYS) if kind == "profiles" else
                                 (Variable, VariableForm, VARIABLE_KEYS))
    obj = model.objects.filter(pk=pk).first() if pk is not None else None
    if pk is not None and obj is None:
        return error("Resource not found.", 404)
    if request.method == "GET":
        if obj:
            return JsonResponse(resource_info(kind, obj))
        return JsonResponse([resource_info(kind, row) for row in model.objects.order_by("name", "pk")], safe=False)
    if (request.method == "POST") != (obj is None):
        return error("Use POST to create, PUT to edit, or DELETE to remove.", 405)
    action = {"POST": "add", "PUT":"change", "DELETE":"delete"}[request.method]
    if not request.user.has_perm(f"console.{action}_{model._meta.model_name}") or getattr(obj, "preset", False):
        return error("Permission required. Built-in profiles are read-only.", 403)
    if request.method == "DELETE":
        from .resources import remove_resource
        remove_resource(request, obj)
        return JsonResponse({"ok": True})
    body = json_data(request)
    if set(body) - set(mapping) - ({"value"} if kind == "variables" else set()):
        raise ValueError("Unknown resource field.")
    if "isSecret" in body and type(body["isSecret"]) is not bool:
        raise ValueError("isSecret must be true or false.")
    defaults = obj or model()
    data = {model_key: body.get(api_key, getattr(defaults, model_key)) for api_key, model_key in mapping.items()}
    if kind == "variables":
        value = body.get("value", "")
        if not isinstance(value, str):
            raise ValueError("Variable value must be text.")
        data["value"] = "" if obj and obj.secret and value == "••••••••" else value
    form = form_type(data, instance=obj)
    if not form.is_valid():
        return error(form.errors, 400)
    saved = form.save()
    record(request, "Saved workspace resource", saved)
    return JsonResponse(resource_info(kind, saved), status=201 if request.method == "POST" else 200)


def system_info(user):
    process = psutil.Process()
    from .models import Exercise
    payload = {"python": platform.python_version(), "django": django.get_version(),
               "uptimeSec": max(0, round(time.time() - process.create_time())), "cpuCount": os.cpu_count(),
               "cpuPercent": psutil.cpu_percent(interval=.1), "memoryMb": round(process.memory_info().rss / 1048576),
               "monitors": Monitor.objects.count(), "scheduled": Monitor.objects.filter(paused=False).count(),
               "scheduledTests": Exercise.objects.filter(next_at__isnull=False).count(),
               "activeRuns": Run.objects.filter(state="Running").count(), "queuedRuns": Run.objects.filter(state="Queued").count()}
    try:
        payload["agentVersion"] = version("pypulse-agent")
    except PackageNotFoundError:
        pass
    if user.is_superuser:
        from django.conf import settings
        payload["dataDir"] = str(settings.DATA)
    return payload


@json_view
@signed_in
@require_GET
def system_api(request):
    return JsonResponse(system_info(request.user))


@login_required
@require_GET
def system_page(request):
    return render(request, "console/workspace_system.html", {"section":"Workspace", "system":system_info(request.user)})
