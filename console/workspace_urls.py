from django.urls import path
from . import workspace_views as views

urlpatterns = [
    path("workspace/", views.workspace, name="workspace"),
    path("workspace/settings/", views.settings_view, name="workspace-settings"),
    path("workspace/members/", views.members, name="workspace-members"),
    path("workspace/members/<int:pk>/", views.update_member, name="workspace-update-member"),
    path("workspace/tokens/", views.tokens, name="workspace-tokens"),
    path("workspace/tokens/<int:pk>/revoke/", views.revoke, name="workspace-revoke"),
    path("workspace/config/", views.config, name="workspace-config"),
    path("workspace/config/export/", views.export, name="workspace-export"),
    path("workspace/config/import/", views.import_file, name="workspace-import"),
]
