import base64
import json

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables

_HKDF_SALT = b"django-extra-settings.secrets"
_HKDF_INFO = b"fernet-key"


def _import_cryptography():
    try:
        from cryptography import fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as error:
        raise ImproperlyConfigured(
            "Secret settings require the 'cryptography' package, "
            "install it with: pip install django-extra-settings[secrets]"
        ) from error
    return fernet, hashes, HKDF


def _derive_fernet_keys():
    """
    Derive one Fernet key per entry of ``SECRET_KEY`` + ``SECRET_KEY_FALLBACKS``,
    the first one is the primary key (used to encrypt).
    """
    _, hashes, HKDF = _import_cryptography()
    secret_keys = [settings.SECRET_KEY]
    secret_keys += list(getattr(settings, "SECRET_KEY_FALLBACKS", None) or [])
    keys = []
    for secret_key in secret_keys:
        raw_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        ).derive(str(secret_key).encode("utf-8"))
        keys.append(base64.urlsafe_b64encode(raw_key))
    return keys


def _payload(name, value):
    return json.dumps([name, value]).encode("utf-8")


@sensitive_variables()
def encrypt_secret(name, value):
    fernet, _, _ = _import_cryptography()
    primary_key = _derive_fernet_keys()[0]
    token = fernet.Fernet(primary_key).encrypt(_payload(name, value))
    return token.decode("ascii")


@sensitive_variables()
def decrypt_secret(name, token):
    """
    Return a ``(value, needs_rotation)`` tuple, ``needs_rotation`` is ``True``
    when the token was not encrypted with the primary key.
    Fail closed with ``ImproperlyConfigured``, never including the value.
    """
    fernet, _, _ = _import_cryptography()
    keys = _derive_fernet_keys()
    error = ImproperlyConfigured(
        f"Unable to decrypt secret setting {name!r}: wrong SECRET_KEY, "
        "corrupted value or setting renamed outside of the ORM."
    )
    plaintext = None
    needs_rotation = False
    for index, key in enumerate(keys):
        try:
            plaintext = fernet.Fernet(key).decrypt(token.encode("ascii"))
        except (fernet.InvalidToken, UnicodeError):
            continue
        needs_rotation = index > 0
        break
    if plaintext is None:
        raise error
    try:
        stored_name, value = json.loads(plaintext)
    except (TypeError, ValueError):
        raise error from None
    if stored_name != name:
        raise error
    return value, needs_rotation
