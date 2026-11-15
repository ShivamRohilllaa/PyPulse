import json
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import AlertChannel, Maintenance, Monitor


class PrivateJSON(forms.JSONField):
    """The browser never receives a saved or previously submitted secret."""
    widget = forms.PasswordInput

    def __init__(self, **kwargs):
        super().__init__(widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "spellcheck": "false"}), **kwargs)

    def to_python(self, value):
        if isinstance(value, str) and len(value) > 65536:
            raise ValidationError("Limit this configuration to 64 KiB.")
        try:
            result = super().to_python(value)
        except (ValidationError, RecursionError):
            raise ValidationError("Enter a JSON object.") from None
        if result is not None and not isinstance(result, dict):
            raise ValidationError("Enter a JSON object.")
        return result


def save_private(form, field, model_field, commit):
    from .probes import seal
    obj = forms.ModelForm.save(form, commit=False)
    value = form.cleaned_data.get(field)
    if value is not None:
        setattr(obj, model_field, seal(json.dumps(value)))
    if commit:
        obj.save()
        form.save_m2m()
    return obj


class MonitorForm(forms.ModelForm):
    url = forms.CharField(label="Endpoint URL", max_length=1000, help_text="Place query parameters in the encrypted request options.")
    interval = forms.IntegerField(min_value=10, max_value=86400, initial=60, label="Check interval (seconds)")
    timeout_ms = forms.IntegerField(min_value=100, max_value=60000, initial=5000, label="Timeout (ms)")
    failure_threshold = forms.IntegerField(min_value=1, max_value=50, initial=2, label="Consecutive failures before an incident")
    expected_status = forms.IntegerField(min_value=100, max_value=599, initial=200, label="Expected HTTP status")
    method = forms.ChoiceField(choices=[(m,m) for m in ("GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS")], initial="GET")
    alert_events = forms.MultipleChoiceField(required=False, choices=[(value,value) for value in
        ("Opened", "Recovered", "Slow", "Up", "Reminder", "Escalation")], help_text="Leave empty to receive every supported event.")
    options = PrivateJSON(required=False, label="Private request options (JSON)", help_text=(
        'Available keys: headers, query, body, auth, statuses and json_equals. '
        'For example {"headers":{"Accept":"application/json"},"json_equals":{"health.ready":true}}. '
        'Leave blank to keep existing options. Enter {} to reset.'))
    class Meta:
        model = Monitor
        fields = ("name","group","tags","method","url","environment","network_profile","options","interval","timeout_ms",
                  "expected_status","contains","warn_ms","failure_threshold","alert_events","channels","reminder_minutes","escalation_minutes","escalation_channel","paused","public")
        labels = {"group":"Project / group","channels":"Standard alert channels","contains": "Required body text", "warn_ms": "Slow response threshold (ms)", "public": "Show on public status page"}
        help_texts = {"tags":"Separate tags with commas.", "channels":"Leave empty to use all standard channels.", "group": "Group related endpoints under a shared label.", "reminder_minutes": "Repeat alerts for an unacknowledged incident. 0 disables reminders.",
                      "escalation_minutes": "Notify escalation channels once after this delay. 0 disables escalation."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legacy_query = {}
        if self.instance.pk and "?" in self.instance.url:
            parts = urlsplit(self.instance.url)
            self.legacy_query = dict(parse_qsl(parts.query, keep_blank_values=True))
            self.initial["url"] = urlunsplit(parts._replace(query=""))
        if not self.instance.pk:
            from .models import WorkspaceSettings
            defaults = WorkspaceSettings.current()
            for key in ("interval", "timeout_ms", "failure_threshold", "warn_ms"):
                self.initial[key] = getattr(defaults, key)
        # Old API clients that omit extension fields keep their original defaults.
        if self.is_bound:
            self.data = self.data.copy()
            for key in ("timeout_ms", "method", "reminder_minutes", "escalation_minutes"):
                self.data.setdefault(self.add_prefix(key), self.initial.get(key, getattr(self.instance, key)))

    def clean_tags(self):
        return ", ".join(dict.fromkeys(tag.strip().lower() for tag in self.cleaned_data["tags"].split(",") if tag.strip()))

    def clean(self):
        from .probes import http_url, interpolate, workspace_variables
        data = super().clean()
        if data.get("url"):
            try:
                value = interpolate(data["url"], workspace_variables(data.get("environment", "")))
                if urlsplit(http_url(value)).query:
                    raise ValidationError("Place query parameters in the private request options.")
            except (ValidationError, ValueError):
                self.add_error("url", "Enter a resolvable HTTP(S) URL; move any query parameters to private request options.")
        return data

    def clean_options(self):
        options = self.cleaned_data.get("options")
        if options is None:
            return None
        if set(options) - {"headers","query","body","auth","statuses","json_equals"}:
            raise ValidationError("An unsupported request option was supplied.")
        for key in ("headers", "query"):
            if key in options and (not isinstance(options[key], dict) or
                    not all(isinstance(v, str) and isinstance(k,str) for k,v in options[key].items())):
                raise ValidationError("Headers and query parameters must be JSON objects with string values.")
        if "body" in options and not isinstance(options["body"], (str, dict)):
            raise ValidationError("Supply a text or JSON object body.")
        if "statuses" in options and (not isinstance(options["statuses"], list) or not options["statuses"] or
                any(type(v) is not int or not 100 <= v <= 599 for v in options["statuses"])):
            raise ValidationError("Supply one or more HTTP status codes between 100 and 599.")
        if "json_equals" in options and not isinstance(options["json_equals"], dict):
            raise ValidationError("Supply a JSON object mapping dotted paths to expected values.")
        if "auth" in options:
            auth = options["auth"]
            required = {"none": (), "bearer": ("token",), "basic": ("username", "password"), "api_key": ("name", "value")}
            kind = auth.get("type") if isinstance(auth, dict) else None
            if not isinstance(kind, str) or kind not in required or any(not isinstance(auth.get(k),str) or not auth[k] for k in required[kind]):
                raise ValidationError("Supply a complete none, bearer, basic or api_key configuration.")
            if kind == "api_key" and auth.get("in","header") not in ("header","query"):
                raise ValidationError("API keys must use a header or query parameter.")
        return options

    def save(self, commit=True):
        if self.legacy_query and self.cleaned_data.get("options") is None:
            from .uptime_engine import request_options
            options = request_options(self.instance)
            options["query"] = {**self.legacy_query, **options.get("query", {})}
            self.cleaned_data["options"] = options
        return save_private(self, "options", "request_secret", commit)


class ChannelForm(forms.ModelForm):
    configuration = PrivateJSON(required=False, label="Private delivery configuration (JSON)",
        help_text="Editing a channel? Leave this blank to keep its existing configuration.")
    class Meta:
        model = AlertChannel
        fields = ("name","kind","enabled","escalation_only","configuration")
        help_texts = {"escalation_only": "Receive escalations instead of initial, reminder and recovery alerts."}

    def clean(self):
        data = super().clean()
        kind, config = data.get("kind"), data.get("configuration")
        if not kind:
            return data
        if config is None:
            if not self.instance.config_secret or kind != self.instance.kind:
                self.add_error("configuration", "Supply the channel configuration.")
            return data
        if kind == "email":
            try:
                if (not isinstance(config.get("host"),str) or not config["host"] or type(config.get("port")) is not int
                        or not 1 <= config["port"] <= 65535 or config.get("security","starttls") not in ("tls","starttls","plain")
                        or not isinstance(config.get("to"),list) or not config["to"] or not isinstance(config.get("from"),str)):
                    raise ValueError
                if config.get("username") and (not isinstance(config.get("password"),str) or config.get("security") == "plain"):
                    raise ValueError
                for addr in [config["from"], *config["to"]]:
                    validate_email(addr)
            except (ValidationError, ValueError, TypeError, AttributeError):
                self.add_error("configuration","Supply SMTP host, port, from, to and valid TLS settings.")
        else:
            try:
                parsed = urlsplit(config.get("url", ""))
                port = parsed.port
                if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment or
                        (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost","127.0.0.1","::1")))):
                    raise ValueError
                if kind == "webhook" and (not isinstance(config.get("secret"),str) or not config["secret"]):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self.add_error("configuration","Supply an HTTPS URL (HTTP is available on loopback) and a signing secret for a webhook.")
        return data

    def save(self, commit=True):
        return save_private(self, "configuration", "config_secret", commit)


class MaintenanceForm(forms.ModelForm):
    class Meta:
        model = Maintenance
        fields = ("start","end","recurrence")
        labels = {"start": "Starts (UTC)", "end": "Ends (UTC)"}
        widgets = {key: forms.DateTimeInput(format="%Y-%m-%dT%H:%M",attrs={"type":"datetime-local"}) for key in ("start","end")}

    def clean(self):
        data = super().clean()
        start, end = data.get("start"), data.get("end")
        if start and end:
            length = end - start
            if length <= timedelta(0):
                self.add_error("end","End must follow start.")
            period = {"daily": 1, "weekly": 7}.get(data.get("recurrence"))
            if period and length >= timedelta(days=period):
                self.add_error("end","The window must end before its next occurrence.")
        return data


class SharedMaintenanceForm(MaintenanceForm):
    name = forms.CharField(max_length=120, label="Window name")
    all_monitors = forms.BooleanField(required=False, label="All current and future monitors")
    targets = forms.ModelMultipleChoiceField(queryset=Monitor.objects.order_by("name"), required=False,
        label="Selected monitors", widget=forms.CheckboxSelectMultiple)

    class Meta(MaintenanceForm.Meta):
        fields = ("name", "start", "end", "recurrence", "all_monitors", "targets")

    def clean(self):
        data = super().clean()
        targets = data.get("targets")
        if data.get("all_monitors") and targets:
            self.add_error("targets", "Clear selected monitors to use the workspace-wide scope.")
        elif not data.get("all_monitors") and not targets:
            self.add_error("targets", "Choose at least one monitor or select the workspace-wide scope.")
        return data

    def save_window(self):
        obj = self.save(commit=False)
        targets = list(self.cleaned_data["targets"])
        obj.monitor = targets[0] if len(targets) == 1 else None
        obj.save()
        obj.targets.set(targets if len(targets) > 1 else [])
        return obj
