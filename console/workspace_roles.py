from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

User = get_user_model()


def role_of(member):
    if member.is_superuser:
        return "admin"
    if any(group.name == "PyPulse Editors" for group in member.groups.all()):
        return "editor"
    return "viewer"


def apply_role(member, role, active):
    if member.pk and member.is_superuser and member.is_active and (role != "admin" or not active):
        if not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=member.pk).exists():
            raise ValueError("Keep at least one active administrator.")
    member.is_active = active
    member.is_superuser = member.is_staff = role == "admin"
    member.save()
    group, _ = Group.objects.get_or_create(name="PyPulse Editors")
    if role == "editor":
        # Editors manage test inputs and observations. Workspace access and outgoing alert credentials stay admin-only.
        models = ("monitor", "maintenance", "incident", "exercise", "run")
        group.permissions.set(Permission.objects.filter(content_type__app_label="console", content_type__model__in=models))
        member.groups.add(group)
    else:
        member.groups.remove(group)
