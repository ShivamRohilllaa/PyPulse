"""Single-worker benchmarks against explicitly approved endpoints."""
import asyncio
import json
import os
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from itertools import groupby
from urllib.parse import urlsplit

import psutil
import regex
from asgiref.sync import sync_to_async
from django.utils import timezone

from .benchconfig import MAX_SAMPLES, dataset, validate_budget, validate_options
from .benchstats import full_report, gate_checks, stage_plan, summarize
from .models import NetworkProfile, Run
from .probes import probe, seal, unseal, workspace_variables
from .uptime_rules import lookup

DIRECT = {"name":"Direct","latency_ms":0,"jitter_ms":0,"loss_pct":0,"upload_kbps":0,"download_kbps":0}


def freeze(exercise):
    options = validate_options(exercise.options or {}, exercise.url)
    validate_budget(options, exercise.pattern, exercise.rate, exercise.seconds)
    items = {p.pk: p.snapshot() for p in NetworkProfile.objects.filter(pk__in=options["profiles"])}
    profiles = [items[i] for i in options["profiles"]] or [DIRECT]
    parts = urlsplit(exercise.url)
    config = {
        "version": 3, "url": "Workspace endpoint" if "{{" in exercise.url else f"{parts.scheme}://{parts.netloc}",
        "model": options["model"], "profiles": [p["name"] for p in profiles],
        "environment": options["environment"], "step_count": max(1, len(options["steps"])),
        **{k:getattr(exercise,k) for k in ("rate","pattern","seconds","max_p95","max_error_pct")},
        "plan": seal(json.dumps({"options":options,"profiles":profiles,"url":exercise.url})),
    }
    stages = stage_plan(config, options) if exercise.pattern != "latency" else []
    if stages:
        config["seconds"] = sum(s["seconds"] for s in stages)
    codes = options["statuses"] or list(range(200, 400))
    ranges = []
    for _, members in groupby(enumerate(codes), key=lambda pair: pair[1] - pair[0]):
        values = [pair[1] for pair in members]
        ranges.append(str(values[0]) if len(values)==1 else f"{values[0]}–{values[-1]}")
    steps = options["steps"] or [{"name":"Endpoint","method":"GET"}]
    config["settings"] = {
        "concurrency":options["users"], "delay_ms":options["delay_ms"], "timeout_ms":options["timeout_ms"],
        "keep_alive":options["keep_alive"], "statuses":", ".join(ranges), "stages":stages,
        "steps":[{"name":s["name"],"method":s["method"]} for s in steps],
        "profile_mode":options["profile_mode"] if exercise.pattern == "latency" else "sequential",
        "count": options.get("count", exercise.seconds) if exercise.pattern == "latency" else None,
        "gate_policy":options["gate_policy"],
    }
    return config


def extract(mapping, measurement):
    result, document = {}, None
    for name, selector in mapping.items():
        source, path = selector.split(":", 1)
        if source == "header":
            value = measurement["headers"][path.lower()]
        elif source == "regex":
            match = regex.search(path, measurement["body"].decode("utf-8", errors="replace"), timeout=.025)
            if not match:
                raise ValueError("Pattern did not match")
            value = match.group(1) if match.lastindex else match.group(0)
        else:
            if document is None:
                document = json.loads(measurement["body"])
            value = lookup(document,path.removeprefix("$").removeprefix("."))
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
        if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 4096:
            raise ValueError("Invalid extracted value")
        result[name] = str(value)
    return result


