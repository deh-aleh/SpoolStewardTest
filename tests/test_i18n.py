"""Interface languages: how the language of a request is chosen, that the user's choice sticks, and that the catalogs are complete."""

import importlib.util
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from spoolsteward import i18n
from spoolsteward.db.models import PermissionGrant, SubjectType, User
from spoolsteward.policy import permissions
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"


def test_negotiation():
    assert i18n.negotiate("it-IT,it;q=0.9,en;q=0.8") == "it"
    assert i18n.negotiate("fr;q=0.9, ru;q=0.5, es;q=0.7") == "es"
    assert i18n.negotiate("zh-TW,zh;q=0.9") == "zh" and i18n.negotiate("zh-Hans-CN") == "zh"
    assert i18n.negotiate("fr, de") is None and i18n.negotiate("") is None and i18n.negotiate(None) is None
    assert i18n.negotiate("it;q=0, en") == "en"                      # q=0 means "not acceptable"
    assert i18n.negotiate("it;q=abc, es") == "es"
    assert i18n.match("EN_us") == "en" and i18n.match("xx") is None


def test_translation_falls_back_to_english_and_formats():
    tok = i18n.set_language("it")
    try:
        assert i18n._("Save") == "Salva"
        assert i18n._("A text nobody translated %(x)s", x=1) == "A text nobody translated 1"
        assert i18n._("Automatic refresh every %(n)s seconds", n=7) == "Aggiornamento automatico ogni 7 secondi"
    finally:
        i18n.reset(tok)
    assert i18n._("Save") == "Save"                                  # no language set: the installation default (English)
    tok = i18n.set_language("xx")                                    # unknown code is ignored
    assert i18n.current() == "en"
    i18n.reset(tok)


async def test_page_follows_the_browser_then_the_users_choice(portal):
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    assert "<html lang=\"en\"" in (await portal.get("/login")).text
    r = await portal.get("/login", headers={"Accept-Language": "ru,en;q=0.5"})
    assert '<html lang="ru"' in r.text
    await portal_login(portal, "ann", GOOD)
    assert "<h1>Settings</h1>" in (await portal.get("/me/settings")).text
    tok = await csrf_of_page(portal, "/me/settings")
    r = await portal.post("/me/settings/language", data={"csrf_token": tok, "language": "it"})
    assert r.status_code == 303
    page = (await portal.get("/me/settings", headers={"Accept-Language": "ru"})).text      # the choice beats the browser
    assert "<h1>Impostazioni</h1>" in page and "Lingua salvata" in page and '<html lang="it"' in page
    with portal.sm() as s:
        assert s.scalars(select(User).where(User.username == "ann")).one().language == "it"
    await portal.post("/me/settings/language", data={"csrf_token": tok, "language": ""})   # back to automatic
    assert '<html lang="es"' in (await portal.get("/me/settings", headers={"Accept-Language": "es"})).text
    r = await portal.post("/me/settings/language", data={"csrf_token": tok, "language": "xx"})
    assert r.status_code == 400
    assert (await portal.post("/me/settings/language", data={"language": "it"})).status_code == 403   # CSRF


async def test_the_installation_language_is_used_when_the_browser_asks_for_none(portal, italian):
    assert i18n._("Save") == "Salva"                                 # no language set: the installation default
    page = (await portal.get("/login")).text
    assert '<html lang="it"' in page and "Accedi" in page
    assert '<html lang="ru"' in (await portal.get("/login", headers={"Accept-Language": "ru"})).text   # the browser still wins


async def test_the_choice_follows_the_user_to_a_new_login(portal):
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    await portal_login(portal, "ann", GOOD)
    await portal.post("/me/settings/language", data={"csrf_token": await csrf_of_page(portal, "/me/settings"), "language": "zh"})
    await portal.post("/logout", data={"csrf_token": await csrf_of_page(portal, "/me/settings")})
    await portal_login(portal, "ann", GOOD)
    assert '<html lang="zh"' in (await portal.get("/me/settings")).text


