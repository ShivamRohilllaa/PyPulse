"""Scheduled observations, incident transitions and the notification outbox."""
import hashlib
import hmac
import http.client
import json
import os
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import probes
from .models import AlertChannel, AlertDelivery, Check, Incident, Maintenance, Monitor


def occurrence(window, now):
    """Current or next occurrence in UTC; a one-off retains its original dates."""
    start = window.start
    duration = window.end - start
    period = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}.get(window.recurrence)
    if period and now >= start:
        start += ((now - start) // period) * period
        if now >= start + duration:
            start += period
    return start, start + duration


def maintenance_until(monitor, now):
    windows = Maintenance.for_monitor(monitor).filter(start__lte=now).exclude(recurrence="once", end__lte=now)
    ends = []
    for window in windows:
        start, end = occurrence(window, now)
        if start <= now < end:
            ends.append(end)
    return max(ends, default=None)


def in_maintenance(monitor, now):
    return maintenance_until(monitor, now) is not None


def request_options(monitor):
    return json.loads(probes.unseal(monitor.request_secret)) if monitor.request_secret else {}


def verdict(monitor, options, result):
    if result["error"] or not result["status"]:
        return "Request failed"
    if result["status"] not in options.get("statuses", [monitor.expected_status]):
        return "Unexpected HTTP status"
    if monitor.contains and monitor.contains.encode() not in result["body"]:
        return "Body text mismatch"
    if "json_equals" in options:
        try:
            observed = json.loads(result["body"])
            for path, expected in options["json_equals"].items():
                value = observed
                for part in path.split(".") if path else []:
                    value = value[int(part)] if isinstance(value, list) and part.isdecimal() else value[part]
                if type(value) is not type(expected) or value != expected:
                    return "JSON value mismatch"
        except (ValueError, IndexError, KeyError, TypeError, RecursionError):
            return "JSON assertion failed"
    return ""


def notify(monitor, state, event=None, incident=None, now=None):
    now = now or timezone.now()
    if in_maintenance(monitor, now):
        return
    event = event or state
    if monitor.alert_events and event not in monitor.alert_events:
        return
    if event == "Escalation" and monitor.escalation_channel_id:
        channels = AlertChannel.objects.filter(enabled=True, pk=monitor.escalation_channel_id)
    elif event != "Escalation" and monitor.channels.exists():
        channels = monitor.channels.filter(enabled=True)
    else:
        channels = AlertChannel.objects.filter(enabled=True, escalation_only=(event == "Escalation"))
    rows = [AlertDelivery(monitor=monitor, channel=channel, incident=incident, state=state, event=event, at=now, due=now)
            for channel in channels if channel.config_secret]
    if os.environ.get("PYPULSE_ALERT_WEBHOOK") and event != "Escalation" and not monitor.channels.exists():
        rows.append(AlertDelivery(monitor=monitor, state=state, incident=incident, event=event, at=now, due=now))
    AlertDelivery.objects.bulk_create(rows)


def measure_monitor(monitor, *, timeout_ms=None):
    """Evaluate the request without recording or exposing its contents."""
    try:
        opts = request_options(monitor)
        request = {key: opts[key] for key in ("headers", "query", "body", "auth") if key in opts}
        request.update(url=monitor.url, method=monitor.method)
        variables = probes.workspace_variables(monitor.environment)
        profile = monitor.network_profile.snapshot() if monitor.network_profile else None
        result = probes.probe(request, variables, profile, timeout_ms=timeout_ms if timeout_ms is not None else monitor.timeout_ms)
        reason = verdict(monitor, opts, result)
    except Exception:
        # Invalid stored configuration and transport failures must not reveal request secrets.
        result, reason = {"ms": 0, "status": 0}, "Check configuration failed"
    return {"ms": result["ms"], "status": result["status"] or None, "reason": reason}


def check_monitor(pk):
    monitor = Monitor.objects.select_related("network_profile").filter(pk=pk, paused=False).first()
    if monitor is None:
        return
    result = measure_monitor(monitor)
    reason = result["reason"]
    now = timezone.now()
    with transaction.atomic():
        monitor = Monitor.objects.select_for_update().filter(pk=pk, paused=False).first()
        if monitor is None:
            return
        previous = monitor.state
        slow = bool(not reason and monitor.warn_ms and result["ms"] > monitor.warn_ms)
        Check.objects.create(monitor=monitor, at=now, ms=result["ms"], status=result["status"] or None, ok=not reason, reason=reason, slow=slow)
        monitor.failures = monitor.failures + 1 if reason else 0
        if not reason:
            monitor.state = "Slow" if slow else "Up"
        elif monitor.failures >= monitor.failure_threshold:
            monitor.state = "Down"
        monitor.save(update_fields=["state", "failures"])
        if monitor.state == previous:
            return
        incident = Incident.objects.filter(monitor=monitor, closed__isnull=True).first()
        if monitor.state == "Down":
            reminder = now + timedelta(minutes=monitor.reminder_minutes) if monitor.reminder_minutes else None
            escalation = now + timedelta(minutes=monitor.escalation_minutes) if monitor.escalation_minutes else None
            incident = Incident.objects.create(monitor=monitor, opened=now, next_reminder=reminder, next_escalation=escalation)
            notify(monitor, "Down", "Opened", incident, now)
        elif previous == "Down":
            if incident:
                incident.closed = now
                incident.save(update_fields=["closed"])
            notify(monitor, monitor.state, "Recovered", incident, now)
        elif monitor.state == "Slow" or previous == "Slow":
            notify(monitor, monitor.state, monitor.state, now=now)


def queue_followups(now):
    due = Q(next_reminder__lte=now) | Q(next_escalation__lte=now)
    pending = Incident.objects.filter(due, closed=None, acknowledged=False, monitor__paused=False).values_list("pk", flat=True)[:50]
    for pk in list(pending):
        with transaction.atomic():
            item = Incident.objects.select_for_update().select_related("monitor").filter(due, pk=pk, closed=None, acknowledged=False).first()
            if not item or item.monitor.paused:
                continue
            resume = maintenance_until(item.monitor, now)
            if resume:
                for field in ("next_escalation", "next_reminder"):
                    value = getattr(item, field)
                    if value and value <= now:
                        setattr(item, field, resume)
                item.save(update_fields=["next_escalation","next_reminder"])
                continue
            if item.next_escalation and item.next_escalation <= now:
                notify(item.monitor, "Down", "Escalation", item, now)
                item.escalated, item.next_escalation = True, None
            if item.next_reminder and item.next_reminder <= now:
                notify(item.monitor, "Down", "Reminder", item, now)
                item.reminder_at = now
                minutes = item.monitor.reminder_minutes
                item.next_reminder = now + timedelta(minutes=minutes) if minutes else None
            item.save(update_fields=["reminder_at", "next_reminder", "escalated", "next_escalation"])


def post_json(address, body, headers):
    url = urlsplit(address)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Invalid destination")
    connection = (http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection)(url.hostname, url.port, timeout=5)
    try:
        path = url.path or "/"
        connection.request("POST", path + ("?" + url.query if url.query else ""), body, headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ValueError("Delivery rejected")
    finally:
        connection.close()


def send_delivery(delivery):
    channel = delivery.channel
    if channel:
        kind, config = channel.kind, json.loads(probes.unseal(channel.config_secret))
    else:
        kind, config = "webhook", {"url": os.environ["PYPULSE_ALERT_WEBHOOK"], "secret": os.environ.get("PYPULSE_ALERT_SECRET", "")}
    name = delivery.monitor.name if delivery.monitor_id else "PyPulse"
    text = f"{name} · {delivery.event} · {delivery.state}"
    if kind == "email":
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = text, config["from"], ", ".join(config["to"])
        message.set_content(f"{text}\nUTC {delivery.at.isoformat()}\nDelivery {delivery.pk}")
        smtp = smtplib.SMTP_SSL if config.get("security", "starttls") == "tls" else smtplib.SMTP
        options = {"context": ssl.create_default_context()} if smtp is smtplib.SMTP_SSL else {}
        with smtp(config["host"], config["port"], timeout=5, **options) as client:
            if config.get("security", "starttls") == "starttls":
                client.starttls(context=ssl.create_default_context())
            if config.get("username"):
                client.login(config["username"], config["password"])
            client.send_message(message)
        return
    payload = {"monitor": name, "state": delivery.state, "event": delivery.event, "at": delivery.at.isoformat(), "delivery_id": delivery.pk}
    if kind == "slack":
        payload = {"text": text}
    elif kind == "teams":
        payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                   "content": {"type": "AdaptiveCard", "version": "1.2", "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if kind == "webhook" and config.get("secret"):
        headers["X-PyPulse-Signature"] = hmac.new(config["secret"].encode(), body, hashlib.sha256).hexdigest()
    post_json(config["url"], body, headers)


def alert_tick():
    now = timezone.now()
    queue_followups(now)
    due = AlertDelivery.objects.filter(disposition__in=["Queued", "Sending"], due__lte=now).order_by("due", "pk")[:20]
    for row in list(due):
        claimed = AlertDelivery.objects.filter(pk=row.pk, disposition=row.disposition, due=row.due).update(
            disposition="Sending", due=now + timedelta(minutes=2), attempts=row.attempts + 1)
        if not claimed:
            continue
        followup_finished = (row.event in ("Reminder", "Escalation") and row.incident_id
                             and (row.incident.closed or row.incident.acknowledged or row.monitor.paused))
        if followup_finished or (row.channel_id and not row.channel.enabled) or (row.monitor_id and in_maintenance(row.monitor, timezone.now())):
            AlertDelivery.objects.filter(pk=row.pk).update(disposition="Suppressed")
            continue
        try:
            send_delivery(row)
        except Exception:
            attempts = row.attempts + 1
            AlertDelivery.objects.filter(pk=row.pk).update(disposition="Failed" if attempts >= 3 else "Queued",
                due=timezone.now() + timedelta(seconds=30 * attempts))
        else:
            AlertDelivery.objects.filter(pk=row.pk).update(disposition="Delivered")
