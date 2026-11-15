"""Bounded, process-local aggregation. HTTP work runs outside the request thread."""
import bisect
import json
from http.client import HTTPException
import logging
import os
import threading
import time
import uuid
from collections import deque
from urllib import request
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
BOUNDS_MS = (5, 25, 100, 250, 500, 1000, 2500, 10000)
METHODS = frozenset("GET HEAD POST PUT PATCH DELETE OPTIONS TRACE CONNECT".split())


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Agent:
    def __init__(self, url, token, *, interval=10):
        address = urlsplit(url)
        if address.scheme not in ("http", "https") or not address.hostname or not token:
            raise ValueError("Set PYPULSE_URL to the dashboard base URL and PYPULSE_TOKEN to the service token")
        if not 1 <= interval <= 60:
            raise ValueError("interval must be between 1 and 60 seconds")
        self.endpoint = url.rstrip("/") + "/api/python/ingest"
        self.token = token
        self.interval = interval
        self._reset()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset)

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("PYPULSE_URL", ""), os.environ.get("PYPULSE_TOKEN", ""))

    def _reset(self):
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._routes = {}
        self._queue = deque(maxlen=60)
        self._stop = threading.Event()
        self._thread = None

    def record(self, method, route, status, elapsed_ms, queries=None):
        """Supply the matched route template only. None groups all unmatched requests."""
        key = (method if method in METHODS else "OTHER", (route or "(unmatched)")[:256])
        with self._lock:
            if self._stop.is_set():
                return
            if key not in self._routes and len(self._routes) >= 255:
                key = ("OTHER", "(overflow)")
            if key not in self._routes:
                self._routes[key] = dict(method=key[0], route=key[1], count=0, clientErrors=0,
                                         serverErrors=0, sqlSamples=0, sqlQueries=0,
                                         buckets=[0] * (len(BOUNDS_MS) + 1))
            row = self._routes[key]
            row["count"] += 1
            row["clientErrors"] += int(400 <= status < 500)
            row["serverErrors"] += int(500 <= status < 600)
            row["buckets"][bisect.bisect_left(BOUNDS_MS, elapsed_ms)] += 1
            if queries is not None:
                row["sqlSamples"] += 1
                row["sqlQueries"] += queries
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="pypulse-export", daemon=True)
                self._thread.start()

    def _send(self, batch):
        req = request.Request(self.endpoint, data=json.dumps(batch).encode(),
                              headers={"Content-Type": "application/json",
                                       "Authorization": "Bearer " + self.token})
        with request.build_opener(_NoRedirect()).open(req, timeout=3) as response:
            return response.status in (200, 202, 204)

    def flush(self):
        """Snapshot current counters. Unacknowledged batches retain their ID on retry."""
        with self._send_lock:
            with self._lock:
                if self._routes:
                    self._queue.append(dict(id=str(uuid.uuid4()), timestamp=int(time.time() * 1000),
                                            routes=list(self._routes.values())))
                    self._routes = {}
            while self._queue:
                try:
                    if not self._send(self._queue[0]):
                        return False
                except (OSError, HTTPException, ValueError) as exc:
                    log.warning("PyPulse export unsuccessful (%s)", type(exc).__name__)
                    return False
                self._queue.popleft()
        return True

    def _run(self):
        while not self._stop.wait(self.interval):
            self.flush()

    def close(self):
        self._stop.set()
        return self.flush()
