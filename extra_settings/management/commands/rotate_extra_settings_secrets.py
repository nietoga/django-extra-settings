from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from extra_settings.choices import SettingType
from extra_settings.models import Setting


class Command(BaseCommand):
    help = (
        "Re-encrypt all secret settings with the current SECRET_KEY. "
        "Put the previous key in SECRET_KEY_FALLBACKS before running it."
    )

    def handle(self, *args, **options):
        rotated = 0
        with transaction.atomic():
            secrets = Setting.objects.filter(value_type=SettingType.SECRET).exclude(
                value_secret=""
            )
            for setting_obj in secrets:
                try:
                    setting_obj.rotate_secret()
                except ImproperlyConfigured as error:
                    raise CommandError(
                        f"Aborted, cannot decrypt secret setting {setting_obj.name!r}."
                    ) from error
                rotated += 1
        self.stdout.write(self.style.SUCCESS(f"Rotated {rotated} secret(s)."))
