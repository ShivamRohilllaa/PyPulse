"""Validate portable scenario configuration before recording or scheduling a test."""
import csv
import io
import re
import math
from datetime import timedelta,timezone as dt_timezone
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError

REF = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
SENSITIVE = re.compile(r"authorization|cookie|password|passwd|secret|api.?key|token", re.I)
PHASES = ("dns", "tcp", "tls", "ttfb", "download")
MAX_SAMPLES = 30_000
LIMITS = {"users": (1,20,5), "timeout_ms": (50,30000,5000), "delay_ms": (0,60000,0),
          "count": (1,10000,None), "plateau_windows": (0,60,0), "start_load": (1,50,None),
          "step_size": (1,50,None), "stage_seconds": (1,3600,None)}
KEYS = set(LIMITS) | {"model", "environment", "profiles", "steps", "csv_data", "stages", "keep_alive", "daily_at", "min_rps", "csv_order", "statuses"}


def fail(text):
    raise ValidationError(text)


def integer(value, lo, hi, name):
    if isinstance(value,bool) or not isinstance(value,int) or not lo <= value <= hi:
        fail(f"{name} must be a whole number between {lo} and {hi}.")
    return value


def statuses(value):
    if value is None or value=="" or value==[]: return []
    if isinstance(value,str):
        if len(value)>2000: fail("Status code list is too long.")
        codes=[]
        for part in value.split(","):
            ends=part.strip().split("-")
            if len(ends)>2 or not all(re.fullmatch(r"[1-5][0-9]{2}",v) for v in ends):
                fail("Enter HTTP codes or ranges, e.g. 200,201,204 or 200-299.")
            first,last=int(ends[0]),int(ends[-1])
            if first>last: fail("Status ranges must be ascending.")
            codes.extend(range(first,last+1))
    elif isinstance(value,list) and len(value)<=500:
        codes=[integer(v,100,599,"Accepted status") for v in value]
    else:
        fail("Enter a list of HTTP status codes.")
    return sorted(set(codes))


def next_schedule(exercise,now):
    daily=(exercise.options or {}).get("daily_at")
    if daily:
        hour,minute=map(int,daily.split(":"))
        current=now.astimezone(dt_timezone.utc)
        candidate=current.replace(hour=hour,minute=minute,second=0,microsecond=0)
        return candidate if candidate>current else candidate+timedelta(days=1)
    return now+timedelta(minutes=exercise.repeat_minutes) if exercise.repeat_minutes else None


def dataset(value):
    if not value:
        return []
    if not isinstance(value,str) or len(value.encode())>65536:
        fail("CSV data must be at most 64 KiB.")
    try:
        reader=csv.DictReader(io.StringIO(value.lstrip("\ufeff")), strict=True)
        fields=reader.fieldnames or []
        if not fields or len(fields)>32 or len(fields)!=len(set(fields)) or any(not NAME.fullmatch(k) or SENSITIVE.search(k) for k in fields):
            fail("Use 1–32 distinct variable names for CSV headings. Store credentials in workspace variables.")
        rows=[]
        for row in reader:
            if len(rows)>=1000 or None in row or any(v is None or len(v)>2048 for v in row.values()):
                fail("CSV supports up to 1,000 complete rows, with values of up to 2,048 characters.")
            rows.append(row)
        if not rows:
            fail("CSV needs a heading and at least one data row.")
        return rows
    except csv.Error:
        fail("Invalid CSV quoting.")


def stringmap(value, name):
    if not isinstance(value,dict) or len(value)>40 or any(not isinstance(k,str) or not k or len(k)>120 or not isinstance(v,str) or len(v)>2048 or '\n' in k or '\r' in k for k,v in value.items()):
        fail(f"{name} must be an object containing short text keys and values.")
    return value


