import asyncio
import html
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from spoolsteward import updates
from spoolsteward.config import Settings, settings
from tests.conftest import GOOD, csrf_of_page, make_user, portal_login


def test_parse_accepts_only_plain_releases():
    assert updates.parse("v1.2.3") == (1, 2, 3)
    assert updates.parse("1.10.0") == (1, 10, 0)
    for bad in ("1.2", "1.2.3-rc1", "latest", "", "v1.2.3; rm -rf /", "1.2.3-beta.1"):
        assert updates.parse(bad) is None


def test_newer_compares_numbers_not_text_and_puts_a_beta_before_its_final_release():
    assert updates.newer("1.10.0", "1.9.0")
    assert not updates.newer("1.2.0", "1.2.0")
    assert not updates.newer("1.1.0", "1.2.0")
    assert not updates.newer("2.0.0", "unknown")                      # unknown installed version: say nothing
    assert updates.newer("1.2.3", "1.2.3b1") and updates.newer("1.2.3", "1.2.3-beta.4")
    assert updates.newer("1.2.3-beta.2", "1.2.3-beta.1") and updates.newer("1.2.4-beta.1", "1.2.3")
    assert not updates.newer("1.2.3-beta.9", "1.2.3")
    assert not updates.newer("1.2.3", "1.2.3bx")                      # junk installed version: say nothing


def rel(tag, *, pre=None, draft=False, body=""):
    return {"tag_name": tag, "draft": draft, "prerelease": ("-beta." in tag) if pre is None else pre, "body": body,
            "html_url": "javascript:alert(1)", "author": {"login": "x"}}


def test_only_published_releases_with_a_version_tag_are_kept():
    found = updates.releases_from([
        rel("v1.0.0"), rel("v1.1.0-beta.1"), rel("v2.0.0", draft=True), rel("nightly"), rel("v1.2.0-rc.1"), rel("1.3.0b1"),
        rel("v1.0.1", pre=True), {"tag_name": "v1.0.2", "prerelease": False}, "junk", None, {"tag_name": 7, "draft": False},
        {**rel("v1.0.3"), "prerelease": None},
    ])
    assert [(r["version"], r["beta"]) for r in found] == [("1.0.0", False), ("1.1.0-beta.1", True),
                                                          ("1.0.1", True), ("1.0.3", True)]   # flagged pre-release: a beta
    assert updates.releases_from({"message": "Not Found"}) == []


@pytest.mark.parametrize("body", [
    "## Breaking changes\n- the database", "# BREAKING CHANGES", "intro\r\n### Breaking Changes ###\r\n- x",
    "###### breaking changes:", "   ## Breaking changes", "Breaking changes\n================", "x\n\nBreaking changes\r\n---\r\n",
    "\t\n##\tBreaking changes  \n",
])
def test_a_heading_breaking_changes_marks_a_release(body):
    assert updates.breaking_notes(body)


@pytest.mark.parametrize("body", [
    "", None, 42, ["## Breaking changes"], "No breaking changes", "Breaking changes in this release: none",
    "## No breaking changes", "####### Breaking changes", "    ## Breaking changes", "##Breaking changes",
    "Breaking changes\nmore text", "* ## Breaking changes", "<h2>Breaking changes</h2>",
    "x" * updates.MAX_NOTES + "\n## Breaking changes",
])
def test_other_text_is_not_a_heading(body):
    assert not updates.breaking_notes(body)


def releases(*items):
    return updates.releases_from(list(items))


LIST = releases(rel("v1.3.0-beta.1"), rel("v1.2.1"), rel("v1.2.0"), rel("v1.2.0-beta.2"), rel("v1.1.0"))


def test_betas_are_offered_only_with_the_setting():
    assert updates.pick(LIST, "1.1.0", beta=True)["version"] == "1.3.0-beta.1"
    assert updates.pick(LIST, "1.1.0", beta=True)["beta"] is True
    off = updates.pick(LIST, "1.1.0", beta=False)
    assert off["version"] == "1.2.1" and off["beta"] is False
    assert updates.pick(LIST, "1.2.0-beta.1", beta=False)["version"] == "1.2.1"     # from a beta: its final release
    assert updates.pick(LIST, "1.2.0b1", beta=False)["version"] == "1.2.1"          # (the package metadata spelling)
    assert updates.pick(LIST, "1.2.1", beta=False) is None and updates.pick(LIST, "1.3.0-beta.1", beta=True) is None
    assert updates.pick(LIST, "unknown", beta=True) is None


