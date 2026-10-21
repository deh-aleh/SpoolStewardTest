# Languages (i18n)

[Documentation index](../README.md) · English · [Italiano](../it/lingue.md)

The portal speaks **English** (the language of the source code), **Italian**, **Spanish**, **Russian** and **Simplified Chinese**.

> The Spanish, Russian and Chinese translations have not been reviewed by native speakers.

## 1. Which language a page uses

In this order:

1. the user's choice in **Settings → Language** (stored with the account, so it follows the user on every device);
2. the browser language (`Accept-Language`);
3. `SPOOLSTEWARD_LANGUAGE` (default `en`);
4. English.

A text with no translation is shown in English: it never breaks a page. Pages follow the language of the person who is
looking: an administrator who opens the audit sees it in their own language.

**The login page** has no user yet, so it always uses the browser language (step 2), then the default. Its help sentence depends on the
installation: with RADIUS configured it says "Log in with your company (RADIUS) or local account."; without RADIUS only "Log in with your
local account.". Both sentences are translated in every language; a new sentence on that page must be too.


## 2. What is *not* translated, on purpose

- The audit text stored in `job_log` (rejection reasons stay English because IPP clients also see them; the portal shows the
  same sentence translated when it refuses a print).
- Server logs, `spoolsteward-admin` commands and the command-line messages of backup/restore.
- Protocol-level HTTP answers (403, 404…).
- Texts that come from printers or CUPS (ink names, firmware, device alerts).
- **PDF pages printed by the gateway** (test and maintenance pages): always English. Russian and Chinese can not be written with
  the base PDF fonts; they would need an embedded font (`FONT_LANGUAGES` in `app/policy/testpage.py`).
- Dates and numbers are not localised (fixed ISO format).

## 3. Adding or fixing a translation

Catalogs are gettext files: `app/locales/<lang>/messages.po` (`msgid` = English text, `msgstr` = translation). They are read at
start and compiled in memory with Babel — no `.mo` files, no build step. Keep `%(name)s` placeholders unchanged and write a literal `%` as `%%`.

```sh
python scripts/i18n.py extract    # refresh the catalogs from the code
python scripts/i18n.py missing    # what still lacks a translation
python scripts/i18n.py check      # completeness and placeholders
pytest tests/test_i18n.py         # fails if a catalog is incomplete or has wrong placeholders
```

New language: add it to `LANGUAGES` in `app/i18n.py` and to `LANGS` in `scripts/i18n.py`, then run `extract`.

## 4. Rules for people writing code

- Every visible text is English inside `_("...")` (Python: `from app.i18n import _`; templates: `{{ _("...") }}`).
- Menu labels are short on purpose and may differ from the page titles: they use `_p("menu", "Backup")` (same text, context `menu`). To translate one, give `scripts/i18n.py add` the key `"menu::Backup"`.
- Values are always named (`_("Key %(label)s revoked", label=x)`), never concatenated.
- Plurals use `ngettext` (Russian has three forms).
- In templates, text for JavaScript uses `_js(...)` (unformatted, `%(name)s` replaced by the script) or `_t(...)|tojson` (for `confirm()`).
- Cached data (queue states, counter labels) are English *keys* translated only when shown. Enumerations are translated with `_(value)` (list in `VALUE_LABELS`).
- `Decision.reason` (audit, IPP clients) stays English; `Decision.text()` is the localised sentence for the portal.

## 5. How it works

`app/i18n.py` keeps the request language in a `ContextVar` set by an ASGI middleware that sits inside the session middleware.
The `ContextVar` follows the request into the threads of synchronous handlers and into tasks it starts (portal scan), so `_()`
works everywhere in a request. Outside a request (schedulers) the default applies. `_()` returns trusted Markup in templates;
`_t()` returns plain text. `user.language` (migration 0007) is written at login and re-aligned if it changes from another device.
