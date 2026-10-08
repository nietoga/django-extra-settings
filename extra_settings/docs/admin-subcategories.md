# Subcategories in the admin: investigation

Goal: let admins organize settings into categories and subcategories
(e.g. `AI > Grading > Retries`) instead of one flat, name-sorted list.

## What exists today

- `Setting.name` is a unique `CharField`; `Meta.ordering = ["name"]`.
- `admin.py` has an opt-in `SettingNamePrefixFilter`
  (`EXTRA_SETTINGS_SHOW_NAME_PREFIX_LIST_FILTER`) that derives prefixes by splitting names
  on `_` and keeping those that match more than one but not all names. It is a sidebar
  filter only: flat list, no hierarchy, no ordering control, prefixes are implicit.
- The changelist is a single `list_editable` table with all 15 `value_*` columns;
  `extra_settings.js` hides the irrelevant cells per row and un-hides the table via the
  `extra-settings-ready` CSS class.
- `register_extra_settings_admin()` already builds a **dynamic proxy model + ModelAdmin**
  and can move the admin into another app group (`EXTRA_SETTINGS_ADMIN_APP`).
- No templates ship with the package, so any grouped changelist needs new templates.

## Data model options

| Option | Schema change | Pros | Cons |
|---|---|---|---|
| **A. Derive from name** (extend prefix filter) | none | zero migration, works on existing rows | implicit; fragile (`_` appears in real names); no labels/order; can't express a category that differs from the name |
| **B. `category` string field** with a separator, e.g. `AI/Grading` | 1 migration | explicit, simple, nesting via separator, easy to filter (`category__startswith`), set from `EXTRA_SETTINGS_DEFAULTS` | free-text typos create stray categories; no per-category metadata |
| **C. `Category` model** (self-FK tree) | 1-2 migrations | real hierarchy, labels, ordering, descriptions | heaviest; admin for categories; FK on every setting; defaults format needs category references |

**Recommendation: B.** It's the smallest explicit model. Add
`category = CharField(max_length=255, blank=True, db_index=True)` and a
`EXTRA_SETTINGS_CATEGORY_SEPARATOR = "/"` option; `set_defaults` accepts an optional
`"category"` key (only applied on creation, like `description`/`validator`). Change
`Meta.ordering` to `["category", "name"]`. Per `AGENTS.md`, no upgrade path is needed:
update seeds/defaults and recreate dev data.

## Admin presentation options

1. **Hierarchical list filter** (cheapest). Replace/augment `SettingNamePrefixFilter`
   with a `CategoryFilter` whose lookups are the distinct category paths (with counts) and
   whose `queryset` uses `category = X OR category startswith X + sep`. Works with the
   existing table and JS untouched. Weakness: still one flat table.
2. **Grouped changelist** (best UX/effort). Override `change_list_template` and render rows
   in collapsible sections (`<details>`) per category path, nested for subcategories.
   Implementation notes:
   - Django's `result_list` tag emits one table; either post-process in JS (insert
     category header rows into `tbody` based on a `field-category` cell, then add
     expand/collapse), or override `result_list` rendering and `{% regroup %}` the
     `cl.result_list` by `category`.
   - JS approach is least invasive and fits the existing `extra_settings.js` pattern, but
     `list_editable` rows are still submitted as one formset, which is fine because rows
     stay inside the same `<form>`.
   - Pagination (`list_per_page`) splits categories across pages; either raise
     `list_per_page` or paginate by top-level category.
3. **One admin entry per category via proxy models.** Reuse the
   `register_extra_settings_admin` pattern to generate a proxy model + admin per top-level
   category, each with `get_queryset` filtered by category and `verbose_name_plural` set
   to the category label. Appears as separate sidebar links under the app group.
   Caveats: proxy models are created at import time, so categories must be known
   statically (from settings, not the DB); each needs a unique class name/`app_label`
   handling; the current helper hard-codes the class name `"Setting"`, which would collide.
   Good for a few fixed top-level groups, poor for dynamic/nested ones.
4. **Tabs / fieldsets on a single page** (what `site_config` does: one form, module-owned
   groups). Doesn't fit this library's one-row-per-setting model; would require a custom
   view that builds a formset per category. Highest cost.

**Recommendation:** B (data) + 1 and 2 (admin): ship the hierarchical filter first,
then the grouped changelist through a template + small JS that reads the category cell.
Option 3 only if users want separate sidebar entries.

## Implementation sketch (B + 1 + 2)

1. `choices.py`/`settings.py`: add `EXTRA_SETTINGS_CATEGORY_SEPARATOR` (default `"/"`).
2. `models.py`: add `category` field; include it in `set_defaults` (creation only);
   `clean()` normalizes it (strip, collapse repeated separators, reject empty segments).
3. `admin.py`:
   - add `category` to `list_display` (hidden by JS/CSS when grouped) and to the
     fieldsets in `get_fieldsets` for both add and change.
   - `CategoryFilter` (SimpleListFilter) with indented labels per depth.
   - `change_list_template = "admin/extra_settings/setting/change_list.html"`.
   - Preserve `register_extra_settings_admin` behaviour for the category fields.
4. `static/extra_settings/js/extra_settings.js`: after hiding value columns, group rows by
   the `.field-category` text, insert header rows with a toggle, indent by depth, persist
   collapsed state in `localStorage` (wrapped in try/catch).
5. `cache.py`: unaffected (cache is keyed by setting name).
6. Tests in `tests/test_admin.py` / `test_models.py`: normalization, defaults with
   category, filter lookups and queryset (parent includes children), ordering, changelist
   renders grouped headers, `list_editable` still saves.

## Risks / open questions

- `list_editable` + grouping: header rows must not break the row-index-based formset
  (`form-N-*`); insert non-form rows only.
- Name prefix filter and category filter overlap; probably deprecate the prefix filter
  (no compat needed per `AGENTS.md`) once categories exist.
- Whether categories should be required (strict) or optional (uncategorized bucket shown
  last). Suggest optional.
- Interaction with secrets (see `secrets-design.md`): the indicator column and the
  grouped layout should treat `secret` rows identically; categories never reveal values.
