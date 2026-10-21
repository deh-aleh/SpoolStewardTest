import logging

from spoolsteward.logsetup import ZonedFormatter


def _line(tz):
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello", None, None)
    rec.created, rec.msecs = 1792000000.0, 0.0   # 2026-10-14 17:46:40 UTC
    return ZonedFormatter(tz).format(rec)


def test_log_times_follow_the_configured_zone_and_show_the_offset():
    assert _line("UTC").startswith("2026-10-14 17:46:40,000+0000 INFO x: hello")
    assert _line("Europe/London").startswith("2026-10-14 18:46:40,000+0100")


def test_a_wrong_zone_name_does_not_break_logging():
    assert _line("Not/AZone").startswith("2026-10-14 17:46:40,000+0000")
