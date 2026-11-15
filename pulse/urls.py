from django.views.decorators.http import require_GET, require_POST
from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import include, path
from django.views.generic.base import RedirectView
from console import views as v, api, comparisons, live_events, lab_views
from console.audit import audit_api
from console.telemetry import ingest
from console.workspace_auth import WorkspaceLogin

urlpatterns=[
    path("",include("console.lab_urls")),
    path("api/audit", audit_api),
    path("api/stream", live_events.events),
    path("api/events", live_events.events, name="live_events"),
    path("benchmarks/<int:pk>/delete/",lab_views.remove,{"kind":"exercise"},name="delete_test"),
    path("runs/<int:pk>/delete/",lab_views.remove,{"kind":"run"},name="delete_run"),
    path("runs/<int:pk>/summary.txt",lab_views.summary_text,name="summary_text"),
    path("api/runs/<int:pk>/raw",lab_views.raw_csv,name="raw_csv"),
    path("benchmarks/compare/", comparisons.compare, name="compare_runs"),
    path("",include("console.resource_urls")),path("",include("console.uptime_urls")),path("",include("console.workspace_urls")),
    path("services/<int:pk>/target/",v.edit_target,name="edit_target"),
    path("api/python/services",api.services),path("api/python/services/<int:pk>/goal",api.goal),
    path("api/python/services/<int:pk>/rotate",api.rotate),path("api/python/services/<int:pk>",api.remove),
    path("api/ci/tests/<int:pk>/run",require_POST(api.ci)),path("api/ci/runs/<int:pk>",require_GET(api.ci)),
    path("healthz",v.health),path("api/healthz",v.health),
    path("api/python/ingest",ingest),path("api/python/services/<int:pk>/report",v.api_report),
    path("login/",WorkspaceLogin.as_view(),name="login"),
    path("logout/",auth.LogoutView.as_view(),name="logout"),path("admin/",admin.site.urls),
    path("",v.traffic,name="traffic"),path("python",v.traffic),
    path("services/connect/",v.connect,name="connect"),path("services/<int:pk>/rotate/",v.rotate,name="rotate"),
    path("uptime/",v.monitoring,name="monitors"),path("uptime/new/",v.configure,name="new_monitor"),
    path("uptime/<int:pk>/",v.monitor_detail,name="monitor"),path("uptime/<int:pk>/edit/",v.configure,name="edit_monitor"),
    path("uptime/<int:pk>/check/",v.check_now,name="check"),path("status/",v.status,name="status"),
    path("incidents/",v.incidents,name="incidents"),path("incidents/<int:pk>/ack/",v.acknowledge,name="ack"),
    path("tests/load",lab_views.index),path("benchmarks/",lab_views.index,name="benchmarks"),
    path("benchmarks/new/",lab_views.editor,name="new_test"),
    path("benchmarks/<int:pk>/edit/",lab_views.editor,name="edit_test"),
    path("benchmarks/<int:pk>/run/",lab_views.start,name="start"),
    path("runs/<int:pk>",v.run_detail,name="run"),path("runs/<int:pk>/csv/",v.export,name="csv"),
    path("runs/<int:pk>/stop/",v.stop,name="stop"),path("activity/",v.activity,name="activity")
]


urlpatterns += [
    path("tests/latency", lab_views.editor, {"pattern":"latency"}, name="latency_test"),
    path("runs/compare", RedirectView.as_view(pattern_name="compare_runs", query_string=True)),
    path("monitors/new", RedirectView.as_view(pattern_name="new_monitor")),
    path("monitors/<int:pk>", RedirectView.as_view(pattern_name="monitor")),
    path("monitors/<int:pk>/edit", RedirectView.as_view(pattern_name="edit_monitor")),
]
for old_path, view_name in (("runs", "benchmarks"), ("alerts", "incidents"), ("profiles", "networks"),
                           ("channels", "channels"), ("settings", "workspace"), ("audit", "activity")):
    urlpatterns.append(path(old_path, RedirectView.as_view(pattern_name=view_name, query_string=True)))