def test_the_newest_release_wins_whatever_the_order_of_the_list():
    mixed = releases(rel("v1.2.0-beta.1"), rel("v1.10.0"), rel("v1.9.0"), rel("v1.10.0-beta.3"), rel("v1.2.0"))
    assert updates.pick(mixed, "1.0.0", beta=True)["version"] == "1.10.0"
    assert updates.pick(releases(rel("v1.2.4-beta.1"), rel("v1.2.3")), "1.0.0", beta=True)["version"] == "1.2.4-beta.1"


def test_the_release_page_is_built_from_the_repository_never_taken_from_the_answer(monkeypatch):
    monkeypatch.setattr(settings, "update_repository", "deh-aleh/SpoolStewardTest")
    info = updates.pick(releases(rel("v1.2.0")), "1.0.0", beta=True)
    assert info["url"] == "https://github.com/deh-aleh/SpoolStewardTest/releases/tag/v1.2.0"


def test_breaking_changes_in_the_notes_of_any_release_on_the_way_count():
    notes = "## Breaking changes\nthe config folder moved"
    chain = releases(rel("v1.4.0"), rel("v1.3.0", body=notes), rel("v1.2.0"), rel("v1.1.0", body=notes))
    assert updates.pick(chain, "1.3.0", beta=True)["breaking"] is False           # the installed one is behind us
    assert updates.pick(chain, "1.2.0", beta=True)["breaking"] is True            # 1.3.0 is skipped, but counts
    assert updates.pick(chain, "1.0.0", beta=True)["breaking"] is True
    # a beta that is not offered still counts: its notes are the warning
    chain = releases(rel("v1.3.0"), rel("v1.3.0-beta.1", body=notes))
    assert updates.pick(chain, "1.2.0", beta=False) == {**updates.pick(chain, "1.2.0", beta=False), "version": "1.3.0", "breaking": True}
    # a release newer than the one offered does not
    chain = releases(rel("v1.4.0-beta.1", body=notes), rel("v1.3.0"))
    assert updates.pick(chain, "1.2.0", beta=False)["breaking"] is False


def test_a_new_major_or_a_new_minor_on_zero_is_breaking():
    def info(target, installed):
        return updates.pick(releases(rel("v" + target)), installed, beta=True)

    assert info("2.0.0", "1.9.3")["breaking"] and info("2.0.0", "1.9.3")["major"]
    assert info("0.2.0", "0.1.4")["breaking"] and not info("0.2.0", "0.1.4")["major"]
    assert info("0.1.0-beta.1", "0.0.0b1")["breaking"]
    for target, installed in (("1.3.0", "1.2.0"), ("0.1.5", "0.1.4"), ("0.0.0-beta.2", "0.0.0-beta.1"), ("0.0.0", "0.0.0b1"),
                              ("0.0.1", "0.0.0")):
        assert not info(target, installed)["breaking"], (target, installed)
        assert not info(target, installed)["major"]


def api(pages: dict, seen: list, *, status=200):
    def handler(request: httpx.Request):
        seen.append(request.url)
        return httpx.Response(status, json=pages.get(int(request.url.params["page"]), []))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_every_page_of_releases_is_read_and_the_newest_is_found_on_any_of_them(monkeypatch):
    monkeypatch.setattr(settings, "update_repository", "someone/fork.of_it-2")
    page1 = [rel(f"v1.0.{i}") for i in range(100)]
    seen = []
    found = asyncio.run(updates.fetch_releases(api({1: page1, 2: [rel("v1.5.0"), rel("v0.9.0")]}, seen)))
    assert len(found) == 102 and len(seen) == 2
    assert str(seen[0]).startswith("https://api.github.com/repos/someone/fork.of_it-2/releases?")
    assert seen[0].params["per_page"] == "100" and seen[1].params["page"] == "2"
    assert updates.pick(found, "1.0.3", beta=True)["version"] == "1.5.0"
    seen = []                                   # never more than MAX_PAGES requests, however long the list
    asyncio.run(updates.fetch_releases(api({n: page1 for n in range(1, 50)}, seen)))
    assert len(seen) == updates.MAX_PAGES


def test_a_broken_or_huge_answer_is_an_error_not_an_empty_list(monkeypatch):
    with pytest.raises(ValueError):
        asyncio.run(updates.fetch_releases(api({1: {"message": "rate limited"}}, [])))     # not a list
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(updates.fetch_releases(api({}, [], status=403)))
    monkeypatch.setattr(updates, "MAX_PAGE_BYTES", 1000)
    with pytest.raises(ValueError, match="larger than"):
        asyncio.run(updates.fetch_releases(api({1: [rel("v1.0.0", body="x" * 2000)]}, [])))


