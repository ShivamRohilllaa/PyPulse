from django import template
from django.utils.html import format_html,format_html_join
register=template.Library()


@register.simple_tag
def bench_plot(series, metric):
    """At most 120 SVG points per trace, regardless of the run length."""
    traces={"latency":(("p50","#84ebc8"),("p95","#ffad9c"),("p99","#c8b4fa")),
            "cpu":(("host_cpu_pct","#84ebc8"),("loop_cpu_pct","#c8b4fa")),"scheduler_delay":(("loop_lag_ms","#ffad9c"),),
            "active":(("active","#84ebc8"),),"errors":(("error_pct","#ffad9c"),)}
    chosen=traces[metric]
    if not series: return "Waiting for samples"
    stride=max(1,(len(series)+119)//120)
    points=[series[i:i+stride] for i in range(0,len(series),stride)]
    values=[[max((sample.get(k) or 0 for sample in group),default=0) for group in points] for k,_ in chosen]
    peak=max(1,*[n for line in values for n in line])
    lines=[]
    for line,(_,color) in zip(values,chosen):
        pairs=" ".join(f"{i*720/max(1,len(line)-1):.2f},{90-v*86/peak:.2f}" for i,v in enumerate(line))
        if len(line)==1: pairs+=" 720,"+f"{90-line[0]*86/peak:.2f}"
        lines.append((color,pairs))
    return format_html('<svg class="chart" viewBox="0 0 720 96" preserveAspectRatio="none" role="img" aria-label="{} trend, peak {}"><path d="M0 4H720 M0 47H720 M0 90H720" fill="none" stroke="#344242" stroke-dasharray="3 5"/>{}</svg><small class="muted">Scale: 0 – {} {}</small>',metric,round(peak,2),format_html_join("",'<polyline fill="none" stroke="{}" vector-effect="non-scaling-stroke" stroke-width="2" points="{}"/>',lines),round(peak,2),{"latency":"ms","errors":"%","active":"iterations","cpu":"%","scheduler_delay":"ms"}[metric])


@register.simple_tag
def profile_timeline(rows, profiles):
    """One trace per profile. Missing transport responses produce gaps in the trace."""
    from console.benchstats import answered
    from math import ceil
    palette = ("#84ebc8","#ffad9c","#c8b4fa","#86caff","#e7cc81","#f6a5ce","#a7d67f","#a5b7c7")
    timings = {profile["label"]: [] for profile in profiles}
    for row in rows:
        if len(row) >= 5 and row[4] in timings:
            timings[row[4]].append(row[1] if answered(row) else None)
    n = max((len(values) for values in timings.values()), default=0)
    if not n:
        return ""
    stride = max(1, ceil(n / 160))
    total = ceil(n/stride)
    peak = max(1, max((ms for values in timings.values() for ms in values if ms is not None), default=0))
    traces, legend = [], []
    for index, (name, values) in enumerate(timings.items()):
        color = palette[index % len(palette)]
        legend.append((color, name))
        segments = []
        for start in range(0, len(values), stride):
            ms = max((v for v in values[start:start+stride] if v is not None), default=None)
            if ms is None:
                if segments:
                    traces.append((color, " ".join(segments)))
                    segments = []
                continue
            x = 4 + 712*(start//stride)/max(1,total-1)
            y = 91 - 85*ms/peak
            if not segments:
                # Make single observations visible, even when separated by failures.
                segments.append(f"{x-1:.1f},{y:.1f}")
            segments.append(f"{x:.1f},{y:.1f}")
        if segments:
            traces.append((color, " ".join(segments)))
    return format_html(
        '<svg class="chart" viewBox="0 0 720 96" preserveAspectRatio="none" role="img" '
        'aria-label="Per-request latency for {} profiles, up to {} requests per profile">'
        '<path d="M0 5H720 M0 48H720 M0 91H720" fill="none" stroke="#344242" stroke-dasharray="3 5"/>{}</svg>'
        '<div class="axis"><span>Request 1</span><span>Request {}</span></div><div class="meta">{}</div>'
        '<small class="muted">Range 0–{} ms. Each point shows the slowest response in {} request(s).</small>',
        len(timings), n,
        format_html_join("",'<polyline fill="none" stroke="{}" vector-effect="non-scaling-stroke" stroke-width="2" stroke-linecap="round" points="{}"/>',traces),
        n, format_html_join("",'<span style="color:{}">{}</span>',legend), round(peak,2), stride,
    )


@register.simple_tag
def phase_bar(phases):
    palette = {"dns":"#84ebc8","tcp":"#86caff","tls":"#c8b4fa","ttfb":"#ffad9c","download":"#e7cc81"}
    total = sum((phases or {}).get(k) or 0 for k in palette)
    if not total:
        return ""
    bars = []
    x = 0
    for key,color in palette.items():
        ms = phases.get(key) or 0
        if ms:
            width = 100 * ms / total
            bars.append((x, width, color, key.upper(), ms))
            x += width
    return format_html('<svg width="100%" height="12" viewBox="0 0 100 12" preserveAspectRatio="none" role="img" aria-label="Average share of measured HTTP phase time">{}</svg>',
        format_html_join("",'<rect x="{}" width="{}" height="12" fill="{}"><title>{}: {} ms</title></rect>',bars))
