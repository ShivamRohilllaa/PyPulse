"""Portable, versioned workspace configuration with transactional imports."""
import json
import math
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .benchconfig import next_schedule, validate_budget, validate_options
from .models import AlertChannel, Exercise, Maintenance, Monitor, NetworkProfile, Variable, WorkspaceSettings
from .probes import unseal
from .workspace_settings import SettingsForm

SCHEMA = "pypulse.workspace/v1"
MASK = {"$redacted": True}
LIMIT = 1024 * 1024
MODELS = {"profiles": NetworkProfile, "variables": Variable, "channels": AlertChannel,
          "monitors": Monitor, "benchmarks": Exercise, "maintenance": Maintenance}
SECTIONS = ("settings", *MODELS)
FIELDS = {
    "profiles": ("name", "description", "download_kbps", "upload_kbps", "latency_ms", "jitter_ms", "loss_pct"),
    "variables": ("name", "description", "environment", "secret"),
    "channels": ("name", "kind", "enabled", "escalation_only"),
    "monitors": ("name", "group", "tags", "alert_events", "url", "method", "environment", "interval", "expected_status", "status_expression", "contains", "warn_ms",
                 "failure_threshold", "paused", "public", "timeout_ms", "reminder_minutes", "escalation_minutes"),
    "benchmarks": ("name", "url", "pattern", "rate", "seconds", "max_p95", "max_error_pct", "repeat_minutes"),
    "maintenance": ("name", "recurrence"),
}
EXTRAS = {"profiles": (), "variables": ("value",), "channels": ("config",),
          "monitors": ("profile", "request", "channels", "escalation_channel", "escalation_channels"), "benchmarks": ("options",), "maintenance": ("monitor", "targets", "all_monitors", "start", "end")}


class ConfigError(ValueError):
    pass


class ConfigLoader(yaml.SafeLoader):
    """Disallow aliases and duplicate keys so file structure is explicit."""
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConfigError("YAML aliases are unsupported.")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        pairs = self.construct_pairs(node, deep)
        keys = [k for k, _ in pairs]
        if any(not isinstance(k, str) for k in keys) or len(keys) != len(set(keys)):
            raise ConfigError("Use distinct text keys.")
        return dict(pairs)


