from urllib.parse import urlsplit
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from .models import Exercise, Monitor, Service


def check_url(value, test=False):
    from .probes import approved_url
    approved_url(value, allowed=test)
    return value


class ServiceForm(forms.ModelForm):
    target = forms.FloatField(min_value=1,max_value=99.999,initial=99.9,label="Availability target (%)")
    class Meta:
        model = Service
        fields = ("name","framework","target")


from .uptime_forms import MonitorForm


class ExerciseForm(forms.ModelForm):
    from .models import NetworkProfile
    url = forms.CharField(label="Default HTTP endpoint",max_length=1000)
    rate = forms.IntegerField(min_value=1,max_value=50,initial=10,label="Target iterations / second (arrival model)")
    seconds = forms.IntegerField(min_value=1,max_value=3600,initial=30,label="Duration per profile (seconds)")
    max_error_pct = forms.FloatField(min_value=0,max_value=100,initial=0,label="Maximum errors (%)")
    repeat_minutes = forms.IntegerField(min_value=0,max_value=10080,initial=0,label="Repeat every N minutes (0 = manual)")
    model = forms.ChoiceField(choices=[("arrival","Arrival rate"),("users","Virtual users")],required=False,initial="arrival",label="Load model")
    users = forms.IntegerField(min_value=1,max_value=20,required=False,initial=5,label="Virtual users / concurrent iteration limit")
    count = forms.IntegerField(min_value=1,max_value=10000,required=False,label="Latency iterations per profile",help_text="When blank, use one iteration for each configured second.")
    timeout_ms = forms.IntegerField(min_value=50,max_value=30000,required=False,initial=5000,label="Request timeout (ms)")
    delay_ms = forms.IntegerField(min_value=0,max_value=60000,required=False,initial=0,label="Delay between iterations (ms; latency and virtual users)")
    keep_alive = forms.ChoiceField(choices=[("yes","Reuse connections"),("no","Open a fresh connection")],required=False)
    profiles = forms.ModelMultipleChoiceField(queryset=NetworkProfile.objects.order_by("name"),required=False,label="Network profiles",widget=forms.CheckboxSelectMultiple, help_text="Select profiles to compare. Empty uses the unthrottled connection.")
    environment = forms.CharField(required=False,max_length=80,help_text="Workspace variable scope; empty uses global variables.")
    steps = forms.JSONField(required=False,initial=list,label="Scenario steps (JSON array)",widget=forms.Textarea(attrs={"rows":6}),help_text="Empty sends GET to the default endpoint.")
    csv_data = forms.CharField(required=False,strip=False,label="CSV fixture",widget=forms.Textarea(attrs={"rows":3}),help_text="Use non-secret values. Rows rotate between iterations.")
    csv_file = forms.FileField(required=False,label="Or upload a CSV (64 KiB)")
    csv_order = forms.ChoiceField(choices=[("sequential","Sequential rows"),("random","Random rows")],required=False,initial="sequential",label="CSV row selection")
    statuses = forms.CharField(required=False,label="Acceptable HTTP statuses",help_text="Default: 200–399. Enter codes, ranges or classes such as 2xx,304. An explicit step expectation overrides this.")
    min_rps = forms.FloatField(min_value=0,max_value=50,required=False,label="Minimum average requests / second (0 = off)")
    daily_at = forms.RegexField(regex=r"^(?:[01]\d|2[0-3]):[0-5]\d$",required=False,label="Daily run at UTC time",widget=forms.TimeInput(attrs={"type":"time"}),help_text="Use a daily time or an interval, not both.")
    stages = forms.JSONField(required=False,initial=list,label="Custom stages (JSON array)",widget=forms.Textarea(attrs={"rows":3}),help_text='For example: [{"seconds":10,"target":2},{"seconds":20,"target":5}]. Durations must add up to the duration above.')
    start_load = forms.IntegerField(min_value=1,max_value=50,required=False,label="Ramp starting load")
    step_size = forms.IntegerField(min_value=1,max_value=50,required=False,label="Ramp increment")
    stage_seconds = forms.IntegerField(min_value=1,max_value=3600,required=False,label="Seconds per ramp increment")
    plateau_windows = forms.IntegerField(min_value=0,max_value=60,required=False,label="Stress: stop after N higher stages without throughput growth (0 = off)")
    gate_policy = forms.ChoiceField(choices=[("evaluate","Evaluate release gates"),("measure","Measurements only")],required=False,initial="evaluate",label="Gate mode")
    profile_mode = forms.ChoiceField(choices=[("parallel","Run latency profiles together"),("sequential","Run one profile at a time")],required=False,initial="parallel",label="Latency profile timing")
    max_avg_ms = forms.IntegerField(min_value=0,max_value=300000,required=False,label="Maximum mean latency (ms, 0 = off)")
    max_p99_ms = forms.IntegerField(min_value=0,max_value=300000,required=False,label="Maximum p99 latency (ms, 0 = off)")
    stress_policy = forms.ChoiceField(choices=[("gates","Use p95 and error gates"),("independent","Separate stress stop conditions")],required=False,initial="gates",label="Stress stop conditions")
    stop_error_pct = forms.FloatField(min_value=0,max_value=100,required=False,label="Independent stress error limit (%)")
    stop_p95_ms = forms.IntegerField(min_value=1,max_value=300000,required=False,label="Independent stress p95 limit (ms)")
    plateau_growth_pct = forms.IntegerField(min_value=0,max_value=100,required=False,label="Minimum growth between stress stages (%)")
    hold_seconds = forms.IntegerField(min_value=1,max_value=3600,required=False,label="Final stepped-ramp hold (seconds)")
    stress_to_max = forms.BooleanField(required=False,label="Finish stress after one full stage at the maximum load")
    base_load = forms.IntegerField(min_value=1,max_value=50,required=False,label="Spike baseline load")
    base_seconds = forms.IntegerField(min_value=1,max_value=3600,required=False,label="Spike baseline duration (seconds)")
    peak_seconds = forms.IntegerField(min_value=1,max_value=3600,required=False,label="Spike peak duration (seconds)")
    recovery_seconds = forms.IntegerField(min_value=1,max_value=3600,required=False,label="Spike recovery duration (seconds)")
    class Meta:
        model = Exercise
        fields = ("name","url","pattern","model","rate","users","seconds","max_p95","max_error_pct","repeat_minutes")
        labels = {"max_p95":"Maximum p95 in ms (0 = off)"}

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        if not self.instance.pk:
            from .models import WorkspaceSettings
            self.initial.setdefault("timeout_ms",WorkspaceSettings.current().timeout_ms)
        for key,value in (self.instance.options or {}).items():
            self.initial[key]=("yes" if value else "no") if key=="keep_alive" else (",".join(map(str,value)) if key=="statuses" else value)

    @property
    def groups(self):
        groups=[
            ("Target & traffic","Arrival rate schedules iterations; virtual users complete an iteration before starting the next.","name url pattern model rate users seconds",True),
            ("Pass criteria & schedule","A run fails its gate if a threshold is exceeded or an arrival is dropped.","gate_policy max_avg_ms max_p95 max_p99_ms max_error_pct min_rps repeat_minutes daily_at",True),
            ("Connection & latency","Compare profiles under identical scenario data and choose how many latency iterations to send.","profiles profile_mode count timeout_ms delay_ms keep_alive statuses",False),
            ("Scenario & data","Chain requests with JSON or response-header extraction. Use workspace variables for credentials.","environment steps csv_data csv_file csv_order",False),
            ("Load stages","Set an explicit schedule or configure a stepped ramp. Plateau detection compares throughput as the load increases.","stages start_load step_size stage_seconds hold_seconds stress_to_max stress_policy stop_error_pct stop_p95_ms plateau_windows plateau_growth_pct base_load base_seconds peak_seconds recovery_seconds",False)]
        return [dict(title=t,description=d,fields=[self[x] for x in keys.split()],open=o,example=t=="Scenario & data") for t,d,keys,o in groups]

    def _get_validation_exclusions(self):
        fields=super()._get_validation_exclusions()
        if "{{" in (self.cleaned_data.get("url") or ""): fields.add("url")
        return fields

    def clean_url(self):
        value=self.cleaned_data["url"]
        if len(value)>Exercise._meta.get_field("url").max_length:
            raise ValidationError("The default endpoint is too long; put longer URLs in scenario steps.")
        return value

    def clean(self):
        from .benchconfig import KEYS,validate_budget,validate_options
        data=super().clean()
        if self.errors: return data
        opts={k:data[k] for k in KEYS if k in data and data[k] is not None and data[k]!=""}
        opts["profiles"]=[p.pk for p in data["profiles"]]
        opts["keep_alive"]=data.get("keep_alive")!="no"
        file=data.get("csv_file")
        if file:
            if file.size>65536: raise ValidationError("CSV data must be at most 64 KiB.")
            try: opts["csv_data"]=file.read(65537).decode("utf-8-sig")
            except UnicodeDecodeError: raise ValidationError("Upload a UTF-8 CSV file.")
        if data.get("daily_at") and data.get("repeat_minutes"):
            raise ValidationError("Choose either a repeat interval or a daily UTC time.")
        opts=validate_options(opts,data["url"])
        validate_budget(opts,data["pattern"],data["rate"],data["seconds"])
        self.instance.options=opts
        return data
