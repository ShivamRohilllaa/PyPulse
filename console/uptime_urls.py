from django.urls import path

from . import uptime_views as v

urlpatterns = [
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