def validate_options(options, url):
    from .forms import check_url
    from .models import NetworkProfile
    if not isinstance(options,dict) or not set(options)<=KEYS:
        fail("Unknown scenario setting.")
    out={}
    for k,(lo,hi,default) in LIMITS.items():
        if k=="timeout_ms" and k not in options:
            from .models import WorkspaceSettings
            default=WorkspaceSettings.current().timeout_ms
        v=options.get(k,default)
        if v is not None:
            out[k]=integer(v,lo,hi,k)
    out["model"]=options.get("model","arrival")
    if out["model"] not in ("arrival","users"):
        fail("Choose an arrival-rate or virtual-user test.")
    env=options.get("environment","")
    if not isinstance(env,str) or len(env)>80:
        fail("Environment must be 80 characters or less.")
    from .probes import interpolate, workspace_variables
    variables = None
    def approved_template(target):
        nonlocal variables
        if not isinstance(target,str) or len(target)>2048: fail("Invalid target template.")
        # Authorize the configured scheme and host using the environment, even when the
        # path/query value comes from a later extraction or fixture row.
        if target.startswith("{{") or re.search(r"://[^/?#]*\{\{",target):
            if variables is None: variables=workspace_variables(env)
            if target.startswith("{{"):
                m=REF.match(target)
                if not m: fail("Invalid endpoint variable.")
                target=interpolate(m.group(),variables)+target[m.end():]
            try: host=urlsplit(target).netloc
            except ValueError: fail("Invalid target template.")
            if REF.search(host):
                target=target.replace("://"+host,"://"+interpolate(host,variables),1)
        check_url(REF.sub("example",target),test=True)
    approved_template(url)
    out["environment"]=env
    keep=options.get("keep_alive", True)
    if type(keep) is not bool:
        fail("Keep-alive takes true or false.")
    out["keep_alive"]=keep
    out["csv_order"]=options.get("csv_order","sequential")
    if out["csv_order"] not in ("sequential","random"): fail("Choose sequential or random CSV rows.")
    out["daily_at"]=options.get("daily_at","")
    if not isinstance(out["daily_at"],str) or (out["daily_at"] and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]",out["daily_at"])):
        fail("Use HH:MM in UTC for daily schedules.")
    out["min_rps"]=options.get("min_rps",0)
    if type(out["min_rps"]) not in (int,float) or not math.isfinite(out["min_rps"]) or not 0<=out["min_rps"]<=50: fail("Set minimum sustained throughput between 0 and 50 requests/s.")
    out["statuses"]=statuses(options.get("statuses"))
    profiles=options.get("profiles",[])
    if not isinstance(profiles,list) or len(profiles)>8 or any(type(p) is not int or p<=0 for p in profiles) or len(set(profiles))!=len(profiles):
        fail("Choose up to eight different network profiles.")
    if profiles and NetworkProfile.objects.filter(pk__in=profiles).count()!=len(profiles):
        fail("A selected network profile no longer exists.")
    out["profiles"]=profiles
    steps=options.get("steps",[]) or []
    if not isinstance(steps,list) or len(steps)>12:
        fail("Use an array of up to 12 scenario steps.")
    names=set()
    normalized=[]
    for n,step in enumerate(steps,1):
        if not isinstance(step,dict) or set(step)-{"name","url","method","headers","query","body","auth","extract","expected_status","expected_statuses"}:
            fail(f"Step {n}: unknown request option.")
        label=step.get("name",f"Step {n}")
        if not isinstance(label,str) or not label.strip() or len(label)>64 or label in names:
            fail("Give each step its own short name.")
        names.add(label)
        target=step.get("url",url)
        if not isinstance(target,str) or len(target)>2048:
            fail("Each step needs a valid URL.")
        approved_template(target)
        method=step.get("method","GET")
        if method not in ("GET","HEAD","OPTIONS","POST","PUT","PATCH","DELETE"):
            fail("Unsupported request method.")
        headers=stringmap(step.get("headers",{}),"Headers")
        query=stringmap(step.get("query",{}),"Query")
        for key,value in [*headers.items(),*query.items()]:
            if SENSITIVE.search(key) and not REF.search(value):
                fail("Reference a workspace variable for credentials, e.g. Bearer {{token}}.")
        auth=stringmap(step.get("auth",{}),"Authentication")
        if auth and (auth.get("type") not in ("bearer","basic","api_key") or set(auth)-{"type","token","username","password","name","value","in"}):
            fail("Invalid authentication settings.")
        if auth:
            required={"bearer":{"token"},"basic":{"username","password"},"api_key":{"name","value"}}[auth["type"]]
            if not required <= auth.keys() or (auth["type"]=="api_key" and auth.get("in","header") not in ("header","query")):
                fail("Complete the required authentication values and choose header or query for an API key.")
        for key in ("token","password","value"):
            if key in auth and not REF.fullmatch(auth[key]):
                fail("Use a variable reference for authentication values.")
        body=step.get("body","")
        if not isinstance(body,str) or len(body.encode())>16384:
            fail("Step bodies must be text of at most 16 KiB.")
        extract=stringmap(step.get("extract",{}),"Extraction")
        if any(not NAME.fullmatch(k) or not re.fullmatch(r"(?:json:[A-Za-z0-9_.-]+|header:[A-Za-z0-9_-]+)",v) for k,v in extract.items()):
            fail("Use json:field.path or header:Header-Name to extract a value.")
        normal=dict(name=label,url=target,method=method,headers=headers,query=query,auth=auth,body=body,extract=extract)
        if "expected_status" in step:
            normal["expected_status"]=integer(step["expected_status"],100,599,"Expected HTTP status")
        if "expected_statuses" in step:
            if "expected_status" in step: fail("Choose one accepted-status field per step.")
            normal["expected_statuses"]=statuses(step["expected_statuses"])
        normalized.append(normal)
    out["steps"]=normalized
    value=options.get("csv_data","")
    dataset(value)
    out["csv_data"]=value
    stages=options.get("stages",[])
    if not isinstance(stages,list) or len(stages)>100: fail("Use at most 100 load stages.")
    for s in stages:
        if not isinstance(s,dict) or set(s)!={"seconds","target"}: fail("Each stage needs seconds and target.")
        integer(s["seconds"],1,3600,"Stage duration")
        integer(s["target"],1,20 if out["model"]=="users" else 50,"Stage target")
    out["stages"]=stages
    return out


def validate_budget(options, pattern, rate, seconds):
    integer(rate,1,50,"Rate")
    integer(seconds,1,3600,"Duration")
    rounds=max(1,len(options.get("profiles",[])))
    steps=max(1,len(options.get("steps",[])))
    stages=options.get("stages",[])
    if stages and (sum(s["seconds"] for s in stages)!=seconds or pattern=="latency"):
        fail("Stage durations must add up to the load test duration.")
    shape={"start_load","step_size","stage_seconds"} & options.keys()
    if shape and (len(shape)!=3 or stages or pattern not in ("ramp","stress")):
        fail("For a stepped ramp or stress test set start load, step size and stage duration together, without custom stages.")
    if shape and options["start_load"]>(options.get("users",5) if options["model"]=="users" else rate):
        fail("The starting load exceeds the target.")
    if pattern=="latency":
        total=options.get("count",seconds)*steps*rounds
    elif options["model"]=="arrival":
        total=(sum(s["target"]*s["seconds"] for s in stages) if stages else rate*seconds)*steps*rounds
    else:
        total=0  # A runtime budget bounds closed-loop traffic, independent of response time.
    if total>MAX_SAMPLES: fail("Limit a run to 30,000 requests across all steps and profiles.")
    if seconds*rounds>14400: fail("Combined profile duration must be at most four hours.")
