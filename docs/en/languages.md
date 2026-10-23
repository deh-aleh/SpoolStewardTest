# Languages (i18n)

[Documentation index](../README.md) · English · [Italiano](../it/lingue.md)

The portal speaks **English** (the language of the source code), **Italian**, **Spanish**, **Russian** and **Simplified Chinese**.

> The Spanish, Russian and Chinese translations have not been reviewed by native speakers.

## 1. Which language a page uses

In this order: the user's choice in **Settings → Language** (stored with the account, so it follows the user); the browser language (`Accept-Language`); `SPOOLSTEWARD_LANGUAGE` (default `en`); English.
A text with no translation is shown in English and never breaks a page. Pages follow the language of the person looking: an administrator who opens the audit sees it in their own language.

**The login page** has no user yet, so it uses the browser language, then the default. Its help sentence depends on the installation ("Log in with your company (RADIUS) or local account." with RADIUS, "Log in with your local account." without); both are translated in every language, and a new sentence on that page must be too.

## 2. What is *not* translated, on purpose

- The audit text in `job_log` (rejection reasons stay English because IPP clients also see them; the portal shows the same sentence translated when it refuses a print).
- Server logs, `spoolsteward-admin` commands and the command-line messages of backup/restore; protocol-level HTTP answers (403, 404…).
- Texts that come from printers or CUPS (ink names, firmware, device alerts).
- **PDF pages printed by the gateway** (test and maintenance pages): always English; Russian and Chinese would need an embedded font (`FONT_LANGUAGES` in `spoolsteward/policy/testpage.py`).
- Dates and numbers (fixed ISO format).

How to add or fix a translation, and the rules for translatable code: [CONTRIBUTING.md](../../CONTRIBUTING.md#4-translations-and-documentation).
