import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone
from console.engine import benchmark, check_monitor, enqueue
from console.models import Batch, Check, Exercise, Monitor, Run

log=logging.getLogger(__name__)

def execute(run):
    try:
        asyncio.run(benchmark(run))
    except Exception:
        log.exception("Benchmark %s stopped",run.pk)
        Run.objects.filter(pk=run.pk).update(state="Failed",error="Worker could not complete this run",finished=timezone.now())
    finally:
        close_old_connections()

def tick():
    now=timezone.now()
    for monitor in Monitor.objects.filter(paused=False,next_at__lte=now)[:20]:
        if Monitor.objects.filter(pk=monitor.pk,next_at=monitor.next_at).update(next_at=now+timedelta(seconds=monitor.interval)):
            check_monitor(monitor.pk)
    for exercise in Exercise.objects.filter(repeat_minutes__gt=0,next_at__lte=now)[:20]:
        with transaction.atomic():
            changed=Exercise.objects.filter(pk=exercise.pk,next_at=exercise.next_at).update(next_at=now+timedelta(minutes=exercise.repeat_minutes))
            if changed and not Run.objects.filter(exercise=exercise,state__in=["Queued","Running"]).exists():
                enqueue(exercise)

class Command(BaseCommand):
    help="Run the monitor scheduler and benchmark queue. Start one worker per installation."

    def handle(self, **options):
        Run.objects.filter(state="Running").update(state="Interrupted",finished=timezone.now())
        last_trim=0
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=None
            while True:
                try:
                    tick()
                    if future is None or future.done():
                        run=Run.objects.filter(state="Queued").order_by("pk").first()
                        if run and Run.objects.filter(pk=run.pk,state="Queued").update(state="Running",started=timezone.now()):
                            future=pool.submit(execute,run)
                    if time.monotonic()-last_trim>3600 or not last_trim:
                        Batch.objects.filter(at__lt=timezone.now()-timedelta(days=7)).delete()
                        Check.objects.filter(at__lt=timezone.now()-timedelta(days=30)).delete()
                        last_trim=time.monotonic()
                except Exception:
                    log.exception("Scheduler iteration failed")
                close_old_connections()
                time.sleep(1)
