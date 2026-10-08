# Secret settings: design analysis

Analysis of how the encrypted-secrets feature of this repo's `backend/src/apps/site_config`
(Moodle Copy Autograder) would map onto `django-extra-settings`.

## Reference implementation (`site_config`)

**Storage** (`models.py`, `encryption.py`)
- Ciphertext lives in `encrypted_secrets`, a non-editable JSON dict `{key: token}` on a
  single-row config record.
- No separate encryption key: Fernet keys are derived from `SECRET_KEY` and
  `SECRET_KEY_FALLBACKS` with HKDF-SHA256 and a fixed domain salt. `MultiFernet` decrypts
  with fallback keys.
- Plaintext is `json.dumps([key, value])`, so a token is bound to its setting name;
  swapping tokens between rows fails decryption.
- Decryption fails closed with `ImproperlyConfigured` and never includes the value in the
  message. `@sensitive_variables` keeps plaintext out of tracebacks.

**Declaration** (`registry.py`): modules declare `Secret(key, help_text, ...)`. Each gets a
write-only form field plus a `<key>__clear` checkbox; blank means "keep current".

**Read/write** (`services.py`)
- `get_secret(key)` reads from the DB on every call (no cache), so changes apply across
  workers immediately.
- A read that needed a fallback key re-encrypts and saves, only if the stored token is
  unchanged (compare-and-swap).
- Saving requires an authorized actor; history stores key names and actions (`set`/`clear`),
  never values. Admin shows only "configured / not configured".
- Ops: `rotate_site_secrets` re-encrypts everything in one locked transaction;
  `import_site_settings` imports `SITE_SECRET__*` dotenv entries; seeds reject secrets.

## State of `django-extra-settings` today

One row per setting, typed `value_<type>` columns selected by `value_type`
(`SettingType` in `choices.py`), a cache layer, and a plaintext admin form. Relevant leaks
if a secret were stored as an existing type:

- Values are stored in plaintext.
- `SettingAdmin.list_display` / `list_editable` render every `value_*` column.
- `Setting._cache_all_settings` / `_get_from_database` put `setting_obj.value` into the
  cache backend in the clear.
- `Setting.validate` interpolates `{self.value!r}` into its `ValidationError`.
- `Setting.set_defaults` / `EXTRA_SETTINGS_DEFAULTS` would put values in settings files.
- No history or audit trail exists.

## Proposed mapping

1. **New type `SettingType.SECRET`** with a `value_secret = TextField(blank=True)` column
   holding the Fernet token. The existing `value` property (`value_field_name`) is the
   natural hook: the getter decrypts, the setter encrypts. Adds a migration (column + new
   `value_type` choice).
2. **Encryption module** (`extra_settings/encryption.py`) adapted from `site_config`:
   - Derive keys from `settings.SECRET_KEY` + `SECRET_KEY_FALLBACKS`, HKDF salt
     `b"django-extra-settings.secrets"`.
   - Bind to the setting name: `json.dumps([name, value])`. Renaming a secret setting then
     requires re-entering it (acceptable; document it).
   - Same fail-closed error handling and `@sensitive_variables`.
3. **Cache**: never cache plaintext. Simplest: `Setting.get` skips `set_cached_setting` for
   `secret` rows and `_cache_all_settings` filters them out. Cost: one query per secret
   read (same trade-off `site_config` makes). Alternative, cache the ciphertext and decrypt
   per read, saves the query but adds complexity; not recommended initially.
4. **`conf.settings` fallback**: `_get_from_settings` stays valid and is useful: a secret
   can be deployment-managed (env-driven `settings.X`) and never touch the DB.
5. **Admin / form**
   - Exclude `value_secret` from `list_display` and `list_editable`; show a
     "set / not set" indicator column instead.
   - Edit form: `PasswordInput(render_value=False)` plus a "clear" checkbox; blank keeps the
     stored value (mirrors `Secret.form_field`).
   - `extra_settings.js` hides non-matching `value_*` cells by type; `secret` needs no
     special-casing beyond the indicator column, but verify the "last visible header" logic.
   - `Setting.validate`: do not echo the value in the error when type is secret.
6. **`set_defaults`**: reject a non-empty `value` for `secret` items so secrets can't be
   committed via `EXTRA_SETTINGS_DEFAULTS`; allow creating the empty slot.
7. **Rotation**: management command `rotate_extra_settings_secrets` re-encrypting all
   `secret` rows in one `transaction.atomic()` with `select_for_update()`, aborting on any
   undecryptable row. Lazy rotate-on-read can be added later.
8. **Tests**: round-trip, name binding (swap tokens), fallback-key read, rotation,
   cache never contains plaintext, admin changelist/form never renders plaintext,
   validation error doesn't leak, defaults reject secrets.

## Open decisions

- **Ownership**: this directory is an upstream checkout with its own `.git` and CI. These
  changes imply a fork or an upstream PR.
- **Overlap**: `site_config` already provides typed, module-owned settings with history,
  optimistic concurrency and secrets. Decide whether the library replaces it or only
  informs it.
- **Auditing**: encryption alone is covered; "who changed which secret" needs a history
  model the library doesn't have (could use `LogEntry` from Django admin).
- Per the repo's `AGENTS.md`: no backward-compat shims; development data is recreated.
