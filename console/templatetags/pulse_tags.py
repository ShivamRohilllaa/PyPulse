from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
register=template.Library()

@register.filter
def decimal(n):
    return "—" if n is None or n=="" else f"{float(n):,.1f}"

@register.filter
def pct(n):
    return "—" if n is None else f"{n:,.2f}".rstrip("0").rstrip(".")+"%"

@register.filter
def bucket(n,count):
    return f"≤ {n:,} ms" if n else ("> 10,000 ms" if count else "—")

def chart(values,errors):
    width,height=720,95
    peak=max([1,*values])
    bars=[]
    step=width/len(values)
    for i,(n,e) in enumerate(zip(values,errors)):
        for v,color in [(n,"#71e5c1"),(e,"#ff947f")]:
            if v:
                h=max(2,height*v/peak)
                bars.append(f'<rect x="{i*step:.2f}" y="{height-h:.2f}" width="{max(1,step*.82):.2f}" height="{h:.2f}" rx="1.5" fill="{color}"/>')
    return format_html('<svg class="chart" viewBox="0 0 720 96" preserveAspectRatio="none" role="img" aria-label="Request volume, peak {}"><path d="M0 1H720 M0 47H720 M0 95H720" fill="none" stroke="#344242" stroke-dasharray="3 5"/>{}</svg>',peak,mark_safe("".join(bars)))

@register.simple_tag
def traffic_chart(trend,hours):
    size={1:60,24:24,168:168}[hours]
    seconds=60 if hours==1 else 3600
    end=int(timezone.now().timestamp())//seconds
    volume=[0]*size
    errors=[0]*size
    for row in trend:
        i=int(row["t"].timestamp())//seconds-end+size-1
        if 0<=i<size:
            volume[i]=row["n"]
            errors[i]=row["errors"]
    return chart(volume,errors)

@register.simple_tag
def run_chart(series):
    if not series:
        return chart([0],[0])
    stride=max(1,(len(series)+89)//90)
    points=[series[i:i+stride] for i in range(0,len(series),stride)]
    return chart([max(x["count"] for x in p) for p in points],[max(x["errors"] for x in p) for p in points])
