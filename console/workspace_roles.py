from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

User = get_user_model()


def role_of(member):
    if member.is_superuser:
        return "admin"
    if any(group.name == "PyPulse Editors" for group in member.groups.all()):
        return "editor"
    return "viewer"


def editor_group():
    group, _ = Group.objects.get_or_create(name="PyPulse Editors")
    models = ("monitor", "maintenance", "incident", "exercise", "run", "networkprofile", "variable")
    group.permissions.set(Permission.objects.filter(content_type__app_label="console", content_type__model__in=models))
    return group


def apply_role(member, role, active):
    if member.pk and member.is_superuser and member.is_active and (role != "admin" or not active):
        if not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=member.pk).exists():
            raise ValueError("Keep at least one active administrator.")
    member.is_active = active
    member.is_superuser = member.is_staff = role == "admin"
    member.save()
    if role == "editor":
        member.groups.add(editor_group())
    else:
        member.groups.remove(*Group.objects.filter(name="PyPulse Editors"))


def update_access(actor, member, role, active):
    if actor.pk == member.pk and (role != "admin" or not active):
        raise ValueError("You cannot demote or disable your own account.")
    disabling = member.is_active and not active
    apply_role(member, role, active)
    if disabling:
        from django.contrib.sessions.models import Session
        from django.utils import timezone
        for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
            if session.get_decoded().get("_auth_user_id") == str(member.pk):
                session.delete()
