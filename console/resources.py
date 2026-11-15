from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Audit, NetworkProfile, Variable


class NetworkForm(forms.ModelForm):
    class Meta:
        model = NetworkProfile
        fields = ("name", "download_kbps", "upload_kbps", "latency_ms", "jitter_ms", "loss_pct")
        labels = {"download_kbps": "Download (kbit/s)", "upload_kbps": "Upload (kbit/s)",
                  "latency_ms": "Added latency (ms)", "jitter_ms": "Latency variation (±ms)",
                  "loss_pct": "Requests to drop (%)"}


class VariableForm(forms.ModelForm):
    value = forms.CharField(required=False, strip=False, max_length=16384,
                            widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    class Meta:
        model = Variable
        fields = ("name", "environment", "secret")
        labels = {"secret": "Mask this value in the dashboard"}
        help_texts = {"name": "Use {{NAME}} in a request.", "environment": "Leave blank to use across environments."}

    def clean(self):
        data = super().clean()
        if not self.instance.pk and not data.get("value"):
            self.add_error("value", "Enter a value.")
        elif self.instance.pk and self.instance.secret and data.get("secret") is False and not data.get("value"):
            self.add_error("value", "Enter a new value before turning masking off.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data["value"]:
            obj.set_value(self.cleaned_data["value"])
        if commit:
            obj.save()
        return obj


RESOURCES = {
    "networks": (NetworkProfile, NetworkForm, "Network profiles", "network profile"),
    "variables": (Variable, VariableForm, "Variables", "variable"),
}


def authorize(request, model, operation):
    if not request.user.has_perm(f"console.{operation}_{model._meta.model_name}"):
        raise PermissionDenied


@login_required
@require_GET
@never_cache
def listing(request, kind):
    model, _, title, singular = RESOURCES[kind]
    authorize(request, model, "view")
    rows = model.objects.order_by("name", "environment" if kind == "variables" else "pk")
    if kind == "variables":
        rows = [{"item": v, "display": "••••••••" if v.secret else v.reveal()} for v in rows]
    return render(request, "console/resources.html", {
        "section": title, "kind": kind, "singular": singular, "rows": rows,
    })


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def edit(request, kind, pk=None):
    model, form_type, section, singular = RESOURCES[kind]
    authorize(request, model, "change" if pk else "add")
    item = get_object_or_404(model, pk=pk) if pk else None
    if getattr(item, "preset", False):
        raise PermissionDenied
    form = form_type(request.POST if request.method == "POST" else None, instance=item)
    if pk and kind == "variables":
        form.fields["value"].help_text = "Leave blank to keep the current value."
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save()
            Audit.objects.create(actor=request.user.get_username(), action=f"Saved {singular} {item.name}")
        messages.success(request, f"Saved {item.name}.")
        return redirect(kind)
    return render(request, "console/form.html", {"form": form, "section": section,
                  "title": ("Edit " if pk else "New ") + singular, "button": "Save"})


@login_required
@require_POST
def remove(request, kind, pk):
    model, _, _, singular = RESOURCES[kind]
    authorize(request, model, "delete")
    with transaction.atomic():
        item = get_object_or_404(model, pk=pk)
        if getattr(item, "preset", False):
            raise PermissionDenied
        Audit.objects.create(actor=request.user.get_username(), action=f"Removed {singular} {item.name}")
        item.delete()
    return redirect(kind)
