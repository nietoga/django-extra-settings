from django import VERSION, forms
from django.conf import settings

from extra_settings.choices import SettingType
from extra_settings.models import Setting
from extra_settings.utils import enforce_uppercase_setting


class _AssumeHTTPSURLField(forms.URLField):
    """
    URLField subclass that assumes HTTPS scheme for scheme-less URLs.
    Only since Django version 5.0.
    """

    def __init__(self, *args, **kwargs):
        if VERSION >= (5, 0):
            kwargs.setdefault("assume_scheme", "https")
        super().__init__(*args, **kwargs)


class SettingForm(forms.ModelForm):
    secret_value = forms.CharField(
        required=False,
        label=Setting._meta.get_field("value_secret").verbose_name,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="Leave blank to keep the current value.",
    )
    clear_secret_value = forms.BooleanField(
        required=False,
        label="Clear value",
    )

    class Meta:
        model = Setting
        fields = "__all__"
        exclude = ("value_secret",)
        field_classes = {
            "value_url": _AssumeHTTPSURLField,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.value_type != SettingType.SECRET:
            del self.fields["secret_value"]
            del self.fields["clear_secret_value"]
        # resize text area
        if "description" in self.fields:
            self.fields["description"].widget = forms.Textarea(
                attrs={"rows": 3, "cols": 51}
            )
        if "value_text" in self.fields:
            self.fields["value_text"].widget = forms.Textarea(
                attrs={"rows": 5, "cols": 51}
            )

    def clean_name(self):
        value = self.cleaned_data.get("name", "")
        if settings.EXTRA_SETTINGS_ENFORCE_UPPERCASE_SETTINGS:
            value = enforce_uppercase_setting(value)
        if hasattr(settings, value):
            raise forms.ValidationError(
                f"Invalid setting name, settings.{value} already "
                "defined in django.conf.settings."
            )
        return value

    def _post_clean(self):
        if self.instance.is_secret:
            if self.cleaned_data.get("clear_secret_value"):
                self.instance.value = ""
            elif self.cleaned_data.get("secret_value"):
                self.instance.value = self.cleaned_data["secret_value"]
        super()._post_clean()
