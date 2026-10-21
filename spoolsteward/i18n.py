"""Interface languages. Texts are written in English in the code (`_("...")`) and translated through catalogs
(`spoolsteward/locales/<language>/messages.po`), which are read when first needed and compiled in memory: no build step.

The language of a request is, in order: the one the signed-in user chose in their profile (kept in the session),
the browser's Accept-Language, the installation default (`SPOOLSTEWARD_LANGUAGE`), English. A text with no translation
is shown in English, so a missing translation never breaks a page.

Rules for the texts: variables are named (`_("Key %(label)s revoked", label=x)`), never concatenated; a literal
percent sign is written `%%` (every text goes through % formatting)."""

import io
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import NullTranslations, Translations
from markupsafe import Markup

from spoolsteward.config import settings

log = logging.getLogger(__name__)
LOCALES = Path(__file__).with_name("locales")
SOURCE = "en"
# code -> name in the language itself (never translated: whoever picked the wrong language must still find theirs)
LANGUAGES = {"en": "English", "it": "Italiano", "es": "Español", "ru": "Русский", "zh": "中文（简体）"}
_current: ContextVar[str | None] = ContextVar("language", default=None)


def default_language() -> str:
    return settings.language if settings.language in LANGUAGES else SOURCE


def current() -> str:
    return _current.get() or default_language()


def set_language(code: str | None):
    """Language for the code that runs next (a request, a scheduled job). Returns the token for `reset`."""
    return _current.set(code if code in LANGUAGES else None)


def reset(token) -> None:
    _current.reset(token)


def match(code: str | None) -> str | None:
    """'it-IT' -> 'it', 'zh-Hans-CN' / 'zh-TW' -> 'zh'; None if the language is not one we have."""
    base = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return base if base in LANGUAGES else None


def negotiate(accept_language: str | None) -> str | None:
    """The best supported language of an Accept-Language header, or None."""
    ranked = []
    for part in (accept_language or "").split(",")[:20]:
        tag, _, params = part.partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        lang = match(tag)
        if lang and q > 0:
            ranked.append((-q, len(ranked), lang))
    return min(ranked)[2] if ranked else None


@lru_cache(maxsize=None)
def _catalog(code: str):
    path = LOCALES / code / "messages.po"
    if code == SOURCE or not path.is_file():
        return NullTranslations()
    try:
        with path.open("rb") as f:
            catalog = read_po(f, locale=code)
        buf = io.BytesIO()
        write_mo(buf, catalog)
        buf.seek(0)
        return Translations(buf)
    except Exception:  # a broken catalog must not take the portal down: English is shown instead
        log.exception("could not load the %s translations", code)
        return NullTranslations()


def gettext(message: str, /, **kw) -> str:
    return _catalog(current()).gettext(message) % kw


def ngettext(singular: str, plural: str, n: int, /, **kw) -> str:
    kw.setdefault("n", n)
    return _catalog(current()).ngettext(singular, plural, n) % kw


@contextmanager
def using(code: str | None):
    """Run a block in another language (generated documents, scheduled work)."""
    token = set_language(code)
    try:
        yield
    finally:
        reset(token)


def lazy(message: str) -> str:
    """Marks a text to be translated later (module-level tables); translate it with `_(...)` where it is shown."""
    return message


def html_gettext(message: str, /, **kw) -> Markup:
    """For templates: the text is trusted (ours, or a translator's), the values are escaped."""
    return Markup(_catalog(current()).gettext(message)) % kw


def raw_gettext(message: str) -> str:
    """Translated, NOT formatted: for texts a script fills in itself (their %(name)s stay as they are)."""
    return _catalog(current()).gettext(message)


def plain_gettext(message: str, /, **kw) -> str:
    """For templates where the text goes into JavaScript (confirm dialogs): translated and formatted, not HTML-escaped."""
    return gettext(message, **kw)


def html_pgettext(context: str, message: str, /, **kw) -> Markup:
    """Like html_gettext, for a text whose translation depends on where it is used (the short menu labels)."""
    return Markup(_catalog(current()).pgettext(context, message)) % kw


def html_ngettext(singular: str, plural: str, n: int, /, **kw) -> Markup:
    kw.setdefault("n", n)
    return Markup(_catalog(current()).ngettext(singular, plural, n)) % kw


_ = gettext
N_ = lazy


class LanguageMiddleware:
    """Sets the language of each request (see the module docstring). Must sit INSIDE the session middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        chosen = match((scope.get("session") or {}).get("lang"))
        if chosen is None:
            header = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"accept-language"), "")
            chosen = negotiate(header)
        token = set_language(chosen)
        try:
            await self.app(scope, receive, send)
        finally:
            reset(token)


# Words for stored values (enum members, states): listed so the extractor finds them; templates show them with _(value).
VALUE_LABELS = (
    N_("pending"), N_("approved"), N_("rejected"), N_("expired"), N_("canceled"),
    N_("accepted"), N_("denied"), N_("completed"), N_("failed"), N_("held"),
    N_("active"), N_("revoked"), N_("ended"), N_("temporary"), N_("permanent"),
    N_("jobs"), N_("pages"), N_("weekly"), N_("monthly"), N_("page"), N_("job"), N_("missing"), N_("modified"),
)
