"""Measurements used by live charts, release gates and downloadable reports."""
from collections import Counter, defaultdict
from itertools import groupby
from math import ceil, sqrt

from .benchconfig import PHASES


def quantile(values, p):
    if not values:
        return None
    return round(values[ceil(len(values) * p) - 1], 2)


def reason(row):
    if len(row) > 5:
        return row[5]
    return "" if 200 <= row[2] < 400 else ("HTTP " + str(row[2]) if row[2] else "Transport")


def metadata(row):
    return row[9] if len(row) > 9 and isinstance(row[9], dict) else {}


def answered(row):
    return metadata(row).get("answered", bool(row[2]))


def summarize(samples, duration, dropped=0):
    times = sorted(row[1] for row in samples if answered(row))
    codes = Counter(str(row[2]) if row[2] else "network" for row in samples)
    errors = sum(bool(reason(row)) for row in samples)
    average = sum(times) / len(times) if times else None
    deviation = sqrt(sum((x - average) ** 2 for x in times) / len(times)) if times else None
    return {
        "count": len(samples), "answered": len(times), "errors": errors, "dropped": dropped,
        "rps": round(len(samples) / max(duration, .001), 2),
        "minimum": min(times, default=None), "maximum": max(times, default=None),
        "mean": round(average, 2) if times else None, "stddev": round(deviation, 2) if times else None,
        "p50": quantile(times, .50), "p90": quantile(times, .90),
        "p95": quantile(times, .95), "p99": quantile(times, .99),
        "error_pct": round(100 * errors / len(samples), 4) if samples else None, "codes": dict(codes),
    }


def diagnostics(rows):
    completed = [r for r in rows if answered(r)]
    edges = (5,10,25,50,100,250,500,1000,2500,5000,10000,float("inf"))
    hist = [0] * len(edges)
    phases = Counter()
    for row in completed:
        phases.update(row[6])
        bucket = next(i for i,edge in enumerate(edges) if row[1] <= edge)
        hist[bucket] += 1
    return {
        "phases": {k: round(phases[k] / len(completed), 2) if completed else None for k in PHASES},
        "failures": dict(Counter(reason(r) for r in rows if reason(r))),
        "histogram": [
            {"label": f"≤ {edge} ms" if edge != float("inf") else "> 10000 ms",
             "count": n, "width": round(100 * n / max(1, max(hist)))}
            for edge,n in zip(edges, hist)
        ],
        "reused": sum(metadata(r).get("reused") is True for r in completed),
        "compressed": sum(metadata(r).get("compressed") is True for r in completed),
        "received": sum(r[8] for r in rows), "sent": sum(r[7] for r in rows),
    }


def full_report(samples, duration, dropped, series, profile_times):
    report = summarize(samples, duration, dropped)
    steps, profiles = defaultdict(list), defaultdict(list)
    completions = Counter()
    for row in samples:
        steps[row[3]].append(row)
        profiles[row[4]].append(row)
        completions[int(row[0] + row[1] / 1000)] += 1
    report.update(diagnostics(samples))
    report["steps"] = [dict(label=k, **summarize(rows, duration)) for k,rows in steps.items()]
    report["profiles"] = [dict(label=k, **summarize(profiles[k], t), **diagnostics(profiles[k]))
                          for k,t in profile_times.items()]
    report["peak_rps"] = max(completions.values(), default=0)
    ticks = [x["rps"] for x in series if x.get("window", 0) >= .95]
    steady = ticks[1:-1] if len(ticks) > 2 else ticks
    report["steady_rps"] = quantile(sorted(steady), .5)
    report["duration"] = round(duration, 3)
    return report


def pace(pattern, peak, second, seconds):
    fraction = min(1, second / max(1, seconds - 1))
    if pattern in ("ramp", "stress"):
        return max(1, ceil(peak * (.15 + .85 * fraction)))
    if pattern == "spike":
        return peak if .35 <= fraction <= .65 else max(1, peak // 4)
    return 1 if pattern == "latency" else peak


def stage_plan(cfg, options):
    """Compile each workload into stages with a fixed target and duration."""
    if options.get("stages"):
        return options["stages"]
    peak = options["users"] if options["model"] == "users" else cfg["rate"]
    seconds, pattern = cfg["seconds"], cfg["pattern"]
    if pattern == "spike" and options.get("base_seconds"):
        return [
            {"target": options["base_load"], "seconds": options["base_seconds"]},
            {"target": peak, "seconds": options["peak_seconds"]},
            {"target": options["base_load"], "seconds": options["recovery_seconds"]},
        ]
    if options.get("step_size"):
        start, step, width = (options[k] for k in ("start_load", "step_size", "stage_seconds"))
        if options.get("hold_seconds") or options.get("stress_to_max"):
            return [dict(target=x, seconds=width) for x in range(start, peak, step)] + [
                dict(target=peak, seconds=options.get("hold_seconds", width))]
        levels = (min(peak, start + step * (s // width)) for s in range(seconds))
    else:
        levels = (pace(pattern, peak, s, seconds) for s in range(seconds))
    return [dict(target=level, seconds=sum(1 for _ in group)) for level,group in groupby(levels)]


def target(cfg, options, second):
    plan = stage_plan(cfg, options)
    for stage in plan:
        if second < stage["seconds"]:
            return stage["target"]
        second -= stage["seconds"]
    return plan[-1]["target"]


def gate_checks(metrics, config, options):
    scopes = metrics["profiles"] if config["pattern"] == "latency" else [metrics]
    checks = []
    for scope in scopes:
        for key, label, limit in (
            ("mean", "Mean latency (ms)", options.get("max_avg_ms")),
            ("p95", "p95 latency (ms)", config["max_p95"]),
            ("p99", "p99 latency (ms)", options.get("max_p99_ms")),
        ):
            if limit:
                actual = scope[key]
                checks.append(dict(label=label, profile=scope.get("label"), limit=limit, actual=actual,
                                   passed=actual is not None and actual <= limit))
        count = scope["count"]
        checks.append(dict(label="Error rate (%)", profile=scope.get("label"), limit=config["max_error_pct"],
                           actual=scope["error_pct"], passed=bool(count) and scope["errors"]*100<=config["max_error_pct"]*count))
    if options.get("min_rps"):
        checks.append(dict(label="Minimum throughput (req/s)", profile=None, limit=options["min_rps"],
                           actual=metrics["rps"], passed=metrics["count"] / max(.001, metrics["duration"]) >= options["min_rps"]))
    return checks
