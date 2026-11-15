"""Session authentication, first-run setup and shared sign-in throttling."""
from datetime import timedelta
from functools import wraps
import hashlib
import hmac
import ipaddress
import json
import os

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.signals import user_login_failed, user_logged_in
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .audit import record
from .models import LoginThrottle
from .workspace_roles import role_of

User = get_user_model()
WINDOW = timedelta(minutes=15)


def json_data(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("Send a JSON object.") from None
    if not isinstance(value, dict):
        raise ValueError("Send a JSON object.")
    return value


def json_view(fn):
    @never_cache
    @wraps(fn)
    def response(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except ValueError as error:
            return JsonResponse({"error": str(error)}, status=400)
    return response


def signed_in(fn):
    @wraps(fn)
    def response(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sign in required."}, status=401)
        return fn(request, *args, **kwargs)
    return response


def profile(user):
    return {"id": user.pk, "username": user.username, "email": user.email, "name": user.first_name,
            "role": role_of(user), "disabled": not user.is_active, "lastLogin": user.last_login}


class LoginThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path in ("/login/", "/admin/login/", "/api/auth/login"):
            try:
                data = json_data(request) if request.content_type == "application/json" else request.POST
            except ValueError:
                return JsonResponse({"error": "Send a JSON object."}, status=400)
            username = data.get("username") or data.get("email") or ""
            if isinstance(username, str):
                pair = (request.META.get("REMOTE_ADDR", "") + "\x00" + username.strip().casefold()[:254]).encode()
                key = hmac.new(settings.SECRET_KEY.encode(), b"login-attempt\x00" + pair, hashlib.sha256).hexdigest()
                request._login_limit_key = key
                if LoginThrottle.objects.filter(key=key, since__gt=timezone.now()-WINDOW, failures__gte=10).exists():
                    text = "Too many failed sign-ins. Try again in 15 minutes."
                    reply = (JsonResponse({"error": text}, status=429) if request.path.startswith("/api/")
                             else render(request, "console/workspace_signin_limit.html", {"error": text}, status=429))
                    reply["Retry-After"] = "900"
                    return reply
        return self.get_response(request)


def record_failure(sender, request=None, **kw):
    key = getattr(request, "_login_limit_key", None)
    if key:
        now = timezone.now()
        with transaction.atomic():
            LoginThrottle.objects.filter(since__lte=now-WINDOW).delete()
            row, _ = LoginThrottle.objects.select_for_update().get_or_create(key=key, defaults={"since": now})
            row.failures = min(row.failures + 1, 10)
            row.save(update_fields=["failures"])


def record_login(sender, request, user, **kw):
    key = getattr(request, "_login_limit_key", None)
    if key:
        LoginThrottle.objects.filter(key=key).delete()
    record(user, "Signed in", user)


user_login_failed.connect(record_failure, dispatch_uid="pypulse.login.failure")
user_logged_in.connect(record_login, dispatch_uid="pypulse.login.success")


def local_setup(request):
    # A forwarded loopback connection belongs to the proxy. Never use forwarded client IPs as proof of locality.
    forwarded = ("HTTP_FORWARDED", "HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PROTO")
    if any(request.META.get(header) for header in forwarded):
        return False
    try:
        local_peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
    except ValueError:
        return False
    from urllib.parse import urlsplit
    return local_peer and urlsplit("//" + request.get_host()).hostname in {"localhost", "127.0.0.1", "::1"}


def needs_key(request):
    return not local_setup(request)


def can_offer_setup(request):
    return local_setup(request) or (request.is_secure() and bool(os.environ.get("PYPULSE_SETUP_SECRET")))


class SetupForm(forms.Form):
    name = forms.CharField(max_length=150, required=False, label="Display name")
    email = forms.EmailField(max_length=150)
    password = forms.CharField(min_length=12, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    confirm = forms.CharField(strip=False, label="Confirm password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    setup_key = forms.CharField(required=False, label="Server setup secret", strip=False, widget=forms.PasswordInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        if not needs_key(request):
            self.fields.pop("setup_key")

    def clean(self):
        data = super().clean()
        if data.get("password") != data.get("confirm"):
            self.add_error("confirm", "Passwords do not match.")
        if needs_key(self.request):
            expected = os.environ.get("PYPULSE_SETUP_SECRET", "")
            if not expected or not self.request.is_secure() or not constant_time_compare(data.get("setup_key", ""), expected):
                self.add_error("setup_key", "The server setup secret was not accepted.")
        return data


def first_admin(data):
    if User.objects.exists():
        raise ValueError("First-run setup is already complete.")
    user = User.objects.create_superuser(username=data["email"], email=data["email"], first_name=data.get("name", ""),
                                         password=data["password"])
    record(user, "Created first administrator", user)
    return user


class WorkspaceLogin(LoginView):
    template_name = "console/login.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["needs_local_setup"] = not User.objects.exists() and not can_offer_setup(self.request)
        return context

    def get(self, request, *args, **kwargs):
        if not User.objects.exists() and can_offer_setup(request):
            return redirect("workspace-setup")
        return super().get(request, *args, **kwargs)


@never_cache
@require_http_methods(["GET", "POST"])
def setup(request):
    if User.objects.exists():
        return redirect("login")
    if not can_offer_setup(request):
        return HttpResponse("Use a local console or configure PYPULSE_SETUP_SECRET on an HTTPS deployment.", status=403)
    form = SetupForm(request.POST if request.method == "POST" else None, request=request)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if User.objects.exists():
                return HttpResponse("First-run setup is already complete.", status=409)
            user = first_admin(form.cleaned_data)
            login(request, user)
        return redirect("traffic")
    return render(request, "console/workspace_setup.html", {"form": form})


class AccountPasswordForm(PasswordChangeForm):
    new_password1 = forms.CharField(min_length=12, strip=False, label="New password",
                                   widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def account_view(request):
    form = AccountPasswordForm(request.user, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        record(user, "Changed own password", user)
        messages.success(request, "Password changed.")
        return redirect("workspace-account")
    return render(request, "console/workspace_account.html",
                  {"section": "Workspace", "form": form, "role": role_of(request.user)})


@json_view
@require_GET
def status_api(request):
    return JsonResponse({"needsSetup": not User.objects.exists(), "csrfToken": get_token(request),
                         "user": profile(request.user) if request.user.is_authenticated else None})


@json_view
@require_POST
@transaction.atomic
def setup_api(request):
    if User.objects.exists():
        return JsonResponse({"error": "First-run setup is already complete."}, status=409)
    if not can_offer_setup(request):
        return JsonResponse({"error": "Local setup or HTTPS with a server setup secret is required."}, status=403)
    form = SetupForm(json_data(request), request=request)
    if not form.is_valid():
        return JsonResponse({"error": form.errors}, status=400)
    user = first_admin(form.cleaned_data)
    login(request, user)
    return JsonResponse({"user": profile(user), "csrfToken": get_token(request)}, status=201)


@json_view
@require_POST
def login_api(request):
    data = json_data(request)
    username = data.get("username") or data.get("email")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Supply a username and password.")
    user = authenticate(request, username=username.strip(), password=password)
    if not user:
        return JsonResponse({"error": "Invalid username or password."}, status=401)
    login(request, user)
    return JsonResponse({"user": profile(user), "csrfToken": get_token(request)})


@json_view
@require_POST
@signed_in
def logout_api(request):
    logout(request)
    return JsonResponse({"ok": True})


@json_view
@require_GET
@signed_in
def me_api(request):
    return JsonResponse({"user": profile(request.user)})


@json_view
@require_POST
@signed_in
def password_api(request):
    data = json_data(request)
    form = AccountPasswordForm(request.user, {"old_password": data.get("currentPassword"), "new_password1": data.get("newPassword"),
                                               "new_password2": data.get("newPassword")})
    if not form.is_valid():
        return JsonResponse({"error": form.errors}, status=400)
    user = form.save()
    update_session_auth_hash(request, user)
    record(user, "Changed own password", user)
    return JsonResponse({"ok": True, "csrfToken": get_token(request)})
