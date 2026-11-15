from django.conf import settings
from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator


class NetworkProfile(models.Model):
    description = models.CharField(max_length=240, blank=True)
    name = models.CharField(max_length=80, unique=True)
    download_kbps = models.PositiveIntegerField(default=0, help_text="0 for unlimited")
    upload_kbps = models.PositiveIntegerField(default=0, help_text="0 for unlimited")
    latency_ms = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(60000)])
    jitter_ms = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(60000)])
    loss_pct = models.FloatField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    preset = models.BooleanField(default=False, editable=False)

    def snapshot(self):
        return {field: getattr(self, field) for field in
                ("name", "download_kbps", "upload_kbps", "latency_ms", "jitter_ms", "loss_pct")}

    def __str__(self):
        return self.name


class Variable(models.Model):
    description = models.CharField(max_length=240, blank=True)
    name = models.CharField(max_length=80, validators=[RegexValidator(r"^[A-Za-z_][A-Za-z0-9_]*$")])
    environment = models.CharField(max_length=80, blank=True)
    secret = models.BooleanField(default=True)
    ciphertext = models.TextField(editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("name","environment"), name="unique_variable_scope")]

    def set_value(self, value):
        from .probes import seal
        self.ciphertext = seal(value)

    def reveal(self):
        from .probes import unseal
        return unseal(self.ciphertext)

    def __str__(self):
        return f"{self.name} ({self.environment or 'global'})"


class Service(models.Model):
    FRAMEWORKS = [(v, v) for v in ("Django / DRF", "FastAPI", "Flask")]
    name = models.CharField(max_length=100, unique=True)
    framework = models.CharField(max_length=30, choices=FRAMEWORKS)
    target = models.FloatField(default=99.9)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    last_seen = models.DateTimeField(null=True, editable=False)

    def __str__(self):
        return self.name


class Batch(models.Model):
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    batch_id = models.UUIDField()
    at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["service","batch_id"], name="unique_service_delivery")]


class Slice(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="slices")
    method = models.CharField(max_length=12)
    route = models.CharField(max_length=256)
    count = models.IntegerField()
    client_errors = models.IntegerField()
    server_errors = models.IntegerField()
    sql_samples = models.IntegerField()
    sql_queries = models.IntegerField()
    histogram = models.JSONField()


class Monitor(models.Model):
    status_expression = models.CharField(max_length=160, blank=True)
    escalation_channels = models.ManyToManyField("AlertChannel", blank=True, related_name="escalation_targets")
    tags = models.CharField(max_length=240, blank=True)
    alert_events = models.JSONField(default=list, blank=True)
    channels = models.ManyToManyField("AlertChannel", blank=True, related_name="monitors")
    escalation_channel = models.ForeignKey("AlertChannel", null=True, blank=True, on_delete=models.SET_NULL, related_name="escalation_monitors")
    group = models.CharField(max_length=80, blank=True)
    method = models.CharField(max_length=8, default="GET")
    timeout_ms = models.PositiveIntegerField(default=5000)
    request_secret = models.TextField(blank=True, editable=False)
    environment = models.CharField(max_length=100, blank=True)
    network_profile = models.ForeignKey("NetworkProfile", null=True, blank=True, on_delete=models.SET_NULL)
    reminder_minutes = models.PositiveIntegerField(default=0)
    escalation_minutes = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=120)
    url = models.CharField(max_length=1000)
    interval = models.PositiveIntegerField(default=60)
    expected_status = models.PositiveIntegerField(default=200)
    contains = models.CharField(max_length=200, blank=True)
    warn_ms = models.PositiveIntegerField(default=1000)
    failure_threshold = models.PositiveIntegerField(default=2)
    failures = models.PositiveIntegerField(default=0, editable=False)
    paused = models.BooleanField(default=False)
    public = models.BooleanField(default=False)
    state = models.CharField(max_length=16, default="Pending", editable=False)
    next_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return self.name


class Check(models.Model):
    slow = models.BooleanField(default=False)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.IntegerField(null=True)
    ms = models.FloatField()
    ok = models.BooleanField()
    reason = models.CharField(max_length=100, blank=True)


class CheckHour(models.Model):
    latency_histogram = models.JSONField(default=dict, blank=True)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="hours")
    hour = models.DateTimeField(db_index=True)
    total = models.PositiveIntegerField()
    passed = models.PositiveIntegerField()
    slow_count = models.PositiveIntegerField(default=0)
    elapsed_sum_ms = models.FloatField()
    min_ms = models.FloatField()
    max_ms = models.FloatField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("monitor", "hour"), name="unique_monitor_hour")]


class Incident(models.Model):
    kind = models.CharField(max_length=16, default="Down")
    reason = models.CharField(max_length=100, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True)
    suppressed = models.BooleanField(default=False)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="incidents")
    opened = models.DateTimeField(default=timezone.now)
    closed = models.DateTimeField(null=True)
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.CharField(max_length=150, blank=True)
    acknowledged_at = models.DateTimeField(null=True)
    reminder_at = models.DateTimeField(null=True)
    escalated = models.BooleanField(default=False)
    next_reminder = models.DateTimeField(null=True)
    next_escalation = models.DateTimeField(null=True)



