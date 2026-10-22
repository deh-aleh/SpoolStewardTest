"""How the SQLite connections are set up: write-ahead log, and how carefully a save waits for the disk (SPOOLSTEWARD_DB_SYNC)."""

import pytest
from sqlalchemy import text

from spoolsteward.config import Settings
from spoolsteward.db.session import make_engine

FULL, NORMAL = 2, 1       # the values SQLite answers to PRAGMA synchronous


def pragma(url: str, name: str, **kw):
    eng = make_engine(url, **kw)
    try:
        with eng.connect() as c:
            return c.execute(text(f"PRAGMA {name}")).scalar()
    finally:
        eng.dispose()


def test_the_default_is_full_and_the_template_agrees():
    assert Settings(_env_file=None).db_sync == "full"


@pytest.mark.parametrize("sync, expected", [("full", FULL), ("normal", NORMAL)])
def test_every_connection_uses_the_chosen_synchronous_mode(tmp_path, sync, expected):
    url = f"sqlite:///{tmp_path}/a.db"
    assert pragma(url, "synchronous", sync=sync) == expected
    assert pragma(url, "journal_mode", sync=sync) == "wal"


def test_without_an_argument_the_engine_is_as_careful_as_before(tmp_path):
    assert pragma(f"sqlite:///{tmp_path}/b.db", "synchronous") == FULL


# ---------------------------------------------------------------- foreign keys

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from spoolsteward.db.migrate import check_foreign_keys, upgrade  # noqa: E402
from spoolsteward.db.models import Base, JobKind, JobLog, JobState  # noqa: E402


@pytest.mark.parametrize("url", ["sqlite://", "FILE"])
def test_a_row_can_not_point_to_a_user_that_does_not_exist(tmp_path, url):
    eng = make_engine(f"sqlite:///{tmp_path}/fk.db" if url == "FILE" else url)
    Base.metadata.create_all(eng)
    with sessionmaker(eng)() as s:
        s.add(JobLog(kind=JobKind.print, state=JobState.completed, user_id=999))
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            s.commit()
    eng.dispose()


def test_the_migrations_rebuild_tables_with_the_foreign_keys_enforced_and_leave_them_on(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path}/m.db")
    upgrade(eng)                                           # from nothing to head: batch migrations drop and recreate tables
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1      # back on for everything that follows
    assert check_foreign_keys(eng) == 0
    eng.dispose()


def test_an_engine_that_did_not_enforce_the_keys_is_left_as_it_was_by_the_migrations(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/plain.db")  # not made by make_engine: foreign keys off
    upgrade(eng)
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0


def test_a_database_with_orphan_rows_starts_and_says_so(tmp_path, caplog):
    """A database made before the keys were enforced (a test installation) may hold rows that point nowhere: warn, never refuse."""
    url = f"sqlite:///{tmp_path}/old.db"
    old = create_engine(url)                               # foreign keys off, as the old engine was
    upgrade(old)
    with old.begin() as c:
        c.exec_driver_sql("INSERT INTO job_log (kind, state, user_id, ts) VALUES ('print', 'completed', 4242, '2026-01-01')")
    old.dispose()
    eng = make_engine(url)
    with caplog.at_level("WARNING", logger="spoolsteward.db"):
        upgrade(eng)                                       # already at head: nothing to migrate, still checked
    assert "1 row(s) that point to something that does not exist (job_log -> user: 1)" in caplog.text
    assert check_foreign_keys(eng) == 1
    eng.dispose()
