"""Single-worker performance tests against operator-approved endpoints."""
import asyncio
import json
import random
import threading
import time
from urllib.parse import urlsplit
from asgiref.sync import sync_to_async
from django.utils import timezone
from .benchconfig import MAX_SAMPLES, dataset, validate_budget, validate_options
from .benchstats import full_report, summarize, target
from .models import NetworkProfile, Run
from .probes import probe, seal, unseal, workspace_variables

DIRECT = {"name":"Direct","latency_ms":0,"jitter_ms":0,"loss_pct":0,"upload_kbps":0,"download_kbps":0}


def freeze(exercise):
    options=validate_options(exercise.options or {},exercise.url)
    validate_budget(options,exercise.pattern,exercise.rate,exercise.seconds)
    items={p.pk:p.snapshot() for p in NetworkProfile.objects.filter(pk__in=options["profiles"])}
    profiles=[items[i] for i in options["profiles"]] or [DIRECT]
    profile_names=[p["name"] for p in profiles]
    env=options.get("environment","")
    parts=urlsplit(exercise.url)
    # Raw fixtures, headers, query strings and request bodies are never public run metadata.
    return {"version":2,"url":"Workspace endpoint" if "{{" in exercise.url else f"{parts.scheme}://{parts.netloc}", "model":options["model"],
        "profiles":profile_names,"environment":env,"step_count":max(1,len(options["steps"])),
        **{k:getattr(exercise,k) for k in ("rate","pattern","seconds","max_p95","max_error_pct")},
        "plan":seal(json.dumps({"options":options,"profiles":profiles,"url":exercise.url}))}


def extract(mapping,measurement):
    result={}
    document=None
    for name,selector in mapping.items():
        source,path=selector.split(":",1)
        if source=="header":
            value=measurement["headers"][path.lower()]
        else:
            if document is None: document=json.loads(measurement["body"])
            value=document
            for part in path.split("."):
                value=value[int(part)] if isinstance(value,list) and part.isdecimal() else value[part]
        if not isinstance(value,(str,int,float,bool)) or len(str(value))>4096:
            raise ValueError("Extraction is not a scalar")
        result[name]=str(value)
    return result