def parse_file(raw):
    if len(raw) > LIMIT:
        raise ConfigError("The file exceeds 1 MiB.")
    try:
        doc = yaml.load(raw.decode("utf-8-sig"), Loader=ConfigLoader)
        # Refuse YAML-only types that cannot appear in the JSON format.
        json.dumps(doc, allow_nan=False)
    except (UnicodeError, yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ConfigError("Invalid JSON or YAML. Use distinct text keys and quote date strings.") from exc
    return doc


def encode(doc, format):
    if format == "json":
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    if format == "yaml":
        # Our structure uses no references. Avoid generated YAML aliases for common mask values.
        class Dumper(yaml.SafeDumper):
            def ignore_aliases(self, data):
                return True
        return yaml.dump(doc, Dumper=Dumper, sort_keys=False, allow_unicode=True)
    raise ConfigError("Choose JSON or YAML.")


def checked_sections(sections):
    if not sections or len(set(sections)) != len(sections) or not set(sections) <= set(SECTIONS):
        raise ConfigError("Choose one or more different workspace sections.")
    return [section for section in SECTIONS if section in sections]


def decode_secret(value):
    return json.loads(unseal(value)) if value else {}


def masked_url(url, secrets):
    if not secrets:
        parts = urlsplit(url)
        if parts.query or parts.username is not None or parts.password is not None:
            return MASK
    return url


def export_row(kind, obj, secrets):
    row = {key: getattr(obj, key) for key in FIELDS[kind]}
    if kind == "variables":
        row["value"] = MASK if not secrets and obj.secret else obj.reveal()
    elif kind == "channels":
        row["config"] = (decode_secret(obj.config_secret) if secrets else MASK) if obj.config_secret else {}
    elif kind == "monitors":
        for field in ("channels", "escalation_channels"):
            row[field] = list(getattr(obj, field).order_by("name").values_list("name", flat=True))
        row["escalation_channel"] = obj.escalation_channel.name if obj.escalation_channel_id else None
        row["profile"] = obj.network_profile.name if obj.network_profile_id else None
        row["url"] = masked_url(obj.url, secrets)
        options = decode_secret(obj.request_secret)
        row["request"] = {key: value if secrets or key == "statuses" or not value else MASK for key, value in options.items()}
    elif kind == "benchmarks":
        row["url"] = masked_url(obj.url, secrets)
        options = dict(obj.options)
        if options.get("profiles"):
            by_pk = NetworkProfile.objects.in_bulk(options["profiles"])
            try:
                options["profiles"] = [by_pk[n].name for n in options["profiles"]]
            except KeyError as error:
                raise ConfigError("A benchmark references a deleted profile. Edit it before exporting.") from error
        if not secrets:
            if options.get("csv_data"):
                options["csv_data"] = MASK
            options["steps"] = [
                {key: (masked_url(value, False) if key == "url" else MASK if key in
                       ("headers", "query", "body", "auth") and value else value) for key, value in step.items()}
                for step in options.get("steps", [])
            ]
        row["options"] = options
    elif kind == "maintenance":
        row.update(start=obj.start.isoformat(), end=obj.end.isoformat())
        if obj.monitor_id:
            row["monitor"] = obj.monitor.name
        elif obj.all_monitors:
            row["all_monitors"] = True
        else:
            row["targets"] = sorted(m.name for m in obj.targets.all())
    return row


def identity(section, row):
    if section == "maintenance":
        return (row.get("name", ""), row["start"], row.get("monitor"), row.get("all_monitors", False),
                tuple(row.get("targets", ())))
    if section == "variables":
        return row.get("name"), row.get("environment", "")
    return (row.get("name"),)


@transaction.atomic
def export_config(sections=SECTIONS, secrets=False):
    result = {}
    for section in checked_sections(sections):
        if section == "settings":
            settings = WorkspaceSettings.current()
            result[section] = {key: getattr(settings, key) for key in SettingsForm.Meta.fields}
        else:
            items = MODELS[section].objects.order_by("pk")
            if section == "maintenance":
                items = items.select_related("monitor").prefetch_related("targets")
            rows = [export_row(section, item, secrets) for item in items]
            if len(rows) != len({identity(section, row) for row in rows}):
                raise ConfigError(f"Give records in {section} distinct names or schedule start times before transfer.")
            result[section] = rows
    return {"schema": SCHEMA, "sections": result}


MISSING = object()


def restore_mask(value, existing=MISSING):
    if value == MASK:
        if existing is MISSING:
            raise ConfigError("A masked value has no existing match. Supply the value or use an export including secrets.")
        return existing
    if isinstance(value, dict):
        old = existing if isinstance(existing, dict) else {}
        return {k: restore_mask(v, old.get(k, MISSING)) for k, v in value.items()}
    if isinstance(value, list):
        old = existing if isinstance(existing, list) else []
        # Scenario edits can reorder named steps. Never assign a different step's credentials.
        by_name = {item["name"]: item for item in old if isinstance(item, dict) and isinstance(item.get("name"), str)}
        return [restore_mask(v, by_name.get(v.get("name"), MISSING) if isinstance(v, dict) and isinstance(v.get("name"), str)
                             else old[i] if i < len(old) else MISSING) for i, v in enumerate(value)]
    return value


def named(model, name):
    if not isinstance(name, str) or not name:
        raise ConfigError("A reference needs a name.")
    matches = list(model.objects.filter(name=name)[:2])
    if len(matches) != 1:
        raise ConfigError("A referenced name does not exist or is ambiguous.")
    return matches[0]


def timestamp(value):
    if not isinstance(value, str):
        raise ConfigError("Supply an ISO 8601 time with its UTC offset.")
    parsed = parse_datetime(value)
    if not parsed or timezone.is_naive(parsed):
        raise ConfigError("Supply an ISO 8601 time with its UTC offset.")
    return parsed


def existing_item(section, row):
    name = row.get("name")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ConfigError("Each record needs a name without leading or trailing spaces.")
    lookup = {"name": name}
    if section == "variables":
        lookup["environment"] = row.get("environment", "")
        if not isinstance(lookup["environment"], str):
            raise ConfigError("Environment must be a string.")
    model = MODELS[section]
    matches = list(model.objects.filter(**lookup)[:2])
    if len(matches) > 1:
        raise ConfigError(f"A record in {section} has an ambiguous identity.")
    return matches[0] if matches else model(**lookup)


def validate_scalar(field, value):
    field_type = field.get_internal_type()
    if field_type == "JSONField":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if field_type == "BooleanField":
        return type(value) is bool
    if field_type in ("PositiveIntegerField", "IntegerField"):
        return type(value) is int
    if field_type == "FloatField":
        return type(value) in (int, float) and math.isfinite(value)
    return isinstance(value, str)


def bounds(value, low, high):
    if not low <= value <= high:
        raise ConfigError(f"Value must be between {low} and {high}.")


def save_window(row):
    # Single-monitor v1 files omit both the name and the newer scope fields.
    start = timestamp(row.get("start"))
    all_monitors = row.get("all_monitors", False)
    names = row.get("targets", [])
    if type(all_monitors) is not bool or not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
        raise ConfigError("Use a boolean for all_monitors and a list of monitor names for targets.")
    if len(set(names)) != len(names):
        raise ConfigError("A maintenance target is listed more than once.")
    monitor = named(Monitor, row["monitor"]) if row.get("monitor") is not None else None
    if (all_monitors and monitor) or ((all_monitors or monitor) and names):
        raise ConfigError("Choose one maintenance scope.")
    if not all_monitors and not monitor and "targets" not in row:
        raise ConfigError("Supply a monitor, a targets list, or all_monitors: true.")
    targets = list(Monitor.objects.filter(name__in=names))
    if len(targets) != len(names) or {m.name for m in targets} != set(names):
        raise ConfigError("A maintenance target does not exist or has an ambiguous name.")
    lookup = {"start": start, "monitor": monitor, "all_monitors": all_monitors}
    if "name" in row:
        if not isinstance(row["name"], str):
            raise ConfigError("Maintenance name must be text.")
        lookup["name"] = row["name"]
    candidates = Maintenance.objects.filter(**lookup).prefetch_related("targets")
    matches = [window for window in candidates if set(window.targets.all()) == set(targets)]
    if len(matches) > 1:
        raise ConfigError("A maintenance window has an ambiguous identity.")
    window = matches[0] if matches else Maintenance(**lookup)
    if "recurrence" in row:
        window.recurrence = row["recurrence"]
    if "end" in row:
        window.end = timestamp(row["end"])
    if window.end is None or window.end <= start:
        raise ConfigError("Maintenance must end after its start.")
    max_days = {"daily": 1, "weekly": 7}.get(window.recurrence)
    if max_days and window.end - start >= timedelta(days=max_days):
        raise ConfigError("A recurring window cannot extend past its next start.")
    window.full_clean()
    window.save()
    window.targets.set(targets)
    return window.pk


def save_row(section, row):
    if not isinstance(row, dict) or set(row) - set(FIELDS[section] + EXTRAS[section]):
        raise ConfigError(f"Unrecognized field in {section}.")
    if section == "maintenance":
        return save_window(row)
    obj = existing_item(section, row)
    original_schedule = (obj.repeat_minutes, obj.options.get("daily_at", "")) if section == "benchmarks" else None
    original_kind = obj.kind if section == "channels" else None
    if (section == "variables" and obj.pk and obj.secret and row.get("secret") is False
            and row.get("value", MASK) == MASK):
        raise ConfigError("Supply a replacement value before changing a secret variable to public.")
    if section == "profiles" and obj.preset and any(getattr(obj, k) != v for k, v in row.items()):
        raise ConfigError("Built-in profiles are read-only. Give the custom profile a different name.")
    for field in FIELDS[section]:
        if field in row:
            value = restore_mask(row[field], getattr(obj, field) if obj.pk else MISSING)
            if not validate_scalar(obj._meta.get_field(field), value):
                raise ConfigError(f"Invalid type for {section}.{field}.")
            setattr(obj, field, value)
    if section == "variables":
        if "value" in row and (row["value"] != MASK or not obj.pk):
            value = restore_mask(row["value"])
            if not isinstance(value, str) or len(value) > 16384:
                raise ConfigError("Variable values must be text of at most 16,384 characters.")
            obj.set_value(value)
        if not obj.ciphertext:
            raise ConfigError("Supply a value for a new variable.")
    elif section == "channels":
        from .uptime_forms import ChannelForm
        if original_kind != obj.kind and row.get("config", MASK) == MASK and obj.config_secret:
            raise ConfigError("Changing the channel type requires its delivery configuration.")
        if row.get("config", MASK) != MASK:
            config = row["config"]
            if not isinstance(config, dict):
                raise ConfigError("Channel config must be an object.")
            if config:
                data = {field: getattr(obj, field) for field in FIELDS[section]}
                form = ChannelForm({**data, "configuration": json.dumps(config)}, instance=obj)
                if not form.is_valid():
                    raise ConfigError("Invalid delivery configuration for this channel type.")
                obj = form.save(commit=False)
            else:
                obj.config_secret = ""
        if obj.enabled and not obj.config_secret:
            raise ConfigError("An enabled channel needs its delivery configuration.")
    elif section == "monitors":
        from .uptime_forms import MonitorForm
        if "profile" in row:
            obj.network_profile = named(NetworkProfile, row["profile"]) if row["profile"] is not None else None
        old = decode_secret(obj.request_secret) if obj.pk else MISSING
        options = restore_mask(row["request"], old) if "request" in row else None
        parts = urlsplit(obj.url)
        if parts.query:
            options = (old if old is not MISSING else {}) if options is None else options
            if not isinstance(options, dict):
                raise ConfigError("Request options must be an object.")
            options["query"] = {**dict(parse_qsl(parts.query, keep_blank_values=True)), **options.get("query", {})}
            obj.url = urlunsplit(parts._replace(query=""))
        data = {field: getattr(obj, field) for field in FIELDS[section]}
        data["network_profile"] = obj.network_profile_id
        for field in ("channels", "escalation_channels"):
            if field in row:
                if not isinstance(row[field], list):
                    raise ConfigError("Channels must be a list of names.")
                data[field] = [named(AlertChannel, name).pk for name in row[field]]
            elif obj.pk:
                data[field] = list(getattr(obj, field).values_list("pk", flat=True))
        if "escalation_channel" in row:
            obj.escalation_channel = named(AlertChannel, row["escalation_channel"]) if row["escalation_channel"] is not None else None
        data["escalation_channel"] = obj.escalation_channel_id
        if options is not None:
            data["options"] = json.dumps(options)
        form = MonitorForm(data, instance=obj)
        if not form.is_valid():
            raise ConfigError(f"Invalid monitor settings in {', '.join(form.errors)}.")
        obj = form.save(commit=False)
    elif section == "benchmarks":
        options = obj.options
        if "options" in row:
            options = restore_mask(row["options"], obj.options if obj.pk else MISSING)
            if not isinstance(options, dict):
                raise ConfigError("Benchmark options must be an object.")
            if "profiles" in options:
                if not isinstance(options["profiles"], list):
                    raise ConfigError("Benchmark profiles must be a list of names.")
                options["profiles"] = [named(NetworkProfile, n).pk for n in options["profiles"]]
        obj.options = validate_options(options, obj.url)
        validate_budget(obj.options, obj.pattern, obj.rate, obj.seconds)
        bounds(obj.max_error_pct, 0, 100)
        bounds(obj.repeat_minutes, 0, 10080)
        if len(obj.url) > Exercise._meta.get_field("url").max_length:
            raise ConfigError("The default endpoint is too long. Put longer URLs in scenario steps.")
        daily_at = obj.options.get("daily_at", "")
        if obj.repeat_minutes and daily_at:
            raise ConfigError("Choose a daily time or a repeat interval.")
        if obj.next_at is None or original_schedule != (obj.repeat_minutes, daily_at):
            obj.next_at = next_schedule(obj, timezone.now())
    obj.full_clean(exclude=["url"] if section == "benchmarks" and "{{" in obj.url else None)
    obj.save()
    if section == "monitors":
        form.save_m2m()
    return obj.pk


def import_settings(row):
    if not isinstance(row, dict) or set(row) - set(SettingsForm.Meta.fields):
        raise ConfigError("Invalid workspace settings.")
    obj = WorkspaceSettings.current()
    data = {key: getattr(obj, key) for key in SettingsForm.Meta.fields}
    for key, value in row.items():
        if not validate_scalar(obj._meta.get_field(key), value):
            raise ConfigError(f"Invalid type for settings.{key}.")
        data[key] = value
    form = SettingsForm(data, instance=obj)
    if not form.is_valid():
        raise ConfigError(f"Invalid workspace settings: {', '.join(form.errors)}.")
    form.save()


@transaction.atomic
def import_config(doc, mode="merge", scopes=SECTIONS):
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or set(doc) != {"schema", "sections"}:
        raise ConfigError(f"Expected schema {SCHEMA} and sections.")
    sections = doc["sections"]
    if not isinstance(sections, dict):
        raise ConfigError("Invalid configuration sections.")
    sections_present = checked_sections(list(sections))
    requested = checked_sections(scopes)
    selected = [section for section in sections_present if section in requested]
    if not selected or mode not in ("merge", "replace"):
        raise ConfigError("Choose a matching section and a valid import mode.")
    keep = {}
    try:
        for section in selected:
            if section == "settings":
                import_settings(sections[section])
                continue
            rows = sections[section]
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ConfigError(f"Expected a list of up to 10,000 rows in {section}.")
            keep[section] = set()
            for index, row in enumerate(rows, 1):
                try:
                    pk = save_row(section, row)
                    if pk in keep[section]:
                        raise ConfigError("Duplicate record.")
                    keep[section].add(pk)
                except (ValidationError, ConfigError, ValueError, TypeError, KeyError) as error:
                    # Django validation can quote input, including submitted credentials.
                    detail = str(error) if isinstance(error, ConfigError) else "Invalid configuration values."
                    raise ConfigError(f"{section}, row {index}: {detail}") from error
        if mode == "replace":
            for section in reversed(selected):
                if section not in MODELS:
                    continue
                removing = MODELS[section].objects.exclude(pk__in=keep[section])
                if section == "benchmarks" and removing.filter(run__isnull=False).exists():
                    raise ConfigError("A removed benchmark has run history. Keep it in this import or remove it explicitly.")
                if section == "channels" and (Monitor.objects.filter(channels__in=removing).exists() or
                                               Monitor.objects.filter(Q(escalation_channel__in=removing) | Q(escalation_channels__in=removing)).exists()):
                    raise ConfigError("A removed channel is still used by a monitor.")
                if section == "profiles":
                    removing = removing.filter(preset=False)
                    ids = set(removing.values_list("pk", flat=True))
                    in_tests = any(ids.intersection(opts.get("profiles", [])) for opts in Exercise.objects.values_list("options", flat=True))
                    if in_tests or Monitor.objects.filter(network_profile_id__in=ids).exists():
                        raise ConfigError("A removed profile is still used by a monitor or benchmark.")
                if section == "monitors":
                    dependent = Maintenance.objects.filter(Q(monitor__in=removing) | Q(targets__in=removing))
                    if dependent.exists():
                        raise ConfigError("A retained maintenance window still references a removed monitor.")
                removing.delete()
    except ProtectedError as error:
        raise ConfigError("A removed configuration is still referenced by history. Use merge or keep that configuration.") from error
    return {section: len(keep[section]) if section in keep else 1 for section in selected}
