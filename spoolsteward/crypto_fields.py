"""Encryption of the file names kept in the audit (JobLog.title and options["document_name"]).

The key comes from SPOOLSTEWARD_SECRET_KEY (HKDF-SHA256, a fixed purpose label), the cipher is AES-256-GCM with a random
nonce, and a stored value looks like  enc1:<base64>.  The encryption is transparent: the column types below encrypt on
the way into the database and decrypt on the way out, so the rest of the program only sees the plain text.

Names are encrypted only when SPOOLSTEWARD_ENCRYPT_FILE_NAMES is true AND a secret key exists (a random key per start
would make the names unreadable after a restart). Reading does not depend on the switch: a sealed value is opened whenever
the key is the right one, so turning the switch off never hides the names already sealed. A value written in clear is
read as it is, and `titles.encrypt_existing` seals it at the next start once the switch is on. If the key is changed, the names sealed with the old one can not be read any more: they show as LOCKED.
"""

import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import JSON, String, TypeDecorator

from spoolsteward.config import settings

log = logging.getLogger("spoolsteward.crypto")
PREFIX = "enc1:"
LOCKED = "[encrypted]"
_PURPOSE = b"spoolsteward-filename-v1"
_cache: dict[str, AESGCM] = {}


def _cipher() -> AESGCM | None:
    secret = settings.secret_key
    if not secret:
        return None
    if secret not in _cache:
        _cache.clear()
        _cache[secret] = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_PURPOSE).derive(secret.encode()))
    return _cache[secret]


def enabled() -> bool:
    """New names are sealed (switch on and a key set)."""
    return settings.encrypt_file_names and _cipher() is not None


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(text: str | None) -> str | None:
    """Plain text -> enc1:... (unchanged when encryption is off, there is no key, or no text)."""
    cipher = _cipher() if settings.encrypt_file_names else None
    if text is None or cipher is None:
        return text
    nonce = os.urandom(12)
    return PREFIX + base64.urlsafe_b64encode(nonce + cipher.encrypt(nonce, text.encode(), _PURPOSE)).decode()


def unseal(value):
    """enc1:... -> plain text. Plain values pass through; a value that can not be opened (changed key) becomes LOCKED."""
    if not is_sealed(value):
        return value
    cipher = _cipher()
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX):])
        return cipher.decrypt(raw[:12], raw[12:], _PURPOSE).decode() if cipher else LOCKED
    except (InvalidTag, ValueError):
        return LOCKED


class SealedText(TypeDecorator):
    """A text column whose content is encrypted in the database."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value)

    def process_result_value(self, value, dialect):
        return unseal(value)


class SealedOptions(TypeDecorator):
    """The JSON options of a job: only "document_name" (a file name) is encrypted, the rest stays readable."""
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, dict) and isinstance(value.get("document_name"), str):
            value = {**value, "document_name": seal(value["document_name"])}
        return value

    def process_result_value(self, value, dialect):
        if isinstance(value, dict) and is_sealed(value.get("document_name")):
            value = {**value, "document_name": unseal(value["document_name"])}
        return value
