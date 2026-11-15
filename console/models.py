from django.db import models
from django.utils import timezone


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
    name = models.CharField(max_length=120)
    url = models.URLField()
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
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.IntegerField(null=True)
    ms = models.FloatField()
    ok = models.BooleanField()


class Incident(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="incidents")
    opened = models.DateTimeField(default=timezone.now)
    closed = models.DateTimeField(null=True)
    acknowledged = models.BooleanField(default=False)


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
    next_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Run(models.Model):
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT)
    config = models.JSONField(default=dict)
    started = models.DateTimeField(default=timezone.now)
    finished = models.DateTimeField(null=True)
    state = models.CharField(max_length=16, default="Queued")
    stop = models.BooleanField(default=False)
    result = models.JSONField(default=dict)
    series = models.JSONField(default=list)
    samples = models.JSONField(default=list)
    error = models.CharField(max_length=200, blank=True)


class Maintenance(models.Model):
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    start = models.DateTimeField()
    end = models.DateTimeField()


class Audit(models.Model):
    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=200)