def test_the_check_keeps_the_offer_in_memory_and_follows_the_setting(monkeypatch):
    async def fake(client=None):
        return releases(rel("v1.1.0-beta.1"), rel("v1.0.1"))
    monkeypatch.setattr(updates, "fetch_releases", fake)
    monkeypatch.setattr(updates, "app_version", lambda: "1.0.0")
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(settings, "update_beta", True)
    asyncio.run(updates.check_once(app))
    assert app.state.update_info["version"] == "1.1.0-beta.1" and app.state.update_info["beta"]
    monkeypatch.setattr(settings, "update_beta", False)
    asyncio.run(updates.check_once(app))
    assert app.state.update_info["version"] == "1.0.1"


def test_a_failed_check_is_only_logged(monkeypatch, caplog):
    async def broken(app):
        raise httpx.ConnectError("no network")

    async def stop(seconds):
        if seconds == updates.CHECK_EVERY:
            raise asyncio.CancelledError
    monkeypatch.setattr(updates, "check_once", broken)
    monkeypatch.setattr(updates.asyncio, "sleep", stop)
    monkeypatch.setattr(settings, "update_check", True)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(updates.run_checker(app))
    assert app.state.update_info is None and "update check failed" in caplog.text


def test_settings_of_the_update():
    assert Settings.model_fields["update_beta"].default is True
    assert Settings.model_fields["update_repository"].default == "deh-aleh/SpoolSteward"
    for good in ("deh-aleh/SpoolStewardTest", "a/b", "A1-b2/x.y_z-1", "o/.github", "a" * 39 + "/" + "r" * 100):
        assert Settings(update_repository=good).update_repository == good
    for bad in ("deh-aleh", "deh-aleh/", "/x", "a/b/c", "-a/b", "a-/b", "a_b/c", "a/.", "a/..", "a/b c", "a/b\n",
                "https://github.com/a/b", "a" * 40 + "/b", "a/" + "r" * 101, "a/b;id", "a/ü"):
        with pytest.raises(ValueError):
            Settings(update_repository=bad)


def test_an_installed_beta_can_update_from_the_portal_within_its_major(monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "app_version", lambda: "1.2.3b1")
    monkeypatch.setattr(updates, "_dir", lambda: tmp_path)
    updates.write_request("1.2.3", "root", "b")
    updates.write_request("1.3.0-beta.2", "root", "b")                 # a beta, as the tag and .env write it
    assert json.loads((tmp_path / "request.json").read_text())["version"] == "1.3.0-beta.2"
    for bad in ("2.0.0", "2.0.0-beta.1", "1.3.0b2", "v1.3.0", "1.3.0-beta", "1.3.0-beta.1; id"):
        with pytest.raises(ValueError):
            updates.write_request(bad, "root", "b")
    (tmp_path / "status.json").write_text('{"state":"ok","version":"1.3.0-beta.2","backup":"x.tar.gz"}')
    assert updates.read_status()["version"] == "1.3.0-beta.2"
    (tmp_path / "status.json").write_text('{"state":"ok","version":"1.3.0b2"}')
    assert updates.read_status() == {}


# ---- the portal -------------------------------------------------------------------------------------------------------

@pytest.fixture
async def page(portal, tmp_path, monkeypatch):
    """The Backup page of an administrator, with a working backup and a running updater; `offer(info)` sets the offer."""
    from spoolsteward.backup import service

    (tmp_path / "backups").mkdir()
    monkeypatch.setattr(settings, "backup_root", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "update_dir", str(tmp_path / "update"))
    monkeypatch.setattr(updates, "app_version", lambda: "1.0.0")
    monkeypatch.setattr(updates, "updater_alive", lambda: True)
    monkeypatch.setattr(updates, "eligible_backup", lambda db, now: SimpleNamespace(id=1, name="nightly"))
    monkeypatch.setattr(service, "run_target", lambda sm, tid, prune: (True, "spoolsteward-x.tar.gz written"))
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    await portal_login(portal, "ada", GOOD)
    portal.csrf = await csrf_of_page(portal, "/admin/backups")

    def offer(version, *, beta=False, breaking=False, major=False):
        portal.app.state.update_info = {"version": version, "beta": beta, "breaking": breaking, "major": major,
                                        "url": f"https://github.com/deh-aleh/SpoolSteward/releases/tag/v{version}"}
    portal.offer = offer
    portal.wanted = tmp_path / "update" / "request.json"
    yield portal
    portal.app.state.update_info = None


