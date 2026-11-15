from django.urls import path
from . import lab_api

urlpatterns = [
    path("api/tests/preflight", lab_api.adhoc, {"preview": True}),
    path("api/tests/run", lab_api.adhoc),
    path("api/templates", lab_api.templates),
    path("api/templates/<int:pk>", lab_api.templates),
    path("api/runs", lab_api.runs),
    path("api/runs/active", lab_api.runs, {"active": True}),
    path("api/runs/<int:pk>", lab_api.runs),
    path("api/runs/<int:pk>/stop", lab_api.stop),
]
