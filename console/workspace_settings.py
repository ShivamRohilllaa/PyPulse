"""Workspace defaults shared by the console, scheduler and test target validator."""
import ipaddress
import re
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import ValidationError

from .models import WorkspaceSettings


def normalize_hosts(raw):
    hosts = []
    for line in raw.splitlines():
        item = line.strip().lower()
        if not item:
            continue
        wildcard = item.startswith("*.")
        try:
            authority = item[2:] if wildcard else item
            url = urlsplit("//" + authority)
            host, port = url.hostname, url.port
            if not host or url.username is not None or url.password is not None or url.path or url.fragment or url.query:
                raise ValueError
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                host = host.encode("idna").decode("ascii")
                labels = host.split(".")
                if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels):
                    raise ValueError
                if wildcard and len(labels) < 2:
                    raise ValueError
            else:
                if wildcard:
                    raise ValueError
                host = f"[{ip}]" if ip.version == 6 else str(ip)
            if port == 0 or authority.endswith(":") or any(c in authority for c in ("#", "?", "/", "@")):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ValidationError("Enter one hostname per line, optionally with a port or a *.subdomain prefix.")
        value = ("*." if wildcard else "") + host + (f":{port}" if port else "")
        if value not in hosts:
            hosts.append(value)
    if len(hosts) > 200:
        raise ValidationError("Use at most 200 additional host patterns.")
    return "\n".join(hosts)


class SettingsForm(forms.ModelForm):
    interval = forms.IntegerField(min_value=10, max_value=86400, label="Check interval (seconds)")
    timeout_ms = forms.IntegerField(min_value=100, max_value=30000, label="Request timeout (ms)")
    failure_threshold = forms.IntegerField(min_value=1, max_value=50, label="Failures before opening an incident")
    warn_ms = forms.IntegerField(min_value=0, max_value=300000, label="Slow response threshold (ms)")
    raw_days = forms.IntegerField(min_value=1, max_value=3650, label="Raw uptime checks (days)")
    rollup_days = forms.IntegerField(min_value=1, max_value=3650, label="Hourly summaries (days)")
    run_days = forms.IntegerField(min_value=1, max_value=3650, label="Finished test runs (days)")
    telemetry_days = forms.IntegerField(min_value=1, max_value=3650, label="Python traffic batches (days)")

    class Meta:
        model = WorkspaceSettings
        fields = ("interval", "timeout_ms", "failure_threshold", "warn_ms", "raw_days", "rollup_days",
                  "run_days", "telemetry_days", "allowed_hosts", "status_enabled", "status_title")
        labels = {"allowed_hosts": "Additional approved benchmark hosts", "status_enabled": "Enable public status page"}
        widgets = {"allowed_hosts": forms.Textarea(attrs={"rows": 3, "placeholder": "staging.example.com:443"})}
        help_texts = {"allowed_hosts": "One host per line. A *.staging.example.com pattern allows its subdomains."}

    def clean_allowed_hosts(self):
        return normalize_hosts(self.cleaned_data["allowed_hosts"])


def extra_test_hosts():
    return tuple(WorkspaceSettings.current().allowed_hosts.splitlines())


def retention():
    settings = WorkspaceSettings.current()
    return {key: getattr(settings, key) for key in ("raw_days", "rollup_days", "run_days", "telemetry_days")}
