import logging
import sys

import uvicorn

from app.config import settings
from app.logsetup import setup_logging


def main() -> int:
    setup_logging(settings.log_level, settings.timezone)
    for noisy in ("httpx", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one INFO line per CUPS call drowns the real events
    if not (settings.tls_certfile and settings.tls_keyfile) and not settings.allow_insecure_auth:
        print("refusing to start without TLS: set SPOOLSTEWARD_TLS_CERTFILE and SPOOLSTEWARD_TLS_KEYFILE "
              "(dev: scripts/gen_dev_cert.sh)", file=sys.stderr)
        return 2
    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port,
        ssl_certfile=settings.tls_certfile, ssl_keyfile=settings.tls_keyfile,
        log_config=None,  # keep our logging setup (journald-friendly, no duplicate handlers)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