# ---------------------------------------------------------------- the catalogs

def _tool():
    spec = importlib.util.spec_from_file_location("i18n_tool", Path(__file__).resolve().parent.parent / "scripts" / "i18n.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_catalogs_are_complete_and_placeholders_match():
    tool = _tool()
    pot = {m.id for m in tool.extract_pot() if m.id}
    for lang in tool.LANGS:
        cat = tool.load(lang)
        have = {m.id for m in cat if m.id}
        assert pot == have, f"{lang}: run scripts/i18n.py extract (missing {sorted(map(str, pot - have))[:3]}, stale {sorted(map(str, have - pot))[:3]})"
        for m in cat:
            if not m.id:
                continue
            ids = m.id if isinstance(m.id, tuple) else (m.id,)
            strs = m.string if isinstance(m.string, tuple) else (m.string,)
            assert all(strs), f"{lang}: not translated: {ids[0][:70]}"
            for s in strs:
                assert tool._placeholders(s) == tool._placeholders(ids[0]) or tool._placeholders(s) <= tool._placeholders(" ".join(ids)), \
                    f"{lang}: placeholders differ: {ids[0][:70]}"
                assert not re.search(r"%(?!\(|%)", s.replace("%%", "")), f"{lang}: lone % in {s[:70]}"


def test_every_language_loads_from_its_po_file():
    for code in i18n.LANGUAGES:
        if code != i18n.SOURCE:
            assert (i18n.LOCALES / code / "messages.po").is_file(), code
            assert type(i18n._catalog(code)).__name__ == "Translations", code


# ---------------------------------------------------------------- every page, every language

PAGES = ["/admin/jobs", "/admin/users", "/admin/groups", "/admin/groups/new", "/admin/requests", "/admin/actions",
         "/admin/backups", "/admin/archive", "/admin/users/{ann}/edit", "/admin/users/{ann}/keys", "/admin/users/{ann}/delete",
         "/me", "/me/keys", "/me/settings", "/me/print", "/me/scan", "/me/request/quota", "/me/request/group",
         "/me/request/permission"]


@pytest.mark.parametrize("code", sorted(i18n.LANGUAGES))
async def test_every_page_renders_in_every_language(portal, code):
    """A text with a missing or misspelt %(name)s would raise while rendering: this walks the pages in each language."""
    from spoolsteward.db.models import Group

    ann = make_user(portal.sm, "ann", password=GOOD, group="staff")
    boss = make_user(portal.sm, "boss", password=GOOD, admin=True)
    with portal.sm() as s:
        s.add(Group(name="power", requestable=True))
        permissions.set_rule(s, SubjectType.user, boss, color=False, one_sided=False, hours_mode="window",
                             hours_days="01234", hours_start=480, hours_end=1020)   # something to ask for on /me/request/permission
        s.add(PermissionGrant(user_id=ann, perm="color", valid_from=datetime.now(UTC), valid_until=datetime.now(UTC) + timedelta(days=9)))
        s.commit()
    await portal_login(portal, "boss", GOOD)
    for path in PAGES:
        r = await portal.get(path.format(ann=ann), headers={"Accept-Language": code})
        assert r.status_code == 200, (code, path, r.status_code)
        assert f'<html lang="{code}"' in r.text, (code, path)
        assert "%(" not in re.sub(r"<script.*?</script>", "", r.text, flags=re.S), (code, path)   # (the scripts hold templates on purpose)


@pytest.mark.parametrize("code", sorted(set(i18n.LANGUAGES) - {"en"}))
def test_a_translation_is_not_the_english_text_left_in_place(code):
    """Most texts differ from their English original in every language (a handful are names or symbols)."""
    tool = _tool()
    cat = tool.load(code)
    same = [m.id for m in cat if m.id and isinstance(m.id, str) and m.string == m.id and any(c.isalpha() for c in m.id)]
    assert len(same) < 0.08 * len(cat), f"{code}: {len(same)} texts still in English, e.g. {same[:5]}"
