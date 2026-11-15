from django.urls import path
from . import resources

urlpatterns = []
for kind in resources.RESOURCES:
    defaults = {"kind": kind}
    urlpatterns += [
        path(f"{kind}/", resources.listing, defaults, name=kind),
        path(f"{kind}/new/", resources.edit, defaults, name=f"{kind}_new"),
        path(f"{kind}/<int:pk>/edit/", resources.edit, defaults, name=f"{kind}_edit"),
        path(f"{kind}/<int:pk>/delete/", resources.remove, defaults, name=f"{kind}_delete"),
    ]
