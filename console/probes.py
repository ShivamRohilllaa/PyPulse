"""HTTP measurement shared by the scheduler and performance worker."""
import base64
import hashlib
import json
import random
import re
import threading
import secrets
import uuid
from datetime import datetime, timezone
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import pycurl
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import Variable

_TEMPLATE = re.compile(r"{{\s*(\$?[A-Za-z_][A-Za-z0-9_]*)\s*}}")
_DYNAMIC_START = re.compile(r"{{\s*\$")
_DYNAMIC_NAMES = {"$timestamp", "$timestampMs", "$isoTimestamp", "$uuid", "$randomInt"}
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_LOCAL = threading.local()
_PHASES = ("dns", "tcp", "tls", "ttfb", "download")
_PREFIX_LIMIT = 65536
_BODY_LIMIT = 32 * 1024 * 1024
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _cipher():
    key = hashlib.sha256(b"PyPulse/variables/v1\0" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value):
    return _cipher().encrypt(value.encode()).decode()


def unseal(value):
    return _cipher().decrypt(value.encode()).decode()


def workspace_variables(environment=""):
    rows = Variable.objects.filter(Q(environment="") | Q(environment=environment or ""))
    return {row.name: row.reveal() for row in rows.order_by("environment", "pk")}


def validate_dynamic_placeholders(value):
    """Check reserved placeholders without resolving secrets, fixtures or extraction names."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, str):
            for start in _DYNAMIC_START.finditer(item):
                token = _TEMPLATE.match(item, start.start())
                if not token or token.group(1) not in _DYNAMIC_NAMES:
                    raise ValidationError("Unknown or malformed dynamic placeholder. Use $timestamp, $timestampMs, $isoTimestamp, $uuid or $randomInt.")


class _RequestVariables:
    def __init__(self, variables):
        self.variables = variables
        self.generated = {}
        self.timestamp_ms = time.time_ns() // 1_000_000

    def resolve(self, name):
        if not name.startswith("$"):
            if name not in self.variables:
                raise ValidationError("Missing variable: %(name)s", params={"name": name})
            return str(self.variables[name])
        if name not in self.generated:
            if name == "$timestamp":
                value = str(self.timestamp_ms // 1000)
            elif name == "$timestampMs":
                value = str(self.timestamp_ms)
            elif name == "$isoTimestamp":
                utc = datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)
                value = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            elif name == "$uuid":
                value = str(uuid.uuid4())
            elif name == "$randomInt":
                value = str(secrets.randbelow(1_000_000))
            else:
                raise ValidationError("Unknown dynamic placeholder.")
            self.generated[name] = value
        return self.generated[name]


def interpolate(value, variables, *, json_string=False):
    text = str(value)
    if "{{" not in text:
        return text
    validate_dynamic_placeholders(text)
    scope = variables if isinstance(variables, _RequestVariables) else _RequestVariables(variables)
    def substitute(match):
        replacement = scope.resolve(match.group(1))
        return json.dumps(replacement)[1:-1] if json_string else replacement
    return _TEMPLATE.sub(substitute, text)


def http_url(value):
    if not isinstance(value, str) or len(value) > 8192 or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValidationError("Enter a valid HTTP(S) URL.")
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if url.scheme not in ("http", "https") or not host or url.username is not None or url.password is not None or url.fragment:
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9.:_-]+", host):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return urlunsplit((url.scheme, authority, url.path or "/", url.query, ""))
    except (UnicodeError, ValueError) as exc:
        raise ValidationError("Enter an HTTP(S) URL without credentials or a fragment.") from exc


def host_allowed(url, patterns):
    parts = urlsplit(url)
    host = parts.hostname
    authority = f"[{host}]" if ":" in host else host
    authority += f":{parts.port or (443 if parts.scheme == 'https' else 80)}"
    for raw in patterns:
        item = raw.strip().lower().rstrip(".")
        if item in (host, f"[{host}]" if ":" in host else host, authority):
            return True
        if item.startswith("*.") and (host.endswith(item[1:]) or authority.endswith(item[1:])):
            return True
    return False


def approved_url(value, allowed=False):
    url = http_url(value)
    if allowed and not host_allowed(url, settings.TEST_HOSTS):
        from .workspace_settings import extra_test_hosts
        if not host_allowed(url, extra_test_hosts()):
            raise ValidationError("The target host and port are outside the load-test allow-list.")
    return url


def _prepare(spec, variables, allowed):
    variables = _RequestVariables(variables)
    url = approved_url(interpolate(spec["url"], variables), allowed)
    parsed = urlsplit(url)
    query = [(interpolate(k, variables), interpolate(v, variables)) for k, v in (spec.get("query") or {}).items()]
    headers = {interpolate(k, variables): interpolate(v, variables) for k, v in (spec.get("headers") or {}).items()}
    auth = spec.get("auth") or {}
    kind = auth.get("type", "none")
    if kind == "bearer":
        headers["Authorization"] = "Bearer " + interpolate(auth["token"], variables)
    elif kind == "basic":
        text = interpolate(auth["username"], variables) + ":" + interpolate(auth["password"], variables)
        headers["Authorization"] = "Basic " + base64.b64encode(text.encode()).decode()
    elif kind in ("api_key","apikey","apiKey"):
        key = interpolate(auth["name"], variables)
        val = interpolate(auth["value"], variables)
        if auth.get("in") == "query":
            query.append((key, val))
        else:
            headers[key] = val
    elif kind != "none":
        raise ValidationError("Unknown authentication type.")
    body = spec.get("body")
    prepared_body = "form" in spec or isinstance(body, (list, dict))
    if "form" in spec:
        if body is not None or not isinstance(spec["form"], dict):
            raise ValidationError("Choose either form fields or a request body.")
        body = urlencode([(interpolate(k, variables), interpolate(v, variables)) for k,v in spec["form"].items()])
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(body, (list, dict)):
        body = interpolate(json.dumps(body), variables, json_string=True)
        if not any(name.lower()=="content-type" for name in headers):
            headers["Content-Type"] = "application/json"
    method = str(spec.get("method","GET")).upper()
    if method not in _METHODS:
        raise ValidationError("Unsupported HTTP method.")
    for k, v in headers.items():
        if not _HEADER_NAME.fullmatch(k) or any(ord(c)<32 or ord(c)==127 for c in v):
            raise ValidationError("Invalid header.")
        if k.lower() in ("host", "content-length", "transfer-encoding", "connection", "proxy-authorization"):
            raise ValidationError("This header is managed by the HTTP client.")
    if query:
        url = urlunsplit(parsed._replace(query="&".join(filter(None, (parsed.query, urlencode(query))))))
    return (url,method,headers,
            (body.encode() if prepared_body else interpolate(body,variables).encode()) if body is not None else b"")


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _delay(seconds, deadline, cancel):
    end = time.perf_counter() + max(0, seconds)
    while time.perf_counter() < end:
        if _cancelled(cancel):
            return "Cancelled"
        if time.perf_counter() >= deadline:
            return "Timeout"
        time.sleep(max(0, min(0.05, end-time.perf_counter(), deadline-time.perf_counter())))
    return ""


def probe(spec, variables=None, profile=None, *, timeout_ms=5000, keep_alive=True, allowed=False, cancel=None):
    """Measure one request. Never persist query strings, response bodies or expanded credentials."""
    variables = variables or {}
    profile = profile or {}
    result = dict(status=0, ms=0., sent=0, received=0, error="", body=b"", headers={}, reused=None, compressed=None,
                  phases=dict.fromkeys(_PHASES, 0.))
    started = time.perf_counter()
    deadline = started + max(.05, min(300., timeout_ms/1000))
    handle = getattr(_LOCAL, "handle", None)
    if handle is None:
        handle = _LOCAL.handle = pycurl.Curl()
    handle.reset()
    prefix = bytearray()
    header_size = 0

    def capture(chunk):
        result["received"] += len(chunk)
        if result["received"] > _BODY_LIMIT:
            result["error"] = "Response exceeds size limit"
            return 0
        prefix.extend(chunk[:max(0, _PREFIX_LIMIT-len(prefix))])
        return len(chunk)

    def header(line):
        nonlocal header_size
        header_size += len(line)
        if header_size > _PREFIX_LIMIT:
            result["error"] = "Response headers exceed size limit"
            return 0
        if line.startswith(b"HTTP/"):
            result["headers"].clear()
        elif b":" in line:
            key, val = line.split(b":", 1)
            result["headers"][key.decode("latin1").lower()] = val.decode("latin1").strip()
        return len(line)

    try:
        url, method, headers, body = _prepare(spec, variables, allowed)
        jitter = max(0, profile.get("jitter_ms", 0))
        latency = max(0, profile.get("latency_ms", 0)+random.uniform(-jitter, jitter))/1000
        error = _delay(latency, deadline, cancel)
        if error or _cancelled(cancel):
            result["error"] = error or "Cancelled"
            return result
        if profile.get("loss_pct", 0) and random.random()*100 < profile["loss_pct"]:
            result["error"] = "Simulated connection loss"
            return result
        handle.setopt(pycurl.URL, url)
        handle.setopt(pycurl.PROTOCOLS, pycurl.PROTO_HTTP | pycurl.PROTO_HTTPS)
        handle.setopt(pycurl.NOSIGNAL, 1)
        handle.setopt(pycurl.PROXY, "")
        handle.setopt(pycurl.FOLLOWLOCATION, False)
        handle.setopt(pycurl.TIMEOUT_MS, max(1,int((deadline-time.perf_counter())*1000)))
        handle.setopt(pycurl.FRESH_CONNECT, not keep_alive)
        handle.setopt(pycurl.FORBID_REUSE, not keep_alive)
        if spec.get("ignore_tls") is True:
            handle.setopt(pycurl.SSL_VERIFYPEER, False)
            handle.setopt(pycurl.SSL_VERIFYHOST, 0)
        if spec.get("compression") is True:
            handle.setopt(pycurl.ACCEPT_ENCODING, "")
        handle.setopt(pycurl.WRITEFUNCTION, capture)
        handle.setopt(pycurl.HEADERFUNCTION, header)
        if cancel:
            handle.setopt(pycurl.NOPROGRESS, False)
            handle.setopt(pycurl.XFERINFOFUNCTION, lambda *_: int(_cancelled(cancel)))
        if body:
            handle.setopt(pycurl.POSTFIELDS, body)
        if method == "HEAD":
            handle.setopt(pycurl.NOBODY, True)
        handle.setopt(pycurl.CUSTOMREQUEST, method)
        handle.setopt(pycurl.HTTPHEADER, [f"{k}: {v}".encode() for k, v in headers.items()])
        up = profile.get("upload_kbps",0)*125
        down = profile.get("download_kbps",0)*125
        if up:
            handle.setopt(pycurl.MAX_SEND_SPEED_LARGE, max(1,int(up)))
        if down:
            handle.setopt(pycurl.MAX_RECV_SPEED_LARGE, max(1,int(down)))
        wire_start = time.perf_counter()
        handle.perform()
        result["status"] = handle.getinfo(pycurl.RESPONSE_CODE)
        result["sent"] = int(handle.getinfo(pycurl.SIZE_UPLOAD_T))
        result["received"] = int(handle.getinfo(pycurl.SIZE_DOWNLOAD_T))
        result["reused"] = handle.getinfo(pycurl.NUM_CONNECTS) == 0
        encoding = result["headers"].get("content-encoding", "").lower().strip()
        result["compressed"] = bool(encoding and encoding != "identity")
        up_floor = result["sent"]/up if up else 0
        down_floor = result["received"]/down if down else 0
        wire = time.perf_counter()-wire_start
        tail = max(0,up_floor+down_floor-wire)
        result["error"] = _delay(tail, deadline, cancel)
        dns, connect, tls, pre, first, total = (handle.getinfo(opt) for opt in
            (pycurl.NAMELOOKUP_TIME,pycurl.CONNECT_TIME,pycurl.APPCONNECT_TIME,pycurl.PRETRANSFER_TIME,
             pycurl.STARTTRANSFER_TIME,pycurl.TOTAL_TIME))
        result["phases"] = {key: round(1000*max(0, value),3) for key,value in zip(_PHASES,
            (dns,connect-dns,tls-connect if tls else 0,first-pre,total-first))}
        result["body"] = bytes(prefix)
    except pycurl.error as exc:
        result["status"] = handle.getinfo(pycurl.RESPONSE_CODE)
        result["error"] = result["error"] or {pycurl.E_OPERATION_TIMEDOUT:"Timeout",pycurl.E_COULDNT_CONNECT:"Connection failed",
            pycurl.E_COULDNT_RESOLVE_HOST:"DNS lookup failed",pycurl.E_ABORTED_BY_CALLBACK:"Cancelled"}.get(exc.args[0], "HTTP transport error")
    except (KeyError, TypeError, ValueError, ValidationError, UnicodeError):
        result["error"] = "Invalid request configuration or target"
    finally:
        result["ms"] = round((time.perf_counter()-started)*1000,3)
        # Release references to response buffers and credentials after each measurement.
        handle.reset()
    return result
