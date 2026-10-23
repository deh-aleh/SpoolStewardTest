"""File names in the audit: encrypted in the database, can be switched off, can be forgotten after some days."""

import gzip
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from spoolsteward import crypto_fields, titles
from spoolsteward.audit import archive
from spoolsteward.config import settings
from spoolsteward.db.models import Base, Group, JobKind, JobLog, JobState, PolicyRule, SubjectType, User
from spoolsteward.policy import audit, control
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
TZ = ZoneInfo("Europe/Rome")


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "a-test-secret-key")
    monkeypatch.setattr(settings, "encrypt_file_names", True)


@pytest.fixture
def sm():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = sessionmaker(engine, expire_on_commit=False)
    with sm() as s:
        g = Group(name="g")
        s.add(g)
        s.flush()
        s.add(User(username="u", group_id=g.id))
        s.add(PolicyRule(subject_type=SubjectType.group, subject_id=g.id, limit=50))
        s.commit()
    return sm


def raw(sm, column="title"):
    with sm() as s:
        return [r[0] for r in s.execute(text(f"SELECT {column} FROM job_log ORDER BY id"))]


def user(sm):
    with sm() as s:
        return s.query(User).first()


def test_seal_roundtrip_and_a_wrong_key(key, monkeypatch):
    sealed = crypto_fields.seal("contract.pdf")
    assert sealed.startswith("enc1:") and "contract" not in sealed
    assert crypto_fields.unseal(sealed) == "contract.pdf"
    assert crypto_fields.seal("contract.pdf") != sealed          # a fresh nonce every time
    monkeypatch.setattr(settings, "secret_key", "another-key")
    assert crypto_fields.unseal(sealed) == crypto_fields.LOCKED   # the key changed: unreadable, not a crash


