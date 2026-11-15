"""Compact v2 sample: [start offset, elapsed ms, status, step, profile, reason, phases, sent, received]."""
from collections import Counter,defaultdict
from math import ceil
from .benchconfig import PHASES


def quantile(values,p):
    return round(values[max(0,ceil(len(values)*p)-1)],2) if values else None


def reason(row):
    if len(row)>5:
        return row[5]
    return "" if 200<=row[2]<400 else ("HTTP "+str(row[2]) if row[2] else "Transport")


def summarize(samples, duration, dropped=0):
    times=sorted(row[1] for row in samples)
    codes=Counter(str(row[2]) if row[2] else "network" for row in samples)
    errors=sum(bool(reason(row)) for row in samples)
    return dict(count=len(samples),errors=errors,dropped=dropped,rps=round(len(samples)/max(duration,.001),2),
                minimum=min(times,default=None),maximum=max(times,default=None),
                mean=round(sum(times)/len(times),2) if times else None,
                p50=quantile(times,.50),p90=quantile(times,.90),p95=quantile(times,.95),p99=quantile(times,.99),
                error_pct=round(100*errors/len(samples),4) if samples else None,codes=dict(codes))


def full_report(samples,duration,dropped,series,profile_times):
    report=summarize(samples,duration,dropped)
    steps,profiles=defaultdict(list),defaultdict(list)
    edges=[5,10,25,50,100,250,500,1000,2500,5000,10000,float('inf')]
    hist=[0]*len(edges)
    phases=defaultdict(float)
    completed_per_second=Counter()
    for row in samples:
        steps[row[3]].append(row)
        profiles[row[4]].append(row)
        completed_per_second[int(row[0]+row[1]/1000)]+=1
        for k,t in row[6].items(): phases[k]+=t
        hist[next(i for i,v in enumerate(edges) if row[1]<=v)]+=1
    def group(key,rows,elapsed):
        return dict(label=key,**summarize(rows,elapsed))
    report.update(steps=[group(k,v,duration) for k,v in steps.items()],
        profiles=[group(k,profiles.get(k,[]),t) for k,t in profile_times.items()],
        phases={k:round(phases[k]/max(1,len(samples)),2) for k in PHASES},
        failures=dict(Counter(reason(r) for r in samples if reason(r))),
        histogram=[dict(label=f"≤ {e} ms" if e!=float('inf') else "> 10000 ms",count=n,
                width=round(100*n/max(1,max(hist)))) for e,n in zip(edges,hist)],
        peak_rps=max(completed_per_second.values(),default=0),
        received=sum(r[8] for r in samples),sent=sum(r[7] for r in samples),duration=round(duration,3))
    return report


def pace(pattern,peak,second,seconds):
    fraction=min(1,second/max(1,seconds-1))
    if pattern in ("ramp","stress"): return max(1,ceil(peak*(.15+.85*fraction)))
    if pattern=="spike": return peak if .35<=fraction<=.65 else max(1,peak//4)
    if pattern=="latency": return 1
    return peak


def target(cfg,options,second):
    for stage in options.get("stages",[]):
        if second<stage["seconds"]: return stage["target"]
        second-=stage["seconds"]
    peak=options["users"] if options["model"]=="users" else cfg["rate"]
    if options.get("step_size"):
        return min(peak,options["start_load"]+int(second//options["stage_seconds"])*options["step_size"])
    return pace(cfg["pattern"],peak,second,cfg["seconds"])
