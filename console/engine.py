"""HTTP measurements for operator-configured monitors and bounded benchmarks."""
import asyncio
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import time
from collections import Counter
from datetime import timedelta
import httpx
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .forms import check_url
from .models import Check, Exercise, Incident, Maintenance, Monitor, Run

log = logging.getLogger(__name__)

def enqueue(exercise):
    check_url(exercise.url, test=True)
    fields = ("url","pattern","rate","seconds","max_p95","max_error_pct")
    return Run.objects.create(exercise=exercise, config={f:getattr(exercise,f) for f in fields})

def notify(monitor, state):
    if Maintenance.objects.filter(monitor=monitor,start__lte=timezone.now(),end__gt=timezone.now()).exists():
        return
    address = os.environ.get("PYPULSE_ALERT_WEBHOOK")
    if not address:
        return
    body = json.dumps({"monitor":monitor.name,"state":state,"at":timezone.now().isoformat()}).encode()
    headers={"Content-Type":"application/json"}
    secret=os.environ.get("PYPULSE_ALERT_SECRET")
    if secret:
        headers["X-PyPulse-Signature"]=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    try:
        response=httpx.post(address,content=body,headers=headers,timeout=5,follow_redirects=False)
        response.raise_for_status()
    except httpx.HTTPError:
        log.warning("Alert delivery failed for monitor %s",monitor.pk)

def check_monitor(pk):
    monitor=Monitor.objects.get(pk=pk)
    check_url(monitor.url)
    start=time.perf_counter()
    status=None
    ok=False
    try:
        with httpx.stream("GET",monitor.url,timeout=5,follow_redirects=False,trust_env=False) as response:
            status=response.status_code
            sample=bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                sample.extend(chunk)
                if len(sample)>=65536:
                    break
            ok=status==monitor.expected_status and monitor.contains.encode() in sample
    except httpx.HTTPError:
        pass
    ms=(time.perf_counter()-start)*1000
    with transaction.atomic():
        monitor=Monitor.objects.get(pk=pk)
        previous=monitor.state
        Check.objects.create(monitor=monitor,ms=ms,status=status,ok=ok)
        monitor.failures=0 if ok else monitor.failures+1
        if ok:
            monitor.state="Slow" if monitor.warn_ms and ms>monitor.warn_ms else "Up"
        elif monitor.failures>=monitor.failure_threshold:
            monitor.state="Down"
        monitor.save(update_fields=["state","failures"])
        if monitor.state=="Down" and previous!="Down":
            Incident.objects.create(monitor=monitor)
        elif ok and previous=="Down":
            Incident.objects.filter(monitor=monitor,closed=None).update(closed=timezone.now())
    if previous!=monitor.state and (monitor.state in ("Down","Slow") or previous in ("Down","Slow")):
        notify(monitor,monitor.state)

def quantile(values,p):
    return round(values[max(0,math.ceil(len(values)*p)-1)],2) if values else None

def summarize(samples, duration, dropped):
    times=sorted(row[1] for row in samples)
    codes=Counter(str(row[2]) if row[2] else "network" for row in samples)
    errors=sum(not 200 <= row[2] < 400 for row in samples)
    return {"count":len(samples),"errors":errors,"dropped":dropped,"rps":round(len(samples)/max(duration,.001),2),
            "p50":quantile(times,.5),"p95":quantile(times,.95),"p99":quantile(times,.99),
            "error_pct":round(100*errors/len(samples),2) if samples else None,"codes":dict(codes)}

def pace(pattern,peak,second,seconds):
    fraction=second/max(1,seconds-1)
    if pattern in ("ramp","stress"):
        return max(1,math.ceil(peak*(0.15+0.85*fraction)))
    if pattern=="spike":
        return peak if .35<=fraction<=.65 else max(1,peak//4)
    if pattern=="latency":
        return 1
    return peak

async def measure(client,url,origin,records):
    start=time.perf_counter()
    status=0
    try:
        async with asyncio.timeout(6):
            async with client.stream("GET",url) as res:
                n=0
                async for chunk in res.aiter_raw():
                    n+=len(chunk)
                    if n>4*1024*1024:
                        raise ValueError("Response exceeds limit")
                status=res.status_code
    except (httpx.HTTPError,TimeoutError,ValueError):
        pass
    records.append([round(start-origin,3),round((time.perf_counter()-start)*1000,3),status])

async def benchmark(run):
    cfg=run.config
    check_url(cfg["url"],test=True)
    samples, series, tasks = [], [], set()
    dropped=0
    state="Completed"
    start=time.perf_counter()
    async with httpx.AsyncClient(timeout=5,follow_redirects=False,trust_env=False) as client:
        for second in range(cfg["seconds"]):
            if await Run.objects.filter(pk=run.pk,stop=True).aexists():
                state="Stopped"
                break
            rate=pace(cfg["pattern"],cfg["rate"],second,cfg["seconds"])
            for i in range(rate):
                target=start+second+i/rate
                await asyncio.sleep(max(0,target-time.perf_counter()))
                if len(tasks)>=20 or time.perf_counter()>target+1/rate:
                    dropped+=1
                    continue
                task=asyncio.create_task(measure(client,cfg["url"],start,samples))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            await asyncio.sleep(max(0,start+second+1-time.perf_counter()))
            recent=[s for s in samples if second<=s[0]+s[1]/1000<second+1]
            tick=summarize(recent,1,0)
            series.append({"second":second+1,"count":tick["count"],"p95":tick["p95"],"errors":tick["errors"]})
            live=summarize(samples,time.perf_counter()-start,dropped)
            await Run.objects.filter(pk=run.pk).aupdate(result=live,series=series)
            if cfg["pattern"]=="stress" and recent and (tick["error_pct"]>cfg["max_error_pct"] or (cfg["max_p95"] and tick["p95"]>cfg["max_p95"])):
                state="Threshold reached"
                break
        if tasks:
            await asyncio.gather(*list(tasks))
    result=summarize(samples,time.perf_counter()-start,dropped)
    result["passed"]=bool(samples) and state=="Completed" and not dropped and result["error_pct"]<=cfg["max_error_pct"] and (not cfg["max_p95"] or result["p95"]<=cfg["max_p95"])
    await Run.objects.filter(pk=run.pk).aupdate(result=result,series=series,samples=samples,state=state,finished=timezone.now())
