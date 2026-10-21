"""Run Alembic migrations from inside the app (no alembic.ini needed in production)."""

import logging
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
            return
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if "alembic_version" not in tables and "job_log" in tables:
            command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")
