import uuid
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import Client,TestCase
from django.utils import timezone
from .engine import pace,summarize
from .forms import ExerciseForm
from .models import Exercise,Run
from .telemetry import percentile

class WorkspaceTests(TestCase):
    def test_views_and_permissions(self):
        user=User.objects.create_user("reader")
        self.client.force_login(user)
        for path in ["/","/uptime/","/benchmarks/","/incidents/","/activity/"]:
            self.assertEqual(self.client.get(path).status_code,200,path)
        self.assertEqual(self.client.post("/services/connect/",{}).status_code,403)
        self.assertEqual(Client().get("/status/").status_code,200)
        self.assertEqual(Client().get("/").status_code,302)

    def test_create_and_queue(self):
        self.client.force_login(User.objects.create_superuser("owner"))
        r=self.client.post("/benchmarks/new/",{"name":"Orders","url":"http://127.0.0.1:8000/orders/42",
            "pattern":"fixed","rate":10,"seconds":30,"max_p95":100,"max_error_pct":0,"repeat_minutes":0})
        self.assertEqual(r.status_code,302)
        self.client.post(f"/benchmarks/{Exercise.objects.get().pk}/run/")
        run=Run.objects.get()
        self.assertEqual(run.config["rate"],10)
        self.assertEqual(self.client.get(f"/runs/{run.pk}").status_code,200)
        self.client.post(f"/runs/{run.pk}/stop/")
        run.refresh_from_db()
        self.assertEqual(run.state,"Stopped")

    def test_bounds_and_percentiles(self):
        values=[[0,4,200],[.1,12,500]]
        self.assertEqual(summarize(values,1,0)["p95"],12)
        self.assertEqual(summarize(values,1,0)["error_pct"],50)
        self.assertEqual(percentile([19,0,0,0,0,0,0,0,1],.95),5)
        self.assertLess(pace("ramp",10,0,10),pace("ramp",10,9,10))
        self.assertEqual(pace("spike",10,5,10),10)
