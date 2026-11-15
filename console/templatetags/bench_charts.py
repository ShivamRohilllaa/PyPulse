from django import template
from django.utils.html import format_html,format_html_join
register=template.Library()


@register.simple_tag
def bench_plot(series, metric):
    """At most 120 SVG points per trace, regardless of the run length."""
    traces={"latency":(("p50","#84ebc8"),("p95","#ffad9c"),("p99","#c8b4fa")),
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
    return format_html('<svg class="chart" viewBox="0 0 720 96" preserveAspectRatio="none" role="img" aria-label="{} trend, peak {}"><path d="M0 4H720 M0 47H720 M0 90H720" fill="none" stroke="#344242" stroke-dasharray="3 5"/>{}</svg><small class="muted">Scale: 0 – {} {}</small>',metric,round(peak,2),format_html_join("",'<polyline fill="none" stroke="{}" vector-effect="non-scaling-stroke" stroke-width="2" points="{}"/>',lines),round(peak,2),{"latency":"ms","errors":"%","active":"iterations"}[metric])
