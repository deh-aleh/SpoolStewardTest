"""The checks on SPOOLSTEWARD_SECRET_KEY at start: told once, never again at a restart, told again only when the key changes."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import notices, secretcheck
from app.config import settings
from app.db.models import Base, GatewaySetting, JobLog, JobState, Notice
from tests.conftest import make_user


@pytest.fixture
def sm(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'gw.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "secret_key", "first-key-" + "x" * 40)
    monkeypatch.setattr(settings, "bootstrap_admin", True)
    monkeypatch.setattr(settings, "bootstrap_admin_username", "administrator")
    monkeypatch.setattr(notices, "_state", {})  # the rate limit lives in memory: every test starts clean
    return sessionmaker(engine, expire_on_commit=False)


def rows(sm, kind, own=None):
    with sm() as s:
        q = select(Notice).where(Notice.kind == kind)
        if own is not None:
            q = q.where(Notice.user_id.is_not(None) if own else Notice.user_id.is_(None))
        return list(s.scalars(q))


def restart(sm, monkeypatch):
    monkeypatch.setattr(notices, "_state", {})  # a restart forgets what was counted in memory
    secretcheck.run(sm)


def test_the_first_administrator_is_told_once_and_never_again(sm, monkeypatch):
    boss = make_user(sm, "administrator", password="correct horse battery", admin=True)
    secretcheck.run(sm)
    restart(sm, monkeypatch)
    got = rows(sm, "secret_key_save")
    assert len(got) == 1 and got[0].user_id == boss                                  # their own bell, not everybody's
    with sm() as s:                                                                 # closed, then deleted by the clean-up
        s.delete(s.get(Notice, got[0].id))
        s.commit()
    restart(sm, monkeypatch)
    restart(sm, monkeypatch)
    assert rows(sm, "secret_key_save") == []


def test_without_the_first_administrator_every_administrator_is_told(sm, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin", False)
    make_user(sm, "administrator", password="correct horse battery", admin=True)
    secretcheck.run(sm)
    restart(sm, monkeypatch)
    assert len(rows(sm, "secret_key_save", own=False)) == 1


def test_the_key_is_never_stored_only_a_fingerprint(sm):
    secretcheck.run(sm)
    with sm() as s:
        values = [r.value for r in s.scalars(select(GatewaySetting))]
    assert settings.secret_key not in "".join(values)
    assert secretcheck.fingerprint(settings.secret_key) in values


def test_a_changed_key_is_reported_once_with_the_names_it_locks(sm, monkeypatch):
    make_user(sm, "administrator", password="correct horse battery", admin=True)
    secretcheck.run(sm)
    with sm() as s:
        for i in range(3):
            s.add(JobLog(ts=datetime.now(UTC), user_name="ann", printer="p", title=f"file{i}.pdf", state=JobState.completed))
        s.commit()
    monkeypatch.setattr(settings, "secret_key", "second-key-" + "y" * 40)
    restart(sm, monkeypatch)
    restart(sm, monkeypatch)
    changed = rows(sm, "secret_key_changed")
    assert len(changed) == 1 and changed[0].detail_json == {"count": 3}
    assert len(rows(sm, "secret_key_save")) == 2                                    # the new key has to be saved too


def test_no_key_is_reported_and_closes_once_a_key_is_set(sm, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    secretcheck.run(sm)
    assert len(rows(sm, "secret_key_missing")) == 1 and rows(sm, "secret_key_save") == []
    monkeypatch.setattr(settings, "secret_key", "now-set-" + "z" * 40)
    restart(sm, monkeypatch)
    assert rows(sm, "secret_key_missing")[0].dismissed_by == "system"
    assert rows(sm, "secret_key_changed") == []                                     # setting a key for the first time is no change


def test_a_failure_never_stops_the_start(sm, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(secretcheck, "_run", boom)
    secretcheck.run(sm)


@pytest.mark.usefixtures("english")
async def test_the_first_administrator_reads_it_in_the_bell(portal, monkeypatch):
    from tests.conftest import portal_login
    monkeypatch.setattr(settings, "secret_key", "first-key-" + "x" * 40)
    monkeypatch.setattr(notices, "_state", {})
    make_user(portal.sm, "administrator", password="correct horse battery", admin=True)
    secretcheck.run(portal.sm)
    await portal_login(portal, "administrator", "correct horse battery")
    page = (await portal.get("/admin/notices")).text
    assert "Save the secret key in a safe place" in page and "SPOOLSTEWARD_SECRET_KEY" in page