def test_without_a_key_nothing_is_encrypted(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    assert crypto_fields.seal("a.pdf") == "a.pdf" and crypto_fields.unseal("a.pdf") == "a.pdf"


def test_the_switch_is_off_by_default():
    assert settings.encrypt_file_names is False


def test_switch_off_stores_names_in_clear_even_with_a_key(sm, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "a-test-secret-key")
    monkeypatch.setattr(settings, "encrypt_file_names", False)
    assert crypto_fields.seal("a.pdf") == "a.pdf" and not crypto_fields.enabled()
    u = user(sm)
    with sm() as s:
        audit.reserve(s, u, "p", "plain.pdf", {"document_name": "plain.pdf"}, TZ)
    assert raw(sm) == ["plain.pdf"] and titles.encrypt_existing(sm) == 0


def test_switch_off_still_reads_names_sealed_before(sm, key, monkeypatch):
    sealed = crypto_fields.seal("kept.pdf")
    monkeypatch.setattr(settings, "encrypt_file_names", False)
    assert crypto_fields.unseal(sealed) == "kept.pdf"
    u = user(sm)
    with sm() as s:
        s.add(JobLog(user_id=u.id, kind=JobKind.print, printer="p", title=sealed, pages=1, state=JobState.completed))
        s.commit()
    with sm() as s:
        assert s.query(JobLog).one().title == "kept.pdf"


def test_switch_on_without_a_key_encrypts_nothing(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    monkeypatch.setattr(settings, "encrypt_file_names", True)
    assert crypto_fields.seal("a.pdf") == "a.pdf" and not crypto_fields.enabled()


def test_the_database_holds_the_name_encrypted_and_the_program_reads_it_plain(sm, key):
    u = user(sm)
    with sm() as s:
        _, i = audit.reserve(s, u, "p", "secret.pdf", {"via": "web"}, TZ)
        audit.finalize(s, i, ok=True, cups_job_id=7)
        audit.attach_document(s, u.id, 7, "real-secret.pdf", "application/pdf")
    assert raw(sm)[0].startswith("enc1:")
    opts = json.loads(raw(sm, "options_json")[0])
    assert opts["document_name"].startswith("enc1:") and opts["via"] == "web"   # only the name is hidden
    with sm() as s:
        row = s.query(JobLog).one()
        assert row.title == "secret.pdf" and row.options_json["document_name"] == "real-secret.pdf"


def test_old_plain_names_are_sealed_once_a_key_exists(sm, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    u = user(sm)
    with sm() as s:
        for n in range(3):
            audit.reserve(s, u, "p", f"old-{n}.pdf", {"document_name": f"old-{n}.pdf"}, TZ)
    assert raw(sm)[0] == "old-0.pdf"
    monkeypatch.setattr(settings, "secret_key", "a-test-secret-key")
    monkeypatch.setattr(settings, "encrypt_file_names", True)
    assert titles.encrypt_existing(sm) == 3
    assert all(t.startswith("enc1:") for t in raw(sm))
    assert titles.encrypt_existing(sm) == 0                       # nothing left: safe to repeat
    with sm() as s:
        assert [r.title for r in s.query(JobLog).order_by(JobLog.id)] == ["old-0.pdf", "old-1.pdf", "old-2.pdf"]


def test_names_switched_off_keep_the_counts_but_not_the_name(sm, key):
    u = user(sm)
    with sm() as s:
        assert control.titles_enabled(s)
        control.set_titles(s, False)
        s.commit()
        _, i = audit.reserve(s, u, "p", "confidential.pdf", {"document_name": "confidential.pdf", "via": "web"}, TZ, pages=4)
        audit.finalize(s, i, ok=True, cups_job_id=9)
        audit.attach_document(s, u.id, 9, "confidential.pdf", "application/pdf")
        row = s.get(JobLog, i)
        assert row.title is None and row.pages == 4 and row.state is JobState.accepted
        assert "document_name" not in row.options_json and row.options_json["document_format"] == "application/pdf"
        control.set_titles(s, True)
        s.commit()
        _, j = audit.reserve(s, u, "p", "visible.pdf", {}, TZ)
        assert s.get(JobLog, j).title == "visible.pdf"           # the old ones stay as they were, new ones saved again


def test_retention_erases_only_the_old_names(sm, key):
    u = user(sm)
    now = datetime.now(UTC)
    with sm() as s:
        for days, name in ((200, "very-old.pdf"), (100, "old.pdf"), (10, "recent.pdf")):
            s.add(JobLog(ts=now - timedelta(days=days), user_id=u.id, kind=JobKind.print, printer="p", title=name, pages=2,
                         state=JobState.completed, options_json={"document_name": name, "via": "web"}))
        s.commit()
    assert titles.purge(sm, 90) == 2
    with sm() as s:
        rows = s.query(JobLog).order_by(JobLog.id).all()
        assert [r.title for r in rows] == [None, None, "recent.pdf"]
        assert rows[0].pages == 2 and rows[0].options_json == {"via": "web"}   # the row stays: quotas need it
        assert rows[2].options_json["document_name"] == "recent.pdf"


def test_the_archive_keeps_names_encrypted_and_the_portal_reads_them(sm, key, tmp_path, monkeypatch):
    u = user(sm)
    old = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    with sm() as s:
        s.add(JobLog(ts=old, user_id=u.id, kind=JobKind.print, printer="p", title="balance-sheet.pdf", pages=1,
                     state=JobState.completed, options_json={"document_name": "balance-sheet.pdf"}))
        s.commit()
    res = archive.archive_table(sm, "job_log", datetime(2026, 10, 7, tzinfo=UTC), TZ, tmp_path, 0, force=True)
    with gzip.open(res.path, "rt") as f:
        line = json.loads(f.readline())
    assert line["title"].startswith("enc1:") and line["options"]["document_name"].startswith("enc1:")
    assert next(archive.iter_archive(res.path))["title"] == "balance-sheet.pdf"
    rows, total = archive.read_page(res.path, 1, 10, "balance")   # searching works on the names people read
    assert total == 1 and rows[0]["title"] == "balance-sheet.pdf"


def test_archive_drops_names_past_the_retention(sm, key, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "job_title_retention_enabled", True)
    monkeypatch.setattr(settings, "job_title_retention_days", 30)
    u = user(sm)
    with sm() as s:
        s.add(JobLog(ts=datetime(2026, 4, 10, 9, 0, tzinfo=UTC), user_id=u.id, kind=JobKind.print, printer="p",
                     title="forget-me.pdf", pages=1, state=JobState.completed, options_json={"document_name": "forget-me.pdf"}))
        s.commit()
    res = archive.archive_table(sm, "job_log", datetime.now(UTC), TZ, tmp_path, 0, force=True)
    row = next(archive.iter_archive(res.path))
    assert row["title"] is None and row["options"] is None


async def test_portal_button_search_and_csv(portal, key):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    with portal.sm() as s:
        for name in ("invoice-smith.pdf", "menu.pdf"):
            s.add(JobLog(user_id=None, user_name="ada", kind=JobKind.print, printer="p", title=name, pages=1,
                         state=JobState.completed))
        s.commit()
    assert all(t.startswith("enc1:") for t in raw(portal.sm))
    await portal_login(portal, "ada", GOOD)
    page = (await portal.get("/admin/jobs?q=smith")).text
    assert "invoice-smith.pdf" in page and "menu.pdf" not in page
    assert "tdot off" not in page                                   # green: names are saved
    assert "invoice-smith.pdf" in (await portal.get("/admin/jobs.csv?q=smith")).text

    csrf = await csrf_of_page(portal, "/admin/jobs")
    r = await portal.post("/admin/jobs/titles", data=dict(csrf_token=csrf, on="0"))
    assert r.status_code == 303
    assert "tdot off" in (await portal.get("/admin/jobs")).text    # red
    with portal.sm() as s:
        assert not control.titles_enabled(s)
