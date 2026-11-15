from urllib.parse import urlsplit
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from .models import Exercise, Monitor, Service


def check_url(value, test=False):
    try:
        url = urlsplit(value)
        port = url.port
        if url.scheme not in ("http","https") or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError
    except ValueError:
        raise ValidationError("Enter an HTTP(S) URL without credentials or a fragment.")
    host = url.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{port or (443 if url.scheme == 'https' else 80)}"
    if test and authority not in settings.TEST_HOSTS:
        raise ValidationError(f"Add {authority} to PYPULSE_TEST_HOSTS on your dashboard first.")
    return value


class ServiceForm(forms.ModelForm):
    target = forms.FloatField(min_value=1,max_value=99.999,initial=99.9,label="Availability target (%)")
    class Meta:
        model = Service
        fields = ("name","framework","target")


class MonitorForm(forms.ModelForm):
    url = forms.CharField(label="HTTP URL",max_length=1000)
    interval = forms.IntegerField(min_value=10,max_value=86400,initial=60,label="Check interval (seconds)")
    failure_threshold = forms.IntegerField(min_value=1,max_value=50,initial=2)
    expected_status = forms.IntegerField(min_value=100,max_value=599,initial=200)
    class Meta:
        model = Monitor
        fields = ("name","url","interval","expected_status","contains","warn_ms","failure_threshold","public","paused")

    def clean_url(self):
        return check_url(self.cleaned_data["url"])


class ExerciseForm(forms.ModelForm):
    url = forms.CharField(label="HTTP URL",max_length=1000)
    rate = forms.IntegerField(min_value=1,max_value=50,initial=10,label="Target requests / second")
    seconds = forms.IntegerField(min_value=1,max_value=3600,initial=30,label="Duration (seconds)")
    max_error_pct = forms.FloatField(min_value=0,max_value=100,initial=0,label="Maximum errors (%)")
    repeat_minutes = forms.IntegerField(min_value=0,max_value=10080,initial=0,label="Repeat every N minutes (0 = manual)")
    class Meta:
        model = Exercise
        fields = ("name","url","pattern","rate","seconds","max_p95","max_error_pct","repeat_minutes")
        labels = {"max_p95":"Maximum p95 in ms (0 = off)"}

    def clean_url(self):
        return check_url(self.cleaned_data["url"],test=True)

    def clean(self):
        data=super().clean()
        if data.get("rate",0)*data.get("seconds",0)>30000:
            raise ValidationError("Limit each run to 30,000 scheduled requests.")
        return data
