"""Log setup shared by the gateway and the mDNS announcer: the times follow SPOOLSTEWARD_TIMEZONE, with the UTC offset."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class ZonedFormatter(logging.Formatter):
    def __init__(self, tz: str, fmt: str = FORMAT):
        super().__init__(fmt)
        try:
            self.tz = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = ZoneInfo("UTC")  # a wrong name is reported where settings are used; logging must not fail

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        t = datetime.fromtimestamp(record.created, self.tz)
        return f"{t:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}{t:%z}"


def setup_logging(level: str, tz: str) -> None:
    logging.basicConfig(level=level.upper())
    for handler in logging.getLogger().handlers:
        handler.setFormatter(ZonedFormatter(tz))
