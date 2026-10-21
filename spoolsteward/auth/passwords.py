"""Local account passwords: argon2id, slow on purpose (these are guessable human secrets)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from spoolsteward.i18n import _

MIN_LENGTH = 12
MAX_LENGTH = 256  # bounds the work an attacker can make us do per attempt

_hasher = PasswordHasher()
# verified against when the account does not exist, so "no such user" costs as much as "wrong password"
_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        return False
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def validate_new_password(password: str, username: str = "") -> str | None:
    """Returns an error message, or None if the password is acceptable. Length beats complexity rules."""
    if len(password) < MIN_LENGTH:
        return _("the password must be at least %(n)s characters long", n=MIN_LENGTH)
    if len(password) > MAX_LENGTH:
        return _("the password must be at most %(n)s characters long", n=MAX_LENGTH)
    if username and password.strip().lower() == username.strip().lower():
        return _("the password must not be the username")
    if len(set(password)) < 5:
        return _("the password is too repetitive")
    return None
