import threading
from pathlib import Path

import pytest

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from spoolsteward.backup.core import known_revisions
from spoolsteward.auth.users import resolve_user
from spoolsteward.db.migrate import upgrade
from spoolsteward.db.models import Base
from spoolsteward.db.session import make_engine


def test_migrations_match_models(tmp_path):
    """If this fails you changed a model without writing a migration."""
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    upgrade(eng)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent_and_downgrade_clean(tmp_path):
    from alembic import command

    from spoolsteward.db.migrate import _config

    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    upgrade(eng)
    upgrade(eng)
    with eng.begin() as conn:
        cfg = _config(eng); cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert set(inspect(eng).get_table_names()) == {"alembic_version"}


def _config_for(eng, conn):
    from spoolsteward.db.migrate import _config

    cfg = _config(eng)
    cfg.attributes["connection"] = conn
    return cfg


def test_pre_alembic_db_is_stamped_at_0001_then_upgraded_keeping_its_data(tmp_path):
    from alembic import command

    eng = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    with eng.begin() as conn:                      # the schema every early build had...
        command.upgrade(_config_for(eng, conn), "0001")
        conn.execute(text("insert into `group`(name) values ('keep-me')"))
        conn.execute(text("insert into user(provider, username, is_admin, enabled) values ('radius','old-admin',1,1)"))
        for i in range(3):
            conn.execute(text("insert into job_log(ts, user_id, kind, state, title) values "
                              "('2026-09-01 10:00:00', 1, 'print', 'completed', :t)"), {"t": f"old-{i}"})
        conn.execute(text("drop table alembic_version"))   # ...without the version table
    upgrade(eng)
    with eng.begin() as conn:                              # ids of the existing rows survive and never restart
        assert [r[0] for r in conn.execute(text("select id from job_log order by id"))] == [1, 2, 3]
        conn.execute(text("delete from job_log"))
        conn.execute(text("insert into job_log(ts, kind, state) values ('2026-10-01 10:00:00', 'print', 'completed')"))
        assert conn.execute(text("select id from job_log")).scalar() == 4
    with eng.connect() as conn:
        assert conn.execute(text("select name, requestable from `group`")).one() == ("keep-me", 0)
        row = conn.execute(text("select username, is_admin, must_change_password from user")).one()
        assert tuple(row) == ("old-admin", 1, 0)
        assert conn.execute(text("select version_num from alembic_version")).scalar() == max(known_revisions())   # the newest revision
    names = {i["name"] for i in inspect(eng).get_unique_constraints("user")}
    assert names == {"uq_user_username"}           # the old (provider, username) constraint is gone


def test_upgrade_refuses_when_two_providers_share_a_username(tmp_path):
    from alembic import command

    eng = create_engine(f"sqlite:///{tmp_path}/dupes.db")
    with eng.begin() as conn:
        command.upgrade(_config_for(eng, conn), "0001")
        conn.execute(text("insert into user(provider, username, is_admin, enabled) values ('radius','mario',0,1)"))
        conn.execute(text("insert into user(provider, username, is_admin, enabled) values ('local','mario',0,1)"))
    with pytest.raises(Exception, match="mario"):
        upgrade(eng)


def test_concurrent_first_logins_yield_one_user(tmp_path):
    url = f"sqlite:///{tmp_path}/r.db"
    eng = make_engine(url)
    upgrade(eng)
    ids = []

    def login():
        e = make_engine(url)
        with sessionmaker(e, expire_on_commit=False)() as s:
            ids.append(resolve_user(s, "radius", "newbie", True, "default").id)
        e.dispose()

    threads = [threading.Thread(target=login) for _ in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(ids) == 10 and len(set(ids)) == 1


def test_audit_ids_are_never_reused(tmp_path):
    """After archiving empties job_log the next row must not get id 1 again."""
    from sqlalchemy.orm import sessionmaker

    from spoolsteward.db.models import JobKind, JobLog, JobState

    eng = create_engine(f"sqlite:///{tmp_path}/ids.db")
    upgrade(eng)
    with sessionmaker(eng)() as s:
        for _ in range(3):
            s.add(JobLog(kind=JobKind.print, state=JobState.completed))
        s.commit()
        s.query(JobLog).delete()
        s.commit()
        s.add(JobLog(kind=JobKind.print, state=JobState.completed))
        s.commit()
        assert s.query(JobLog).one().id == 4


# ------------------------------------------------------------------------------------------------ expand / contract

# A new version must not break the previous one when something goes wrong and the update is rolled back: the old code
# still runs against the NEW schema. So a migration only ADDS (tables, columns with a default or that may be empty).
# Removing or renaming something is done in two releases: the first stops using it, the second drops it. Migrations up to
# BASELINE are already released; later ones must not use the operations below. A migration that really has to may
# be listed in CONTRACTS with a reason - and the update notes must say that going back needs a backup.
BASELINE = "0013"
CONTRACTS: dict[str, str] = {}
FORBIDDEN = ("drop_column", "drop_table", "drop_constraint", "alter_column", "rename_table", "execute(")


def test_new_migrations_only_expand_the_schema():
    versions = Path(__file__).resolve().parent.parent / "spoolsteward" / "migrations" / "versions"
    bad = []
    for f in sorted(versions.glob("[0-9]*.py")):
        if f.name[:4] <= BASELINE or f.name[:4] in CONTRACTS:
            continue
        upgrade = f.read_text().split("def upgrade", 1)[1].split("def downgrade", 1)[0]
        bad += [f"{f.name}: {op}" for op in FORBIDDEN if op in upgrade]
    assert not bad, "a migration after the released ones removes or changes something (see the note above): " + ", ".join(bad)


def test_a_database_from_a_newer_version_is_left_alone(tmp_path):
    """Rollback: the old code starts on a database a newer version already migrated (revision it does not know)."""
    from sqlalchemy import text

    eng = create_engine(f"sqlite:///{tmp_path / 'n.db'}")
    upgrade(eng)
    with eng.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    upgrade(eng)                                                      # does not raise
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "9999"
