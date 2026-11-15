"""Apply operator-provided first-run settings after database migrations."""
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from console.workspace_auth import User, first_admin
from console.workspace_config import LIMIT, ConfigError, import_config, parse_file
from console.workspace_roles import editor_group
from django.core.validators import validate_email
from django.core.exceptions import ValidationError


class Command(BaseCommand):
    help = "Refresh the built-in editor role and apply optional admin credentials and workspace configuration."

    @transaction.atomic
    def handle(self, **opts):
        editor_group()
        email = os.environ.get("PYPULSE_ADMIN_EMAIL", "").strip()
        password = os.environ.get("PYPULSE_ADMIN_PASSWORD", "")
        if (email or password) and not User.objects.exists():
            try:
                validate_email(email)
            except ValidationError as error:
                raise CommandError("Supply a valid PYPULSE_ADMIN_EMAIL.") from error
            if len(email) > 150 or len(password) < 12:
                raise CommandError("Use an email up to 150 characters and an admin password of at least 12 characters.")
            first_admin({"email": email, "password": password})
            self.stdout.write("Initial administrator created.")
        path = os.environ.get("PYPULSE_CONFIG_FILE", "")
        if path:
            try:
                with Path(path).open("rb") as file:
                    config = parse_file(file.read(LIMIT + 1))
                import_config(config)
            except (OSError, ConfigError) as error:
                raise CommandError(f"Workspace configuration could not be applied: {error}") from error
            self.stdout.write("Workspace configuration applied.")
