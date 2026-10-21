"""Time helpers shared by the whole application (the database keeps UTC; people see the configured time zone)."""

from datetime import UTC, datetime
from typing import overload
from zoneinfo import ZoneInfo

from spoolsteward.config import settings


def now_utc() -> datetime:
    return datetime.now(UTC)


@overload
def aware(dt: datetime) -> datetime: ...
@overload
def aware(dt: None) -> None: ...
def aware(dt: datetime | None) -> datetime | None:
    """SQLite gives datetimes back without a zone; everything we store is UTC."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def local_tz() -> ZoneInfo:
    """The time zone people see (SPOOLSTEWARD_TIMEZONE)."""
    return ZoneInfo(settings.timezone)
