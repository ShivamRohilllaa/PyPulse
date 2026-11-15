import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from console.benchconfig import next_schedule
from console.engine import benchmark, check_monitor, enqueue
from console.models import Exercise, Monitor, Run
from console.retention import trim
from console.uptime_engine import alert_tick

log = logging.getLogger(__name__)


def guarded(fn, *args):
    try:
        fn(*args)
    except Exception:
        log.exception("Worker job %s failed", fn.__name__)
    finally:
        close_old_connections()


def execute(run):
    try:
        asyncio.run(benchmark(run))
    except Exception:
        Run.objects.filter(pk=run.pk).update(state="Failed", error="Worker could not complete this run",
                                             finished=timezone.now())
        raise


def schedule_checks(pool, active, now):
    for pk in list(active):
        if active[pk].done():
            del active[pk]
    slots = 4 - len(active)
    if slots <= 0:
        return
    due = Monitor.objects.filter(paused=False, next_at__lte=now).exclude(pk__in=active).order_by("next_at", "pk")
    for monitor in due[:slots]:
        claimed = Monitor.objects.filter(pk=monitor.pk, next_at=monitor.next_at, paused=False).update(
            next_at=now + timedelta(seconds=monitor.interval))
        if claimed:
            active[monitor.pk] = pool.submit(guarded, check_monitor, monitor.pk)


def schedule_runs(now):
    due = Exercise.objects.filter(next_at__lte=now).order_by("next_at", "pk")
    for exercise in due[:20]:
        with transaction.atomic():
            following = next_schedule(exercise, now)
            claimed = Exercise.objects.filter(pk=exercise.pk, next_at=exercise.next_at).update(next_at=following)
            if not claimed or following is None:
                continue
            if Run.objects.filter(exercise=exercise, state__in=["Queued", "Running"]).exists():
                continue
            try:
                enqueue(exercise)
            except ValidationError:
                # A removed profile or edited allow-list can invalidate a previously saved test.
                Run.objects.create(exercise=exercise, state="Failed", finished=now,
                                   error="Scheduled test configuration is no longer valid.")


def start_next(pool):
    with transaction.atomic():
        run = Run.objects.filter(state="Queued", stop=False).order_by("pk").first()
        if not run:
            return None
        run.state, run.started = "Running", timezone.now()
        run.save(update_fields=["state", "started"])
    return pool.submit(guarded, execute, run)


class Command(BaseCommand):
    help = "Run the monitor scheduler and benchmark queue. Start one worker per installation."

    def handle(self, **options):
        Run.objects.filter(state="Running").update(state="Interrupted", finished=timezone.now())
        active_checks = {}
        run = alerts = cleanup = None
        next_trim = 0
        with ThreadPoolExecutor(max_workers=7, thread_name_prefix="pypulse") as pool:
            while True:
                try:
                    now = timezone.now()
                    schedule_checks(pool, active_checks, now)
                    schedule_runs(now)
                    if run is None or run.done():
                        run = start_next(pool)
                    if alerts is None or alerts.done():
                        alerts = pool.submit(guarded, alert_tick)
                    if time.monotonic() >= next_trim and (cleanup is None or cleanup.done()):
                        cleanup = pool.submit(guarded, trim)
                        next_trim = time.monotonic() + 3600
                except Exception:
                    log.exception("Scheduler iteration failed")
                close_old_connections()
                time.sleep(1)
