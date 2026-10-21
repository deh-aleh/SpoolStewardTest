"""First-run administrator.

The account `administrator` exists without a password. Its password is set with a one-time code that is
printed in the log (readable by whoever runs the server, not by the LAN): the first curious person to open the
portal can not claim the administrator account. `spoolsteward-admin set-admin-password` does the same from a shell.
"""

import logging
import secrets

from sqlalchemy import select

from app.config import settings
from app.db.models import User

log = logging.getLogger(__name__)


def ensure_orchestrator(sm) -> str | None:
    """Create the account if missing. Returns a fresh setup code while its password is still unset."""
    if not settings.bootstrap_admin:
        return None
    name = settings.bootstrap_admin_username
    with sm() as db:
        user = db.scalars(select(User).where(User.username == name)).first()
        if user is None:
            user = User(provider="local", username=name, is_admin=True, enabled=True)
            db.add(user)
            db.commit()
        elif user.provider != "local" or not user.enabled:
            log.error("account %r exists but is not an enabled local account: first-run setup unavailable", name)
            return None
        if user.password_hash:
            # nothing to claim: say so, because people look for the FIRST RUN line and find nothing
            log.info("administrator %r already has a password (first-run setup is done): sign in at /login. "
                     "To set a new one: docker compose exec -it gateway spoolsteward-admin set-admin-password", name)
            return None
    code = secrets.token_urlsafe(16)
    log.warning("FIRST RUN: administrator %r has no password yet. Open /setup and use the one-time code: %s",
                name, code)
    return code
