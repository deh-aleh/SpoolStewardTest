"""Run Alembic migrations from inside the app (no alembic.ini needed in production)."""

import logging
from collections import Counter
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

log = logging.getLogger("spoolsteward.db")
_SCRIPTS = Path(__file__).resolve().parent.parent / "migrations"


def _config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPTS))
    return cfg


def check_foreign_keys(engine: Engine) -> int:
    """How many rows point to a row that does not exist. Only a database made before the foreign keys were enforced can have
    them; they are reported in the log and left as they are (the program works, the rows are just orphans). Never raises."""
    if engine.dialect.name != "sqlite":
        return 0
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    except Exception:
        log.exception("could not check the foreign keys of the database")
        return 0
    if rows:
        where = ", ".join(f"{table} -> {parent}: {n}" for (table, parent), n in sorted(Counter((r[0], r[2]) for r in rows).items()))
        log.warning("the database has %d row(s) that point to something that does not exist (%s). They are left as they are and the "
                    "program works; they come from a time when the database did not check its links. A new database has none.",
                    len(rows), where)
    return len(rows)


def upgrade(engine: Engine) -> None:
    """Bring the schema to head. A DB created before Alembic existed (early create_all() builds) has exactly the
    0001 schema: it is stamped with that revision, then upgraded like any other, so its data is kept."""
    cfg = _config(engine)
    tables = set(inspect(engine).get_table_names())
    if "alembic_version" in tables:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_heads()
        known = {r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()}
        if any(rev not in known for rev in current):
            # The database was upgraded by a NEWER version and this older one was put back (a rollback). Migrations only add,
            # so the old code works on the newer schema: leave it as it is instead of failing to start.
            log.warning("the database is at revision %s, newer than this version knows: left as it is (rollback)", ", ".join(current))
            check_foreign_keys(engine)
            return
    sqlite = engine.dialect.name == "sqlite"
    with engine.connect() as conn:
        if sqlite:
            # The migrations rebuild tables (SQLite cannot ALTER them), and DROP TABLE of a table other tables point to fails while the
            # foreign keys are enforced. The setting works only outside a transaction: it is changed before one starts, and put back
            # as it was when the migrations are done.
            was = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
        try:
            with conn.begin():
                cfg.attributes["connection"] = conn
                if "alembic_version" not in tables and "job_log" in tables:
                    command.stamp(cfg, "0001")
                command.upgrade(cfg, "head")
        finally:
            if sqlite:
                conn.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if was else 'OFF'}")
                conn.commit()
    check_foreign_keys(engine)