class Driver:
    def __init__(self,run,plan,variables):
        self.run,self.cfg=run,run.config
        self.opt,self.profiles=plan["options"],plan["profiles"]
        self.steps=self.opt["steps"] or [dict(name="Endpoint",url=plan["url"],method="GET")]
        self.variables=variables
        self.rows=dataset(self.opt["csv_data"])
        self.cancel=threading.Event()
        self.lock=asyncio.Lock()
        self.origin=time.perf_counter()
        self.next_send=self.origin
        self.serial=self.count=self.dropped=self.active=self.peak=0
        self.samples,self.series,self.profile_times=[],[],{}
        self.state,self.breakpoint="Completed",None
        self.profile=self.profiles[0]
        self.profile_start=self.origin
        self.profile_index=0
        self.stage_target=self.stage_ticks=self.stage_count=self.flat=0
        self.stage_rps=None
        self.remaining_budget=MAX_SAMPLES

    def halt(self,state,reason=None,level=None):
        if self.cancel.is_set(): return
        self.state=state
        if reason: self.breakpoint={"reason":reason,"target":level,"profile":self.profile["name"]}
        self.cancel.set()

    async def sleep(self,delay):
        # asyncio polling bounds cancellation latency even for long inter-iteration delays.
        end=time.perf_counter()+delay
        while not self.cancel.is_set():
            left=end-time.perf_counter()
            if left<=0: break
            await asyncio.sleep(min(.10,left))

    async def permit(self):
        # This applies to actual requests, including every step of a closed-loop iteration.
        async with self.lock:
            if self.cancel.is_set(): return False
            if self.remaining_budget<=0:
                self.halt("Limit reached","Request budget reached",self.level())
                return False
            await self.sleep(max(0,self.next_send-time.perf_counter()))
            if self.cancel.is_set(): return False
            self.next_send=time.perf_counter()+.020
            self.remaining_budget-=1
            return True

    def level(self,second=None):
        if second is None: second=time.perf_counter()-self.profile_start
        return target(self.cfg,self.opt,min(self.cfg["seconds"]-1,max(0,int(second))))

    async def iteration(self):
        number=self.serial
        self.serial+=1
        values=dict(self.variables)
        if self.rows:
            values.update(self.random.choice(self.rows) if self.opt.get("csv_order")=="random" else self.rows[number % len(self.rows)])
        self.active+=1
        self.peak=max(self.peak,self.active)
        try:
            for step in self.steps:
                if not await self.permit(): return
                start=time.perf_counter()
                item=await asyncio.to_thread(probe, step, values, self.profile, timeout_ms=self.opt["timeout_ms"],
                    keep_alive=self.opt["keep_alive"], allowed=True, cancel=self.cancel)
                code=item["status"]
                accepted=step.get("expected_statuses") or self.opt.get("statuses") or range(200,400)
                if "expected_status" in step: accepted=(step["expected_status"],)
                reason=item["error"]
                if not reason and code not in accepted:
                    reason="HTTP "+str(code)
                if not reason and step.get("extract"):
                    try: values.update(extract(step["extract"],item))
                    except (ValueError,TypeError,IndexError,KeyError,RecursionError):
                        reason="Extraction failed"
                self.samples.append([round(start-self.origin,4),item["ms"],code,step["name"],self.profile["name"],
                    reason,item["phases"],item["sent"],item["received"]])
                if reason: return
        finally:
            self.active-=1

    async def load(self):
        pattern=self.cfg["pattern"]
        if pattern=="latency":
            for i in range(self.opt.get("count",self.cfg["seconds"])):
                if self.cancel.is_set(): break
                if i: await self.sleep(self.opt["delay_ms"]/1000)
                await self.iteration()
            return
        seconds=self.cfg["seconds"]
        jobs=set()
        is_users=self.opt["model"]=="users"
        try:
            for sec in range(seconds):
                level=self.level(sec)
                tick=self.profile_start+sec
                if is_users:
                    while not self.cancel.is_set() and time.perf_counter()<tick+1:
                        jobs={j for j in jobs if not self.check_job(j)}
                        async def cycle():
                            await self.iteration()
                            if self.opt["delay_ms"]: await self.sleep(self.opt["delay_ms"]/1000)
                        for _ in range(max(0,level-len(jobs))): jobs.add(asyncio.create_task(cycle()))
                        await self.sleep(min(.01,max(0,tick+1-time.perf_counter())))
                else:
                    for i in range(level):
                        scheduled=tick+i/level
                        await self.sleep(max(0,scheduled-time.perf_counter()))
                        if self.cancel.is_set(): break
                        jobs={j for j in jobs if not self.check_job(j)}
                        if len(jobs)>=self.opt["users"] or time.perf_counter()>scheduled+max(.05,1/level):
                            self.dropped+=1
                        else: jobs.add(asyncio.create_task(self.iteration()))
                    await self.sleep(max(0,tick+1-time.perf_counter()))
                if self.cancel.is_set(): break
        finally:
            if jobs: await asyncio.gather(*jobs)

    @staticmethod
    def check_job(task):
        if task.done(): task.result(); return True
        return False

    def assess(self,tick,level):
        if self.cfg["pattern"]!="stress": return
        if tick["count"]:
            if tick["errors"]*100>self.cfg["max_error_pct"]*tick["count"]:
                self.halt("Threshold reached","Error rate exceeded",level)
            elif self.cfg["max_p95"] and tick["p95"]>self.cfg["max_p95"]:
                self.halt("Threshold reached","p95 latency exceeded",level)
        if level!=self.stage_target:
            self.close_stage()
            self.stage_target,self.stage_count,self.stage_ticks=level,0,0
        self.stage_count+=tick["count"]; self.stage_ticks+=1

    def close_stage(self):
        if not self.stage_ticks: return
        current=self.stage_count/self.stage_ticks
        if self.stage_rps is not None and self.stage_target>self.stage_rps[0]:
            self.flat=self.flat+1 if current<=self.stage_rps[1] else 0
            if self.opt["plateau_windows"] and self.flat>=self.opt["plateau_windows"]:
                self.halt("Threshold reached","Throughput plateau",self.stage_target)
        self.stage_rps=self.stage_target,current

    def tick(self,rows,second,level,elapsed):
        metrics=summarize(rows,elapsed)
        self.series.append({"second":second,"profile":self.profile["name"],"target":level,"active":self.peak,
            **{k:metrics[k] for k in ("count","errors","p50","p95","p99","error_pct")}})
        self.peak=self.active
        self.assess(metrics,level)

    async def watch(self,done):
        cursor=len(self.samples)
        tick_start=self.profile_start
        while not done.is_set():
            try: await asyncio.wait_for(done.wait(),timeout=.20)
            except asyncio.TimeoutError: pass
            if await Run.objects.filter(pk=self.run.pk,stop=True).aexists(): self.halt("Stopped")
            now=time.perf_counter()
            if now-self.origin>=14400: self.halt("Limit reached","Four-hour worker budget",self.level())
            if now-tick_start>=1 or done.is_set():
                new=self.samples[cursor:]
                cursor=len(self.samples)
                elapsed=now-tick_start
                if new or elapsed>=.99:
                    self.tick(new,round(now-self.origin,2),self.level(tick_start-self.profile_start),elapsed)
                tick_start=now
                live=summarize(self.samples,now-self.origin,self.dropped)
                live["active"]=self.active
                await Run.objects.filter(pk=self.run.pk).aupdate(result=live,series=self.series)

    async def execute(self):
        if await Run.objects.filter(pk=self.run.pk,stop=True).aexists(): self.halt("Stopped")
        for profile in self.profiles:
            if self.cancel.is_set(): break
            self.profile=profile
            self.profile_start=time.perf_counter()
            self.random=random.Random(self.run.pk)
            self.serial=self.peak=0  # Identical fixture sequences per profile.
            self.stage_target=self.stage_ticks=self.stage_count=self.flat=0
            self.stage_rps=None
            done=asyncio.Event()
            watcher=asyncio.create_task(self.watch(done))
            try:
                await self.load()
            finally:
                done.set()
                await watcher
            if self.cfg["pattern"]=="stress": self.close_stage()
            self.profile_times[profile["name"]]=time.perf_counter()-self.profile_start
        self.samples.sort(key=lambda row:row[0])
        duration=time.perf_counter()-self.origin
        result=full_report(self.samples,duration,self.dropped,self.series,self.profile_times)
        min_rps = self.opt.get("min_rps", 0)
        reasons = []
        if self.state != "Completed":
            reasons.append(f"Run ended with status {self.state}.")
        if not result["count"]:
            reasons.append("No requests were measured.")
        else:
            if result["errors"] * 100 > self.cfg["max_error_pct"] * result["count"]:
                reasons.append(f"Error rate was {result['error_pct']}%, above the {self.cfg['max_error_pct']}% limit.")
            if self.cfg["max_p95"] and result["p95"] > self.cfg["max_p95"]:
                reasons.append(f"p95 was {result['p95']} ms, above the {self.cfg['max_p95']} ms limit.")
            if min_rps and result["count"] / max(duration, .001) < min_rps:
                reasons.append(f"Throughput was {result['rps']} requests/s, below the {min_rps} minimum.")
        if self.dropped:
            reasons.append(f"The worker dropped {self.dropped} scheduled iterations.")
        result["passed"] = not reasons
        result["gate_reasons"] = reasons
        result["gates"]={"minimum_rps":min_rps,"maximum_p95":self.cfg["max_p95"],"maximum_error_pct":self.cfg["max_error_pct"]}
        result["generator_warning"]=("Some scheduled iterations did not start. The worker reached its concurrency or dispatch limit; this run alone cannot establish the target's maximum throughput." if self.dropped else
            ("The worker reached its request or time budget. The target may support more traffic." if self.state=="Limit reached" else ""))
        result["breakpoint"]=self.breakpoint
        await Run.objects.filter(pk=self.run.pk).aupdate(state=self.state,result=result,series=self.series,
            samples=self.samples,finished=timezone.now())


async def benchmark(run):
    if "plan" in run.config:
        plan=json.loads(unseal(run.config["plan"]))
    else:
        options=await sync_to_async(validate_options)({},run.config["url"])
        plan={"url":run.config["url"],"options":options,"profiles":[DIRECT]}
    variables=await sync_to_async(workspace_variables)(plan["options"]["environment"])
    await Driver(run,plan,variables).execute()
