from django.urls import path
from . import workspace_views as views, workspace_auth as auth, workspace_api as api, workspace_ci as ci

urlpatterns = [
    path("setup/", auth.setup, name="workspace-setup"),
    path("workspace/account/", auth.account_view, name="workspace-account"),
    path("workspace/system/", api.system_page, name="workspace-system"),
    path("api/auth/status", auth.status_api), path("api/auth/setup", auth.setup_api),
    path("api/auth/login", auth.login_api), path("api/auth/logout", auth.logout_api),
    path("api/auth/me", auth.me_api), path("api/auth/password", auth.password_api),
    path("api/users", api.members), path("api/users/<int:pk>", api.members),
    path("api/tokens", api.tokens), path("api/tokens/<int:pk>", api.tokens),
    path("api/settings", api.settings_api), path("api/system", api.system_api),
    path("api/config/export", api.export_api), path("api/config/import", api.import_api),
    path("api/profiles", api.resource, {"kind": "profiles"}),
    path("api/profiles/<int:pk>", api.resource, {"kind": "profiles"}),
    path("api/variables", api.resource, {"kind": "variables"}),
    path("api/variables/<int:pk>", api.resource, {"kind": "variables"}),
    path("api/templates/<int:pk>/run", ci.start_saved),
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