class Driver:
    def __init__(self, run, plan, variables):
        self.run, self.cfg = run, run.config
        self.opt, self.profiles = plan["options"], plan["profiles"]
        self.steps = self.opt["steps"] or [dict(name="Endpoint", url=plan["url"], method="GET")]
        self.variables = variables
        self.rows = dataset(self.opt["csv_data"])
        self.cancel = threading.Event()
        self.lock = asyncio.Lock()
        self.origin = time.perf_counter()
        self.next_send = self.origin
        self.remaining_budget = MAX_SAMPLES
        self.dropped = self.active = self.peak = self.iterations = 0
        self.samples, self.series, self.stages = [], [], []
        self.profile_times = {}
        self.stage_rows = defaultdict(list)
        self.state, self.breakpoint = "Completed", None
        self.profile = self.profiles[0]
        self.plan = stage_plan(self.cfg, self.opt)
        self.stage_number = self.stage_level = 0
        self.stage_start = self.origin
        self.closed_stage = False
        self.flat = 0
        self.best = None
        self.stop_reason = "completed"
        self.latency_test = self.cfg["pattern"] == "latency"
        self.count_per_profile = self.opt.get("count", self.cfg["seconds"])
        self.profile_progress = {
            p["name"]: {"label":p["name"], "done":0, "total":self.count_per_profile, "last":[]}
            for p in self.profiles
        }
        self.row_rng = {p["name"]:random.Random(run.pk) for p in self.profiles}
        self.profile_iterations = defaultdict(int)
        self.host_high = self.loop_high = self.late_ticks = 0
        self.max_cpu = self.max_loop = self.max_delay = 0
        try:
            self.cpu_limit = max(0, min(100, float(os.getenv("PYPULSE_CPU_WARN_PCT","85"))))
        except ValueError:
            self.cpu_limit = 85

    def halt(self, state, reason=None, metrics=None):
        if self.cancel.is_set():
            return
        self.state = state
        self.stop_reason = reason or state
        if reason:
            measured = metrics or {}
            self.breakpoint = {
                "reason":reason, "target":self.stage_level, "stage":self.stage_number,
                "profile":self.profile["name"], "rps":measured.get("rps"), "p95":measured.get("p95"),
                "error_pct":measured.get("error_pct"), "count":measured.get("count"),
            }
        self.cancel.set()

    async def sleep(self, seconds):
        deadline = time.perf_counter() + seconds
        while not self.cancel.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            await asyncio.sleep(min(.1, remaining))

    async def permit(self):
        async with self.lock:
            if self.cancel.is_set():
                return False
            if not self.remaining_budget:
                self.halt("Limit reached", "Request budget reached")
                return False
            await self.sleep(max(0, self.next_send - time.perf_counter()))
            if self.cancel.is_set():
                return False
            self.next_send = time.perf_counter() + .020
            self.remaining_budget -= 1
            return True

    async def iteration(self, profile, pool=None):
        name = profile["name"]
        number = self.profile_iterations[name]
        self.profile_iterations[name] += 1
        self.iterations += 1
        values = dict(self.variables)
        if self.rows:
            row = self.row_rng[name].choice(self.rows) if self.opt.get("csv_order") == "random" else self.rows[number % len(self.rows)]
            values.update(row)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            for step in self.steps:
                if not await self.permit():
                    return
                start = time.perf_counter()
                spec = dict(step, compression=True) if self.latency_test else step
                request = partial(probe, spec, values, profile, timeout_ms=self.opt["timeout_ms"],
                                  keep_alive=self.opt["keep_alive"], allowed=True, cancel=self.cancel)
                item = await asyncio.get_running_loop().run_in_executor(pool, request)
                code = item["status"]
                accepted = step.get("expected_statuses") or self.opt.get("statuses") or range(200,400)
                if "expected_status" in step:
                    accepted = (step["expected_status"],)
                failure = item["error"]
                complete = bool(code) and not failure
                if not failure and code not in accepted:
                    failure = f"HTTP {code}"
                if not failure and step.get("extract"):
                    try:
                        values.update(extract(step["extract"], item))
                    except (ValueError, TypeError, IndexError, KeyError, RecursionError, TimeoutError, regex.error):
                        failure = "Extraction failed"
                row = [round(start-self.origin,4), item["ms"], code, step["name"], name,
                       failure, item["phases"], item["sent"], item["received"],
                       {"stage": self.stage_number or None, "iteration": number+1, "answered": complete,
                        "reused":item.get("reused"), "compressed":item.get("compressed")}]
                self.samples.append(row)
                if self.latency_test:
                    progress = self.profile_progress[name]
                    progress["last"] = (progress["last"] + [row])[-50:]
                else:
                    self.stage_rows[name,self.stage_number].append(row)
                if failure:
                    return
        finally:
            self.active -= 1

    async def latency_profile(self, profile):
        name = profile["name"]
        began = time.perf_counter()
        # Pin each profile to its own curl thread so connection reuse stays comparable.
        with ThreadPoolExecutor(max_workers=1) as pool:
            for index in range(self.count_per_profile):
                if index:
                    await self.sleep(self.opt["delay_ms"] / 1000)
                if self.cancel.is_set():
                    break
                await self.iteration(profile, pool)
                self.profile_progress[name]["done"] += 1
        self.profile_times[name] = time.perf_counter() - began

    def stage_stats(self):
        rows = self.stage_rows[self.profile["name"],self.stage_number]
        elapsed = time.perf_counter() - self.stage_start
        return dict(stage=self.stage_number, target=self.stage_level, profile=self.profile["name"],
                    duration=round(elapsed,3), **summarize(rows, elapsed))

    def stage_violation(self, stats, final=False):
        if self.cfg["pattern"] != "stress" or self.cancel.is_set():
            return None
        independent = self.opt.get("stress_policy") == "independent"
        minimum = 20 if independent else 1
        errors = self.opt.get("stop_error_pct") if independent else self.cfg["max_error_pct"]
        p95 = self.opt.get("stop_p95_ms") if independent else self.cfg["max_p95"]
        if stats["count"] >= minimum and errors is not None and stats["errors"]*100 > stats["count"]*errors:
            return "Error rate exceeded"
        if stats["answered"] >= minimum and p95 and stats["p95"] > p95:
            return "p95 latency exceeded"
        if final and self.best and self.opt.get("plateau_windows") and self.stage_level > self.best["target"]:
            minimum_rps = self.best["rps"] * (1 + self.opt.get("plateau_growth_pct", 0)/100)
            flat = stats["rps"] < minimum_rps if self.opt.get("plateau_growth_pct") else stats["rps"] <= minimum_rps
            self.flat = self.flat + 1 if flat else 0
            if self.flat >= self.opt["plateau_windows"]:
                return "Throughput plateau"
        return None

    def finish_stage(self, interrupted=False):
        stats = self.stage_stats()
        violation = self.stage_violation(stats, final=True)
        if violation:
            self.halt("Threshold reached", violation, stats)
        if self.breakpoint and self.breakpoint["stage"] == self.stage_number and self.breakpoint["profile"]==self.profile["name"]:
            violation = self.breakpoint["reason"]
        stats["passed"] = not interrupted and not violation
        stats["violation"] = violation
        self.stages.append(stats)
        if stats["passed"] and stats["count"] and (not self.best or stats["rps"] > self.best["rps"]):
            self.best = stats
        self.closed_stage = True

    @staticmethod
    def trim_jobs(jobs):
        active = set()
        for job in jobs:
            if job.done():
                job.result()
            else:
                active.add(job)
        return active

    async def load_profile(self, profile):
        self.profile = profile
        began = time.perf_counter()
        end = began + sum(s["seconds"] for s in self.plan)
        jobs = set()
        self.flat, self.best = 0, None

        async def user_iteration():
            await self.iteration(profile)
            think = min(self.opt["delay_ms"]/1000, end - time.perf_counter())
            if think > 0:
                await self.sleep(think)

        try:
            for index, stage in enumerate(self.plan, 1):
                if self.cancel.is_set():
                    break
                self.stage_number, self.stage_level = index, stage["target"]
                self.stage_start = time.perf_counter()
                self.closed_stage = False
                deadline = self.stage_start + stage["seconds"]
                if self.opt["model"] == "users":
                    while time.perf_counter() < deadline and not self.cancel.is_set():
                        jobs = self.trim_jobs(jobs)
                        for _ in range(max(0, self.stage_level - len(jobs))):
                            jobs.add(asyncio.create_task(user_iteration()))
                        await self.sleep(min(.01, deadline-time.perf_counter()))
                else:
                    for arrival in range(stage["seconds"] * self.stage_level):
                        launch = self.stage_start + arrival/self.stage_level
                        await self.sleep(max(0, launch-time.perf_counter()))
                        if self.cancel.is_set():
                            break
                        jobs = self.trim_jobs(jobs)
                        if len(jobs)>=self.opt["users"] or time.perf_counter()>launch+max(.05,1/self.stage_level):
                            self.dropped += 1
                        else:
                            jobs.add(asyncio.create_task(self.iteration(profile)))
                    await self.sleep(max(0, deadline - time.perf_counter()))
                if index == len(self.plan) or self.cancel.is_set():
                    break
                self.finish_stage()
        finally:
            if jobs:
                await asyncio.gather(*jobs)
            if self.stage_number and not self.closed_stage:
                self.finish_stage(interrupted=self.cancel.is_set())
            elif self.closed_stage and self.cancel.is_set() and self.stages:
                # Retain the stop-decision sample in the breakpoint and count later in-flight completions in the stage.
                self.stages[-1].update(self.stage_stats())
            self.profile_times[profile["name"]] = time.perf_counter() - began

    async def drive(self):
        if self.latency_test and self.opt.get("profile_mode","sequential") == "parallel":
            await asyncio.gather(*(self.latency_profile(p) for p in self.profiles))
            return
        for profile in self.profiles:
            if self.cancel.is_set():
                break
            if self.latency_test:
                await self.latency_profile(profile)
            else:
                await self.load_profile(profile)

    def public_progress(self):
        return [self.profile_progress[p["name"]] for p in self.profiles]

    async def watch(self, done):
        cursor = 0
        start = self.origin
        thread_start = time.thread_time()
        psutil.cpu_percent()
        max_lag = 0
        last_progress = start
        while True:
            wake = time.perf_counter() + .2
            try:
                await asyncio.wait_for(done.wait(),timeout=.2)
            except asyncio.TimeoutError:
                max_lag = max(max_lag, 1000*(time.perf_counter()-wake))
            if await Run.objects.filter(pk=self.run.pk,stop=True).aexists():
                self.halt("Stopped")
            now = time.perf_counter()
            if now-self.origin >= 14400:
                self.halt("Limit reached","Four-hour worker budget")
            finished = done.is_set()
            if now-start < 1 and not finished:
                if self.latency_test and now-last_progress >= .35:
                    live=summarize(self.samples, now-self.origin, self.dropped)
                    live.update(progress=self.public_progress(), active=self.active)
                    await Run.objects.filter(pk=self.run.pk).aupdate(result=live)
                    last_progress=now
                continue
            elapsed = max(.001, now-start)
            tick = summarize(self.samples[cursor:], elapsed)
            cpu = psutil.cpu_percent()
            thread_now = time.thread_time()
            loop_cpu = min(100, 100*(thread_now-thread_start)/elapsed)
            if elapsed >= .9:
                self.host_high += cpu>self.cpu_limit
                self.loop_high += loop_cpu>90
                self.late_ticks += max_lag>200
            self.max_cpu = max(self.max_cpu,cpu)
            self.max_loop = max(self.max_loop,loop_cpu)
            self.max_delay = max(self.max_delay,max_lag)
            tick.update(second=round(now-self.origin,2), window=round(elapsed,3), profile=self.profile["name"] if not self.latency_test else None,
                        target=self.stage_level, stage=self.stage_number, active=self.peak, inflight=self.active,
                        host_cpu_pct=cpu, loop_cpu_pct=round(loop_cpu,2), loop_lag_ms=round(max_lag,2))
            if tick["count"] or elapsed>=.95:
                self.series.append(tick)
            self.peak=self.active
            if self.cfg["pattern"] == "stress" and self.stage_number and not self.closed_stage:
                if self.opt.get("stress_policy")!="independent" or now-self.stage_start>=3:
                    stats = self.stage_stats()
                    violation = self.stage_violation(stats)
                    if violation:
                        self.halt("Threshold reached", violation, stats)
            live = summarize(self.samples,now-self.origin,self.dropped)
            live.update(active=self.active, host_cpu_pct=cpu, loop_lag_ms=round(max_lag,2), stages=self.stages)
            if self.latency_test:
                live["progress"] = self.public_progress()
            cursor,start,thread_start,max_lag=len(self.samples),now,thread_now,0
            await Run.objects.filter(pk=self.run.pk).aupdate(result=live, series=self.series)
            if finished:
                break

    async def execute(self):
        if await Run.objects.filter(pk=self.run.pk,stop=True).aexists():
            self.halt("Stopped")
        done = asyncio.Event()
        watcher = asyncio.create_task(self.watch(done))
        try:
            if not self.cancel.is_set():
                await self.drive()
        finally:
            done.set()
            await watcher
        self.samples.sort(key=lambda row:row[0])
        duration = time.perf_counter()-self.origin
        # Preserve configured profile order when slow and fast profiles finish out of order.
        durations = {p["name"]:self.profile_times[p["name"]] for p in self.profiles if p["name"] in self.profile_times}
        result = full_report(self.samples, duration, self.dropped, self.series, durations)
        assess = self.opt.get("gate_policy","evaluate") == "evaluate"
        checks = gate_checks(result,self.cfg,self.opt) if assess else []
        reasons = []
        if self.state != "Completed":
            reasons.append(f"Run ended with status {self.state}.")
        if not result["count"]:
            reasons.append("No requests were measured.")
        for c in checks:
            if not c["passed"]:
                prefix = f'{c["profile"]} · ' if c["profile"] else ""
                reasons.append(f'{prefix}{c["label"]} missed its limit of {c["limit"]} (measured {c["actual"]}).')
        if self.dropped:
            reasons.append(f"The worker dropped {self.dropped} scheduled iterations.")
        warnings = []
        if self.dropped:
            warnings.append(f"{self.dropped} iterations missed their arrival time or concurrency slot.")
        if self.state == "Limit reached":
            warnings.append("The worker reached its configured request or time limit.")
        if self.host_high >= 2:
            warnings.append(f"Host CPU exceeded {self.cpu_limit:g}% in {self.host_high} reporting windows.")
        if self.loop_high >= 2 or self.late_ticks >= 2:
            warnings.append("The worker event loop showed sustained CPU pressure or late wakeups.")
        passed_stages = [s for s in self.stages if s["passed"] and s["count"]]
        sustainable = max(passed_stages,key=lambda s:s["rps"],default=None) if self.cfg["pattern"]=="stress" else None
        if self.state=="Completed" and self.cfg["pattern"]=="stress" and self.stages and self.stages[-1]["target"] == (
            self.opt["users"] if self.opt["model"]=="users" else self.cfg["rate"]):
            self.stop_reason = "Maximum configured load reached"
        result.update(
            passed=(not reasons) if assess else None, gate_reasons=reasons if assess else [], checks=checks, generator_warning=" ".join(warnings),
            stages=self.stages, breakpoint=self.breakpoint, max_sustainable=sustainable,
            stop_reason=self.stop_reason, iterations=self.iterations,
            generator={"host_cpu_peak_pct":self.max_cpu, "loop_cpu_peak_pct":round(self.max_loop,2),
                       "loop_lag_peak_ms":round(self.max_delay,2), "cpu_warn_pct":self.cpu_limit},
            gates={"minimum_rps":self.opt.get("min_rps",0), "maximum_p95":self.cfg["max_p95"],
                   "maximum_avg":self.opt.get("max_avg_ms"),"maximum_p99":self.opt.get("max_p99_ms"),
                   "maximum_error_pct":self.cfg["max_error_pct"]} if assess else {},
        )
        await Run.objects.filter(pk=self.run.pk).aupdate(state=self.state,result=result,series=self.series,
            samples=self.samples,finished=timezone.now())


async def benchmark(run):
    if "plan" in run.config:
        plan = json.loads(unseal(run.config["plan"]))
    else:
        options = await sync_to_async(validate_options)({},run.config["url"])
        plan = {"url":run.config["url"], "options":options, "profiles":[DIRECT]}
    variables = await sync_to_async(workspace_variables)(plan["options"]["environment"])
    await Driver(run, plan, variables).execute()
