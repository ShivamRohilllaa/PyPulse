"""Notify authenticated ASGI dashboards when the shared SQLite workspace changes."""
import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, StreamingHttpResponse
from django.views.decorators.http import require_GET

_lock = threading.Lock()
_reader = None
_checked = -1.0
_revision = ""
_instance = f"{os.getpid()}-{time.time_ns()}"


def database_revision():
    global _reader, _checked, _revision
    with _lock:
        now = time.monotonic()
        if _reader is None:
            path = Path(settings.DATABASES["default"]["NAME"])
            _reader = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                      timeout=.2, check_same_thread=False)
        if now - _checked >= .75:
            version = _reader.execute("PRAGMA data_version").fetchone()[0]
            _revision, _checked = f"{_instance}-{version}", now
        return _revision


async def frames():
    previous = None
    yield "retry: 2500\n\n"
    # Reconnect periodically so Django revalidates session expiry and account access.
    for tick in range(60):
        version = await asyncio.to_thread(database_revision)
        if version != previous:
            yield f"id: {version}\nevent: change\ndata: {version}\n\n"
            previous = version
        elif tick % 15 == 0:
            yield ": heartbeat\n\n"
        await asyncio.sleep(1)


@require_GET
async def events(request):
    if not (await request.auser()).is_authenticated:
        return HttpResponse(status=401)
    if not hasattr(request, "scope"):
        return HttpResponse(status=204)
    stream = StreamingHttpResponse(frames(), content_type="text/event-stream")
    stream["Cache-Control"] = "private, no-cache, no-transform"
    stream["X-Accel-Buffering"] = "no"
    return stream
