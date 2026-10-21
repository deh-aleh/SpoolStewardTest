"""The documentation inside the portal: every page for administrators, the user guides for the others, only real page
names, safe rendering, rewritten links. The pictures have their own tests in test_docs_images.py."""

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from spoolsteward.admin import docs
from spoolsteward.config import Settings
from spoolsteward.db.models import User
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent


def set_language(cl, name, code):
    with cl.sm() as s:
        s.scalars(select(User).where(User.username == name)).one().language = code
        s.commit()


@pytest.fixture
async def boss(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    set_language(portal, "boss", "en")
    await portal_login(portal, "boss", GOOD)
    return portal


async def test_home_goes_to_the_language_of_the_portal(boss):
    r = await boss.get("/docs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/docs/en/"
    set_language(boss, "boss", "it")
    await boss.get("/me")  # the session picks up the language chosen on another device
    r = await boss.get("/docs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/docs/it/"


async def test_index_lists_the_pages_of_that_language(boss):
    en = (await boss.get("/docs/en/")).text
    assert 'href="/docs/en/backup"' in en and "Backup and restore" in en
    assert 'href="/docs/en/configuration"' in en
    assert 'href="/docs/it/' not in en.split('class="doctiles"')[1]
    it = (await boss.get("/docs/it/")).text
    assert 'href="/docs/it/backup"' in it and 'href="/docs/it/configurazione"' in it
    # the notes for contributors in docs/images are not a page
    assert "images" not in en.split('class="doctiles"')[1] and "images" not in it.split('class="doctiles"')[1]
    assert (await boss.get("/docs/images/README.md")).status_code == 404


async def test_a_page_has_heading_ids_rewritten_links_and_the_language_switch(boss):
    r = await boss.get("/docs/en/backup")
    assert r.status_code == 200
    assert '<h3 id="save-the-secret-key">' in r.text
    assert 'href="/docs/en/data-protection"' in r.text      # [data-protection.md](data-protection.md)
    assert 'href="/docs/en/"' in r.text                      # back to the index
    # the "English · Italiano" line leads to the same page in the other language
    assert re.search(r'class="langsw".*?href="/docs/it/backup"', r.text, re.S)
    r = await boss.get("/docs/it/backup")
    assert re.search(r'class="langsw".*?href="/docs/en/backup"', r.text, re.S)
    r = await boss.get("/docs/it/account")
    assert 'href="/docs/en/accounts"' in r.text              # different file names, same page


async def test_other_users_see_only_the_user_guide_and_the_windows_guide(portal):
    make_user(portal.sm, "ann", password=GOOD)
    set_language(portal, "ann", "en")
    await portal_login(portal, "ann", GOOD)
    index = (await portal.get("/docs/en/")).text
    assert 'href="/docs/en/user-guide"' in index and 'href="/docs/en/windows"' in index
    assert 'href="/docs/en/architecture"' not in index and 'href="/docs/en/configuration"' not in index
    assert (await portal.get("/docs/en/windows")).status_code == 200
    assert (await portal.get("/docs/it/guida-utente")).status_code == 200
    for path in ("/docs/en/architecture", "/docs/en/application-internals", "/docs/en/configuration", "/docs/it/architettura"):
        assert (await portal.get(path, follow_redirects=False)).status_code == 404
    assert (await portal.get("/docs/windows", follow_redirects=False)).headers["location"] == "/docs/en/windows"


async def test_not_logged_in_goes_to_the_login(portal):
    r = await portal.get("/docs/en/backup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", [
    "/docs/fr/", "/docs/fr/backup", "/docs/en/nothing", "/docs/en/backup.md",
    "/docs/en/..%2F..%2Fapp%2Fconfig", "/docs/en/..%2F..%2Fapp%2Fconfig.py", "/docs/en/%2e%2e",
    "/docs/en/README", "/docs/..%2Fen/backup", "/docs/en/%00",
])
async def test_unknown_language_or_page_is_404(boss, path):
    assert (await boss.get(path)).status_code == 404


def render(text, lang="en", names=None):
    return docs.render_markdown(text, lang, names or {"en": {"backup", "x"}, "it": {"backup"}})[0]


def test_raw_html_is_shown_as_text():
    html = render("# T\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)> and `<b>`\n\n[a](javascript:alert(1))")
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
    assert "href" not in html                                        # markdown-it does not make a link of javascript:


def test_links_are_rewritten():
    h = render("[a](backup.md#save-the-secret-key) [b](../it/backup.md) [c](../README.md#x) [d](#local) "
               "[e](../../spoolsteward/config.py) [f](../../README.it.md) [g](https://example.com/p) [h](nothing.md)", "en")
    assert 'href="/docs/en/backup#save-the-secret-key"' in h
    assert 'href="/docs/it/backup"' in h
    assert 'href="/docs/en/#x"' in h
    assert 'href="#local"' in h and 'href="#local" target' not in h
    version = docs.app_version()
    ref = f"v{version}" if version != "unknown" else "main"
    assert f'href="https://github.com/deh-aleh/SpoolSteward/blob/{ref}/spoolsteward/config.py" target="_blank" rel="noopener noreferrer"' in h
    assert f"blob/{ref}/README.it.md" in h
    assert 'href="https://example.com/p" target="_blank" rel="noopener noreferrer"' in h
    assert f"blob/{ref}/docs/en/nothing.md" in h                  # not a page of the portal: GitHub (the file may be there)


def test_links_that_escape_or_are_not_web_pages_are_not_live():
    for bad in ("/etc/passwd", "//evil.example/x", "../../../etc/passwd", "mailto:a@b.c", "ftp://x/y", "file:///etc/passwd"):
        assert docs.rewrite_link(bad, "en", {})[0] == "#", bad


def test_heading_ids_follow_github_and_repeat_with_a_number():
    h = render("# One\n\n## 1. Built-in (menu *Backup*)\n\n## Résumé: ça va\n\n## Same\n\n## Same\n\n## Same\n")
    assert 'id="1-built-in-menu-backup"' in h
    assert 'id="résumé-ça-va"' in h
    assert 'id="same"' in h and 'id="same-1"' in h and 'id="same-2"' in h


def test_a_wide_table_is_scrollable_inside_the_card():
    h = render("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in h


async def test_missing_docs_folder_gives_a_friendly_page(boss, monkeypatch):
    monkeypatch.setattr(docs, "docs_root", lambda: None)
    for path in ("/docs/en/", "/docs/it/backup"):
        r = await boss.get(path)
        assert r.status_code == 404 and "not installed" in r.text
    assert (await boss.get("/docs/fr/")).status_code == 404


def test_docs_root_prefers_the_first_existing_folder(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    (second / "en").mkdir(parents=True)
    monkeypatch.setattr(docs, "DOCS_DIRS", (first, second, tmp_path / "c"))
    assert docs.docs_root() == second
    (first / "it").mkdir(parents=True)
    assert docs.docs_root() == first
    monkeypatch.setattr(docs, "DOCS_DIRS", (tmp_path / "none",))
    assert docs.docs_root() is None


async def test_every_page_of_every_language_renders(boss):
    """No page may break the portal, and none of its links may point at a page or a heading that is not there."""
    for lang in docs.LANGS:
        for name in docs.page_files(ROOT / "docs", lang):
            r = await boss.get(f"/docs/{lang}/{name}")
            assert r.status_code == 200, (lang, name)
            for link, frag in re.findall(r'href="(/docs/[^"#]*)(?:#([^"]*))?"', r.text):
                target = await boss.get(link)
                assert target.status_code == 200, (lang, name, link)
                assert not frag or f'id="{frag}"' in target.text, (lang, name, link, frag)   # the anchor exists
            for frag in re.findall(r'href="#([^"]+)"', r.text):
                assert f'id="{frag}"' in r.text, (lang, name, frag)


def test_every_setting_is_described_in_both_configuration_pages():
    """A new field in spoolsteward/config.py must be documented, in English and in Italian."""
    prefix = Settings.model_config["env_prefix"]
    by_image = {"tls_certfile", "tls_keyfile", "database_url", "cups_url", "allow_insecure_auth", "host", "port"}
    pages = [(ROOT / "docs" / "en" / "configuration.md").read_text(), (ROOT / "docs" / "it" / "configurazione.md").read_text()]
    for name in Settings.model_fields:
        var = f"{prefix}{name.upper()}"
        for text in pages:
            assert re.search(rf"`{var}`", text), f"{var} is not described in a configuration page"
    for name in by_image:   # these are in the section "set by the image", not as something to set
        for text in pages:
            assert f"{prefix}{name.upper()}" in text


async def test_the_question_mark_is_one_button_for_everybody(boss):
    page = (await boss.get("/admin/jobs")).text
    assert page.count('class="helpbtn iconbtn ') == 1 and 'href="/docs"' in page
    assert 'class="helpbtn iconbtn on" href="/docs"' in (await boss.get("/docs/en/backup")).text
    with boss.sm() as s:   # a user who becomes an administrator keeps the same single button and gets every page
        make_user(boss.sm, "ann", password=GOOD)
    await portal_login(boss, "ann", GOOD)
    me = (await boss.get("/me")).text
    assert me.count('class="helpbtn iconbtn ') == 1 and 'href="/docs"' in me
    assert (await boss.get("/docs/en/backup")).status_code == 404
    with boss.sm() as s:
        s.scalars(select(User).where(User.username == "ann")).one().is_admin = True
        s.commit()
    assert (await boss.get("/docs/en/backup")).status_code == 200
    assert (await boss.get("/me")).text.count('class="helpbtn iconbtn ') == 1


async def test_create_user_form_has_the_show_password_button_and_the_profile_links_the_windows_guide(boss):
    form = (await boss.get("/admin/users")).text
    assert 'name="password"' in form and form.count("pwtoggle") >= 1
    make_user(boss.sm, "ann", password=GOOD)
    set_language(boss, "ann", "en")
    await portal_login(boss, "ann", GOOD)
    me = (await boss.get("/me")).text
    assert 'href="/docs/windows"' in me and "Not yet tried" in me   # macOS keeps its note, Windows lost it
    assert me.count("Not yet tried") == 1
