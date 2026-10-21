from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 15}  # wait for the writer lock instead of failing
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite") and ":memory:" not in url and url != "sqlite://":

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers (admin UI) never block the gateway
            cur.close()

    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    from app.db.migrate import upgrade

    upgrade(engine)
