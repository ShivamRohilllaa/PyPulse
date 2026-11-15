from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .audit import record
from .models import ApiToken, WorkspaceSettings
from .workspace_ci import issue_token
from .workspace_config import ConfigError, LIMIT, SECTIONS, encode, export_config, import_config, parse_file
from .workspace_roles import apply_role, role_of, update_access
from .workspace_settings import SettingsForm

User = get_user_model()
ROLES = (("viewer", "Viewer"), ("editor", "Editor"), ("admin", "Administrator"))


def admin_only(fn):
    @login_required
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return fn(request, *args, **kwargs)
    return wrapped


def show(request, page, **context):
    return render(request, "console/workspace_" + page + ".html", {"section": "Workspace", **context})


@login_required
@require_GET
def workspace(request):
    return show(request, "home", may_export=can_export(request.user))


@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request):
    if not request.user.is_superuser and request.method == "POST":
        raise PermissionDenied
    form = SettingsForm(request.POST if request.method == "POST" else None, instance=WorkspaceSettings.current())
    if not request.user.is_superuser:
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            record(request, "Updated workspace defaults", form.save())
        messages.success(request, "Workspace settings saved.")
        return redirect("workspace-settings")
    return show(request, "settings", form=form)


class MemberForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        fields = ("username", "email", "first_name")
        labels = {"first_name": "Display name"}

    role = forms.ChoiceField(choices=ROLES)
    password1 = forms.CharField(label="Password (at least 12 characters)", min_length=12,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))


@admin_only
@require_http_methods(["GET", "POST"])
def members(request):
    form = MemberForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            apply_role(user, form.cleaned_data["role"], True)
            record(request, "Created workspace member", user, details={"role": form.cleaned_data["role"]})
        messages.success(request, "Member created.")
        return redirect("workspace-members")
    users = [(member, role_of(member)) for member in User.objects.order_by("username").prefetch_related("groups")]
    return show(request, "members", form=form, members=users, roles=ROLES)


@admin_only
@require_POST
@transaction.atomic
def update_member(request, pk):
    member = get_object_or_404(User.objects.select_for_update(), pk=pk)
    role = request.POST.get("role")
    if role not in dict(ROLES):
        return HttpResponse("Choose a valid role.", status=400)
    try:
        update_access(request.user, member, role, "active" in request.POST)
    except ValueError as error:
        return HttpResponse(str(error), status=400)
    record(request, "Updated member access", member, details={"role": role, "active": member.is_active})
    return redirect("workspace-members")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def tokens(request):
    may_run = request.user.has_perm("console.add_run")
    token_value = error = None
    if request.method == "POST":
        name, scope = request.POST.get("name", "").strip(), request.POST.get("scope", "")
        if not name or len(name) > 100 or scope not in ("read", "run"):
            error = "Enter a name of up to 100 characters and a valid scope."
        elif scope == "run" and not may_run:
            raise PermissionDenied
        else:
            with transaction.atomic():
                token, token_value = issue_token(request.user, name, scope)
                record(request, "Issued CI token", token, details={"scope": scope})
    return show(request, "tokens", tokens=ApiToken.objects.filter(owner=request.user).order_by("-created"),
                value=token_value, error=error, may_run=may_run)


@login_required
@require_POST
@transaction.atomic
def revoke(request, pk):
    token = get_object_or_404(ApiToken, pk=pk, owner=request.user)
    if ApiToken.objects.filter(pk=pk, revoked=None).update(revoked=timezone.now()):
        record(request, "Revoked CI token", token)
    return redirect("workspace-tokens")


@login_required
@require_GET
def config(request):
    if not can_export(request.user):
        raise PermissionDenied
    return show(request, "config", sections=SECTIONS)


def can_export(user):
    return user.is_superuser or role_of(user) == "editor"


@login_required
@never_cache
@require_POST
def export(request):
    kind = request.POST.get("format", "json")
    secrets = request.POST.get("secrets") == "on"
    if not can_export(request.user) or (secrets and not request.user.is_superuser):
        raise PermissionDenied
    try:
        data = encode(export_config(request.POST.getlist("sections"), secrets), kind)
    except ConfigError as error:
        reply = show(request, "config", sections=SECTIONS, error=str(error))
        reply.status_code = 400
        return reply
    record(request, "Exported workspace configuration", details={"secrets_included": secrets})
    reply = HttpResponse(data, content_type="application/json" if kind == "json" else "application/yaml")
    reply["Content-Disposition"] = f'attachment; filename="pypulse-workspace.{kind}"'
    return reply


@admin_only
@require_POST
def import_file(request):
    try:
        upload = request.FILES.get("config")
        pasted = request.POST.get("document", "")
        if (upload is None and not pasted.strip()) or (upload is not None and pasted.strip()):
            raise ConfigError("Upload a file or paste one JSON or YAML document.")
        if upload and upload.size > LIMIT:
            raise ConfigError("Choose a JSON or YAML file up to 1 MiB.")
        mode = request.POST.get("mode", "merge")
        if mode == "replace" and request.POST.get("confirm") != "on":
            raise ConfigError("Confirm removal of missing records for Replace.")
        doc = parse_file(upload.read() if upload is not None else pasted.encode())
        with transaction.atomic():
            counts = import_config(doc, mode, request.POST.getlist("sections"))
            record(request, "Imported workspace configuration", details={"mode": mode, "counts": counts})
        messages.success(request, "Imported " + ", ".join(f"{n} {section}" for section, n in counts.items()) + ".")
        return redirect("workspace-config")
    except ConfigError as error:
        reply = show(request, "config", sections=SECTIONS, error=str(error))
        reply.status_code = 400
        return reply