def update_form(text: str) -> dict:
    """The attributes of the update form, unescaped as the browser reads them."""
    tag = re.search(r'<form method="post" action="/admin/update"([^>]*)>', text).group(1)
    return {k: html.unescape(v) for k, v in re.findall(r'(data-confirm[\w-]*)(?:="([^"]*)")?', tag)}


async def test_a_final_release_asks_the_usual_question_and_needs_no_tick(page):
    page.offer("1.1.0")
    text = (await page.get("/admin/backups")).text
    form = update_form(text)
    assert form == {"data-confirm": "Update now? A backup is made first. The gateway restarts and printing stops for a minute or two."}
    assert "A new version is available: 1.1.0 (installed: " in text
    assert '<input type="hidden" name="version" value="1.1.0">' in text
    await page.post("/admin/update", data=dict(csrf_token=page.csrf, version="1.1.0-beta.1"))   # the offer changed meanwhile
    assert not page.wanted.exists()
    await page.post("/admin/update", data=dict(csrf_token=page.csrf, version="1.1.0"))
    assert json.loads(page.wanted.read_text())["version"] == "1.1.0"


async def test_a_beta_needs_the_tick_of_its_confirmation(page):
    page.offer("1.1.0-beta.1", beta=True)
    text = (await page.get("/admin/backups")).text
    form = update_form(text)
    assert form["data-confirm"] == ("1.1.0-beta.1 is a test release (pre-release): it can contain bugs.\n\n"
                                    "Update now? A backup is made first. The gateway restarts and printing stops for a minute or two.")
    assert form["data-confirm-title"] == "Install a test release?"
    assert form["data-confirm-check"] == "I understand that this is a test release"
    assert form["data-confirm-check-name"] == "accept" and "data-confirm-check-required" in form
    assert "data-confirm-danger" not in form
    assert "A new test release (pre-release) is available: 1.1.0-beta.1 (installed: " in text
    assert "new test release 1.1.0-beta.1 available (pre-release)" in text

    r = await page.post("/admin/update", data=dict(csrf_token=page.csrf))     # without the tick: nothing happens
    assert r.status_code == 303 and not page.wanted.exists()
    assert "tick the box" in (await page.get("/admin/backups")).text
    await page.post("/admin/update", data=dict(csrf_token=page.csrf, accept="1"))
    assert json.loads(page.wanted.read_text())["version"] == "1.1.0-beta.1"


async def test_breaking_changes_get_the_strong_warning(page):
    page.offer("1.2.0", breaking=True)
    text = (await page.get("/admin/backups")).text
    form = update_form(text)
    assert form["data-confirm"] == (
        "The update to 1.2.0 contains breaking changes. The installation could fail and leave SpoolSteward not working.\n\n"
        "Before you go on, copy a recent backup off the server and keep a copy of the file .env and of the config folder.\n\n"
        "Read the release notes first: https://github.com/deh-aleh/SpoolSteward/releases/tag/v1.2.0\n\n"
        "Update now? A backup is made first. The gateway restarts and printing stops for a minute or two.")
    assert form["data-confirm-title"] == "Update with breaking changes" and "data-confirm-danger" in form
    assert form["data-confirm-check"] == "I have a backup, a copy of .env and of the config folder, and I have read the release notes"
    assert "data-confirm-check-required" in form
    assert "It contains breaking changes: read the release notes before updating." in text
    assert '<a href="https://github.com/deh-aleh/SpoolSteward/releases/tag/v1.2.0" rel="noopener noreferrer" target="_blank">Read the release notes</a>' in text

    await page.post("/admin/update", data=dict(csrf_token=page.csrf))
    assert not page.wanted.exists()
    await page.post("/admin/update", data=dict(csrf_token=page.csrf, accept="1"))
    assert json.loads(page.wanted.read_text())["version"] == "1.2.0"


async def test_a_beta_with_breaking_changes_says_both(page):
    page.offer("1.2.0-beta.1", beta=True, breaking=True)
    form = update_form((await page.get("/admin/backups")).text)
    assert form["data-confirm"].startswith("1.2.0-beta.1 is a test release (pre-release): it can contain bugs.\n\nThe update to 1.2.0-beta.1 contains breaking changes.")
    assert form["data-confirm-title"] == "Update with breaking changes" and "data-confirm-check-required" in form


async def test_another_major_version_is_not_installed_from_the_portal(page):
    page.offer("2.0.0", breaking=True, major=True)
    text = (await page.get("/admin/backups")).text
    assert "<button disabled>" in text and 'action="/admin/update"' not in text
    assert "2.0.0 is a new major version: it is not installed from the portal." in text
    await page.post("/admin/update", data=dict(csrf_token=page.csrf, accept="1"))
    assert not page.wanted.exists()
