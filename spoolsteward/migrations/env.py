from alembic import context
from sqlalchemy import create_engine

from spoolsteward.config import settings
from spoolsteward.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")  # passed in by spoolsteward.db.migrate
    if connection is None:
        engine = create_engine(_url())
        with engine.connect() as connection:
            _run(connection)
        return
    _run(connection)


def _run(connection) -> None:
    # batch mode: SQLite cannot ALTER most things in place
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
