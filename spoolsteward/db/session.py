from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from spoolsteward.config import settings


def make_engine(url: str, sync: str = "full"):
    """`sync`: "full" (SQLite waits for the disk after every save) or "normal" (it does not: faster on an SD card, and after a power
    cut the last few seconds of saves can be missing; the database is never damaged by it). Only used with WAL, i.e. a real file."""
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 15}  # wait for the writer lock instead of failing
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        on_disk = ":memory:" not in url and url != "sqlite://"

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            # SQLite ignores the foreign keys of the schema unless every connection asks for them: without this a row could point
            # to a user or group that does not exist. (The migrations switch it off while they rebuild tables: db/migrate.py.)
            cur.execute("PRAGMA foreign_keys=ON")
            if on_disk:
                cur.execute("PRAGMA journal_mode=WAL")  # readers (admin UI) never block the gateway
                cur.execute("PRAGMA synchronous=NORMAL" if sync == "normal" else "PRAGMA synchronous=FULL")  # (FULL = SQLite's own default, said out loud)
            cur.close()

    return eng


engine = make_engine(settings.database_url, settings.db_sync)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    from spoolsteward.db.migrate import upgrade

    upgrade(engine)
