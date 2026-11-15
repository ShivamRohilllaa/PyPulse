from django.urls import path

from . import uptime_views as v, uptime_api as api

urlpatterns = [
    path("api/monitors",api.monitors),path("api/monitors/test",api.preview),
    path("api/monitors/<int:pk>",api.monitors),path("api/alerts",api.alerts),
    path("api/alerts/<int:pk>/notifications",api.notifications),
    path("api/channels",api.channels),path("api/channels/<int:pk>",api.channels),path("api/channels/<int:pk>/test",api.test_channel),
    path("api/maintenance",api.windows),path("api/maintenance/<int:pk>",api.windows),path("api/public/status",api.public_status),
    *[path(f"api/monitors/<int:pk>/{part}",api.history,{"section":part}) for part in ("checks","stats","alerts")],
    *[path(f"api/monitors/<int:pk>/{verb}",api.action,{"action":verb}) for verb in ("check","clone","pause","resume")],
    *[path(f"api/alerts/<int:pk>/{verb}",api.alert_action,{"verb":verb}) for verb in ("ack","resolve")],
    path("maintenance/<int:pk>/edit/", v.maintenance, name="edit_maintenance"),
    path("incidents/<int:pk>/", v.incident_detail, name="incident_detail"),
    path("incidents/<int:pk>/resolve/", v.resolve, name="resolve"),
    path("uptime/new/preview/", v.preview_monitor, name="new_monitor_preview"),
    path("uptime/<int:pk>/preview/", v.preview_monitor, name="edit_monitor_preview"),
    path("maintenance/", v.maintenance, name="maintenance"),
    path("maintenance/<int:window_pk>/delete/", v.remove_shared_window, name="remove_shared_window"),
    path("uptime/<int:pk>/actions/<str:action>/", v.monitor_action, name="monitor_action"),
    path("uptime/<int:pk>/maintenance/", v.add_window, name="add_window"),
    path("uptime/<int:pk>/maintenance/<int:window_pk>/delete/", v.delete_window, name="delete_window"),
    path("incidents/channels/", v.channels, name="channels"),
    path("incidents/channels/new/", v.channel_form, name="new_channel"),
    path("incidents/channels/<int:pk>/", v.channel_form, name="edit_channel"),
    path("incidents/channels/<int:pk>/<str:action>/", v.channel_action, name="channel_action"),
]
