from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from extra_settings.admin import SettingAdmin
from extra_settings.cache import get_cached_setting
from extra_settings.encryption import (
    _derive_fernet_keys,
    decrypt_secret,
    encrypt_secret,
)
from extra_settings.forms import SettingForm
from extra_settings.models import Setting

PLAINTEXT = "s3cr3t-plaintext-value"


def is_long_enough(value):
    return len(value) > 100


class MockRequest:
    pass


class MockSuperUser:
    def has_perm(self, perm):
        return True


request = MockRequest()
request.user = MockSuperUser()


class ExtraSettingsSecretsTestCase(TestCase):
    def setUp(self):
        cache.clear()

    def _create(self, name="TEST_SECRET", value=PLAINTEXT, **kwargs):
        return Setting.objects.create(
            name=name, value_type=Setting.TYPE_SECRET, value=value, **kwargs
        )

    def test_round_trip_and_not_stored_in_plaintext(self):
        self._create()
        obj = Setting.objects.get(name="TEST_SECRET")
        self.assertNotIn(PLAINTEXT, obj.value_secret)
        self.assertEqual(obj.value, PLAINTEXT)
        self.assertEqual(Setting.get("TEST_SECRET"), PLAINTEXT)

    def test_empty_secret_falls_back_to_default(self):
        self._create(value="")
        self.assertEqual(Setting.get("TEST_SECRET", "dflt"), "dflt")

    def test_empty_secret_falls_back_to_conf_settings(self):
        self._create(name="EXTRA_SETTINGS_TEST_VALUE", value="")
        self.assertEqual(Setting.get("EXTRA_SETTINGS_TEST_VALUE"), "value")
        self.assertIsNone(get_cached_setting("EXTRA_SETTINGS_TEST_VALUE"))

    def test_name_binding(self):
        a = self._create("SECRET_A", "aaa")
        b = self._create("SECRET_B", "bbb")
        Setting.objects.filter(pk=a.pk).update(value_secret=b.value_secret)
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _ = Setting.objects.get(pk=a.pk).value
        self.assertNotIn("bbb", str(ctx.exception))

    def test_rename_rebinds_token(self):
        obj = self._create()
        obj.name = "TEST_SECRET_RENAMED"
        obj.save()
        obj = Setting.objects.get(name="TEST_SECRET_RENAMED")
        self.assertEqual(obj.value, PLAINTEXT)
        obj.name = "TEST_SECRET_AGAIN"
        obj.save()
        obj.name = "TEST_SECRET_THIRD"
        obj.save()
        self.assertEqual(Setting.objects.get(name="TEST_SECRET_THIRD").value, PLAINTEXT)

    def test_wrong_key_fails_closed(self):
        self._create()
        with override_settings(SECRET_KEY="another-key"):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                _ = Setting.objects.get(name="TEST_SECRET").value
        self.assertNotIn(PLAINTEXT, str(ctx.exception))

    def test_fallback_key_read_rotates_token(self):
        with override_settings(SECRET_KEY="old-key"):
            self._create()
        old_token = Setting.objects.get(name="TEST_SECRET").value_secret
        with override_settings(SECRET_KEY="new-key", SECRET_KEY_FALLBACKS=["old-key"]):
            self.assertEqual(Setting.objects.get(name="TEST_SECRET").value, PLAINTEXT)
            new_token = Setting.objects.get(name="TEST_SECRET").value_secret
            self.assertNotEqual(old_token, new_token)
        with override_settings(SECRET_KEY="new-key"):
            self.assertEqual(Setting.objects.get(name="TEST_SECRET").value, PLAINTEXT)

    def test_rotation_compare_and_swap(self):
        with override_settings(SECRET_KEY="old-key"):
            self._create()
        with override_settings(SECRET_KEY="new-key", SECRET_KEY_FALLBACKS=["old-key"]):
            stale = Setting.objects.get(name="TEST_SECRET")
            Setting.objects.filter(name="TEST_SECRET").update(value_secret="changed")
            stale.rotate_secret()
        self.assertEqual(
            Setting.objects.get(name="TEST_SECRET").value_secret, "changed"
        )

    def test_cache_never_contains_plaintext(self):
        self._create()
        self._create("OTHER", "x", description="d")
        Setting.get("TEST_SECRET")
        Setting.get("OTHER")
        Setting._cache_all_settings()
        self.assertIsNone(get_cached_setting("TEST_SECRET"))
        self.assertIsNone(get_cached_setting("OTHER"))

    def test_validation_error_does_not_leak(self):
        obj = Setting(
            name="TEST_SECRET",
            value_type=Setting.TYPE_SECRET,
            value=PLAINTEXT,
            validator="tests.test_secrets.is_long_enough",
        )
        with self.assertRaises(ValidationError) as ctx:
            obj.validate()
        self.assertNotIn(PLAINTEXT, str(ctx.exception))

    def test_set_defaults_rejects_secret_value(self):
        with self.assertRaises(ValueError):
            Setting.set_defaults(
                [{"name": "DEFAULT_SECRET", "type": "secret", "value": PLAINTEXT}]
            )
        self.assertFalse(Setting.objects.filter(name="DEFAULT_SECRET").exists())
        Setting.set_defaults(
            [{"name": "DEFAULT_SECRET", "type": "secret", "value": ""}]
        )
        self.assertEqual(Setting.objects.get(name="DEFAULT_SECRET").value, "")

    def test_admin_changelist_never_renders_plaintext(self):
        obj = self._create()
        ma = SettingAdmin(model=Setting, admin_site=AdminSite())
        self.assertNotIn("value_secret", ma.get_list_display(request))
        self.assertNotIn("value_secret", ma.list_editable)
        self.assertEqual(ma.secret_status(obj), "set")
        self.assertEqual(ma.secret_status(self._create("EMPTY", "")), "not set")
        fieldsets = ma.get_fieldsets(request, obj)
        self.assertIn("secret_value", fieldsets[0][1]["fields"])
        self.assertNotIn("value_secret", fieldsets[0][1]["fields"])

    def test_form_never_renders_secret_and_blank_keeps_value(self):
        obj = self._create()
        form = SettingForm(instance=obj)
        html = str(form["secret_value"])
        self.assertNotIn(PLAINTEXT, html)
        self.assertNotIn(obj.value_secret, html)
        self.assertNotIn("value_secret", form.fields)
        data = {"name": obj.name, "value_type": "secret", "secret_value": ""}
        form = SettingForm(data, instance=obj)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Setting.objects.get(pk=obj.pk).value, PLAINTEXT)

    def test_form_sets_and_clears_secret(self):
        obj = self._create()
        data = {"name": obj.name, "value_type": "secret", "secret_value": "new"}
        form = SettingForm(data, instance=obj)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Setting.objects.get(pk=obj.pk).value, "new")
        data = {"name": obj.name, "value_type": "secret", "clear_secret_value": "on"}
        form = SettingForm(data, instance=Setting.objects.get(pk=obj.pk))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Setting.objects.get(pk=obj.pk).value, "")

    def test_rotate_command(self):
        with override_settings(SECRET_KEY="old-key"):
            self._create()
        out = StringIO()
        with override_settings(SECRET_KEY="new-key", SECRET_KEY_FALLBACKS=["old-key"]):
            call_command("rotate_extra_settings_secrets", stdout=out)
        self.assertIn("Rotated 1", out.getvalue())
        with override_settings(SECRET_KEY="new-key"):
            self.assertEqual(Setting.objects.get(name="TEST_SECRET").value, PLAINTEXT)

    def test_rotate_command_aborts_on_undecryptable_row(self):
        self._create("GOOD", "good")
        bad = self._create("BAD", "bad")
        Setting.objects.filter(pk=bad.pk).update(value_secret="garbage")
        with self.assertRaises(CommandError):
            call_command("rotate_extra_settings_secrets", stdout=StringIO())
        self.assertEqual(Setting.objects.get(name="GOOD").value, "good")

    def test_missing_cryptography_raises_improperly_configured(self):
        with mock.patch.dict("sys.modules", {"cryptography": None}):
            with self.assertRaises(ImproperlyConfigured):
                encrypt_secret("NAME", "value")

    def test_malformed_payload_fails_closed(self):
        token = Fernet(_derive_fernet_keys()[0]).encrypt(b"not-json").decode("ascii")
        with self.assertRaises(ImproperlyConfigured):
            decrypt_secret("NAME", token)
