from django.views.decorators.http import require_GET, require_POST
from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from console import views as v, api
from console.telemetry import ingest

urlpatterns=[
    path("api/python/services",api.services),path("api/python/services/<int:pk>/goal",api.goal),
    path("api/python/services/<int:pk>/rotate",api.rotate),path("api/python/services/<int:pk>",api.remove),
    path("api/ci/tests/<int:pk>/run",require_POST(api.ci)),path("api/ci/runs/<int:pk>",require_GET(api.ci)),
    path("healthz",v.health),path("api/healthz",v.health),
    path("api/python/ingest",ingest),path("api/python/services/<int:pk>/report",v.api_report),
    path("login/",auth.LoginView.as_view(template_name="console/login.html"),name="login"),
    path("logout/",auth.LogoutView.as_view(),name="logout"),path("admin/",admin.site.urls),
    path("",v.traffic,name="traffic"),path("python",v.traffic),
    path("services/connect/",v.connect,name="connect"),path("services/<int:pk>/rotate/",v.rotate,name="rotate"),
    path("uptime/",v.monitoring,name="monitors"),path("uptime/new/",v.configure,{"kind":"monitor"},name="new_monitor"),
    path("uptime/<int:pk>/",v.monitor_detail,name="monitor"),path("uptime/<int:pk>/edit/",v.configure,{"kind":"monitor"},name="edit_monitor"),
    path("uptime/<int:pk>/check/",v.check_now,name="check"),path("status/",v.status,name="status"),
    path("incidents/",v.incidents,name="incidents"),path("incidents/<int:pk>/ack/",v.acknowledge,name="ack"),
    path("tests/load",v.benchmarks),path("benchmarks/",v.benchmarks,name="benchmarks"),
    path("benchmarks/new/",v.configure,{"kind":"test"},name="new_test"),
    path("benchmarks/<int:pk>/edit/",v.configure,{"kind":"test"},name="edit_test"),
    path("benchmarks/<int:pk>/run/",v.start,name="start"),
    path("runs/<int:pk>",v.run_detail,name="run"),path("runs/<int:pk>/csv/",v.export,name="csv"),
    path("runs/<int:pk>/stop/",v.stop,name="stop"),path("activity/",v.activity,name="activity")
]
