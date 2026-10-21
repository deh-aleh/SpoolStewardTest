#!/usr/bin/env python3
"""Translation catalogs. Texts are English in the code; spoolsteward/locales/<lang>/messages.po holds the other languages.

  scripts/i18n.py extract            find every _() / ngettext() text in spoolsteward/ -> spoolsteward/locales/messages.pot
                                     and add the new ones (empty) to every language, dropping the ones no longer used
  scripts/i18n.py check              per language: untranslated texts, placeholders that do not match the English
  scripts/i18n.py missing            the English texts still to translate
  scripts/i18n.py add FILE.py        fill translations from a Python file: TRANSLATIONS = {"English": {"it": ..., "es": ...}}
                                     (a plural is a tuple of forms; the English key of a plural is "singular|plural")
"""

import importlib.util
import re
import sys
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "spoolsteward"
LOCALES = APP / "locales"
LANGS = ("it", "es", "ru", "zh")
KEYWORDS = {"_": None, "_t": None, "_js": None, "gettext": None, "ngettext": (1, 2), "_p": ((1, "c"), 2), "pgettext": ((1, "c"), 2), "N_": None, "lazy": None}
METHODS = [("**/templates/**.html", "jinja2"), ("**.py", "python")]
OPTIONS = {"**/templates/**.html": {"extensions": "jinja2.ext.i18n", "silent": "false"}}
PROJECT = "spoolsteward"


def extract_pot() -> Catalog:
    cat = Catalog(project=PROJECT, charset="utf-8")
    for filename, lineno, message, comments, ctx in extract_from_dir(
            str(APP), method_map=METHODS, options_map=OPTIONS, keywords=KEYWORDS, comment_tags=["i18n:"], strip_comment_tags=True):
        cat.add(message, locations=[(f"spoolsteward/{filename}".replace("\\", "/"), lineno)], auto_comments=comments, context=ctx)
    return cat


def load(lang: str) -> Catalog:
    path = LOCALES / lang / "messages.po"
    if path.is_file():
        with path.open("rb") as f:
            return read_po(f, locale=lang)
    return Catalog(locale=lang, project=PROJECT, charset="utf-8")  # plural rules come from the locale


def save(lang: str, cat: Catalog) -> None:
    path = LOCALES / lang / "messages.po"
    cat.fuzzy = False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        write_po(f, cat, width=0, omit_header=False)


def cmd_extract() -> None:
    pot = extract_pot()
    LOCALES.mkdir(exist_ok=True)
    with (LOCALES / "messages.pot").open("wb") as f:
        write_po(f, pot, width=0)
    for lang in LANGS:
        cat = load(lang)
        cat.update(pot, no_fuzzy_matching=True)  # new texts come in empty, unused ones go
        cat.obsolete.clear()
        save(lang, cat)
    print(f"{len(pot)} texts")


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"%\([A-Za-z_]\w*\)[sd]", text))


def cmd_check() -> int:
    bad = 0
    for lang in LANGS:
        cat = load(lang)
        missing, wrong = [], []
        for m in cat:
            if not m.id:
                continue
            ids = m.id if isinstance(m.id, tuple) else (m.id,)
            strs = m.string if isinstance(m.string, tuple) else (m.string,)
            if not any(strs):
                missing.append(ids[0])
            for s in strs:
                if s and _placeholders(s) != _placeholders(" ".join(ids)) and not (_placeholders(s) <= _placeholders(" ".join(ids))):
                    wrong.append(ids[0])
        print(f"{lang}: {len(missing)} untranslated, {len(wrong)} with different placeholders")
        for t in missing[:15]:
            print("   missing:", t[:90])
        for t in wrong[:15]:
            print("   placeholders:", t[:90])
        bad += len(missing) + len(wrong)
    return 1 if bad else 0


def cmd_missing() -> None:
    """The English texts with no Spanish translation yet, one per line (what to write the next batch from)."""
    for m in load("es"):
        if m.id and not any(m.string if isinstance(m.string, tuple) else (m.string,)):
            print(repr("|".join(m.id) if isinstance(m.id, tuple) else m.id))


def cmd_add(path: str) -> None:
    spec = importlib.util.spec_from_file_location("batch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cats = {lang: load(lang) for lang in LANGS}
    unknown = []
    for english, by_lang in mod.TRANSLATIONS.items():
        context = None
        if "::" in english:  # "menu::Backup": the text Backup in the context "menu" (short labels that need their own translation)
            context, english = english.split("::", 1)
        key = tuple(english.split("|")) if "|" in english else english
        if isinstance(by_lang, (tuple, list)):  # plural: one dict per form (singular first) -> {lang: (form, form...)}
            by_lang = {lang: tuple(form[lang] for form in by_lang if lang in form) for lang in by_lang[0]}
        for lang, text in by_lang.items():
            cat = cats[lang]
            msg = cat.get(key, context)
            if msg is None:
                unknown.append(english)
                continue
            if isinstance(key, tuple):
                n = cat.num_plurals
                msg.string = tuple((list(text) + [""] * n)[:n])
            else:
                msg.string = text
    for lang, cat in cats.items():
        save(lang, cat)
    if unknown:
        print("not in the catalogs (run extract first or fix the English):")
        for t in sorted(set(unknown))[:40]:
            print("  ", t[:100])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "extract":
        cmd_extract()
    elif cmd == "missing":
        cmd_missing()
    elif cmd == "check":
        sys.exit(cmd_check())
    elif cmd == "add" and len(sys.argv) == 3:
        cmd_add(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