class Exercise(models.Model):
    PATTERNS = [(v, v.title()) for v in ("fixed","ramp","spike","soak","stress","latency")]
    name = models.CharField(max_length=120)
    url = models.URLField()
    pattern = models.CharField(max_length=12, choices=PATTERNS, default="fixed")
    rate = models.PositiveIntegerField(default=5)
    seconds = models.PositiveIntegerField(default=15)
    max_p95 = models.PositiveIntegerField(default=0)
    max_error_pct = models.FloatField(default=0)
    repeat_minutes = models.PositiveIntegerField(default=0)
    options = models.JSONField(default=dict, blank=True)
    next_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Run(models.Model):
    exercise = models.ForeignKey(Exercise, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=120, blank=True)
    trigger = models.CharField(max_length=12, default="unknown")
    actor = models.CharField(max_length=150, blank=True)

    @property
    def display_name(self):
        return self.name or (self.exercise.name if self.exercise_id else f"Run #{self.pk}")

    config = models.JSONField(default=dict)
    started = models.DateTimeField(default=timezone.now)
    finished = models.DateTimeField(null=True)
    state = models.CharField(max_length=16, default="Queued")
    stop = models.BooleanField(default=False)
    result = models.JSONField(default=dict)
    series = models.JSONField(default=list)
    samples = models.JSONField(default=list)
    error = models.CharField(max_length=200, blank=True)


class AlertChannel(models.Model):
    KINDS = [(v, t) for v, t in (("webhook", "Signed webhook"), ("slack", "Slack"), ("teams", "Microsoft Teams"), ("email", "SMTP email"))]
    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=16, choices=KINDS)
    enabled = models.BooleanField(default=True)
    escalation_only = models.BooleanField(default=False)
    config_secret = models.TextField(blank=True, editable=False)

    def __str__(self):
        return self.name


class AlertDelivery(models.Model):
    monitor = models.ForeignKey(Monitor, null=True, on_delete=models.CASCADE)
    channel = models.ForeignKey(AlertChannel, null=True, on_delete=models.CASCADE)
    incident = models.ForeignKey(Incident, null=True, on_delete=models.CASCADE)
    event = models.CharField(max_length=24)
    state = models.CharField(max_length=16)
    at = models.DateTimeField(default=timezone.now)
    due = models.DateTimeField(default=timezone.now, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    disposition = models.CharField(max_length=16, default="Queued")


class Maintenance(models.Model):
    name = models.CharField(max_length=120, blank=True)
    all_monitors = models.BooleanField(default=False)
    targets = models.ManyToManyField("Monitor", blank=True, related_name="shared_windows")

    @classmethod
    def for_monitor(cls, monitor):
        return cls.objects.filter(models.Q(monitor=monitor) | models.Q(all_monitors=True) | models.Q(targets=monitor)).distinct()

    recurrence = models.CharField(max_length=8, choices=[("once","Once"),("daily","Daily"),("weekly","Weekly")], default="once")
    monitor = models.ForeignKey(Monitor, null=True, blank=True, on_delete=models.CASCADE)
    start = models.DateTimeField()
    end = models.DateTimeField()


class Audit(models.Model):
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=60, blank=True)
    details = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=200)


class WorkspaceSettings(models.Model):
    interval = models.PositiveIntegerField(default=60)
    timeout_ms = models.PositiveIntegerField(default=5000)
    failure_threshold = models.PositiveIntegerField(default=2)
    warn_ms = models.PositiveIntegerField(default=1000)
    raw_days = models.PositiveIntegerField(default=30)
    rollup_days = models.PositiveIntegerField(default=365)
    run_days = models.PositiveIntegerField(default=90)
    telemetry_days = models.PositiveIntegerField(default=7)
    allowed_hosts = models.TextField(blank=True)
    status_enabled = models.BooleanField(default=True)
    status_title = models.CharField(max_length=140, default="Service availability")

    @classmethod
    def current(cls):
        """Defaults can be read without writing a row during a report request."""
        return cls.objects.filter(pk=1).first() or cls(pk=1)

    def __str__(self):
        return "Workspace settings"


class ApiToken(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=16, editable=False)
    digest = models.CharField(max_length=64, unique=True, editable=False)
    scope = models.CharField(max_length=8, choices=[("read", "Read run results"), ("run", "Start tests and read results")])
    created = models.DateTimeField(default=timezone.now)
    last_used = models.DateTimeField(null=True, editable=False)
    revoked = models.DateTimeField(null=True, editable=False)


class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    since = models.DateTimeField(default=timezone.now, db_index=True)
    failures = models.PositiveSmallIntegerField(default=0)
