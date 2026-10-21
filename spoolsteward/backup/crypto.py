"""Backup encryption with a recovery key.

A backup must be written by a job that runs at 03:00 with nobody typing a password, yet only a person who knows a
passphrase may read it. So the gateway never keeps the passphrase:

  * the *recipient* is a key pair made once: the public half is stored in the clear, the private half is stored
    **wrapped** (scrypt of the passphrase + AES-GCM). The string `cpgbk1.<public>.<wrapped private>` is all the
    gateway keeps;
  * each backup file is encrypted for the public half (ephemeral X25519 + HKDF + AES-256-GCM, in 1 MiB chunks, the last
    one flagged so a truncated file is detected). The wrapped private half is copied in the file header, so a backup
    is self-contained: the file and the passphrase are enough to open it, on any machine.

Lose the passphrase and the backups are unreadable: there is no reset, by design.
"""

import base64
import hashlib
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from spoolsteward.i18n import _

MAGIC = b"CPGBK1\n"
PREFIX = "cpgbk1"
CHUNK = 1 << 20
MIN_PASSPHRASE = 12
_SCRYPT = dict(length=32, n=2**15, r=8, p=1)
_INFO = b"spoolsteward-backup-v1"


class CryptoError(ValueError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception as exc:
        raise CryptoError(_("recovery key is not valid")) from exc


def check_passphrase(passphrase: str) -> str | None:
    if len(passphrase) < MIN_PASSPHRASE:
        return _("The passphrase needs at least %(n)s characters", n=MIN_PASSPHRASE)
    return None


def make_recipient(passphrase: str) -> str:
    error = check_passphrase(passphrase)
    if error:
        raise CryptoError(error)
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    salt, nonce = os.urandom(16), os.urandom(12)
    wrapping = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
    wrapped = salt + nonce + AESGCM(wrapping).encrypt(nonce, raw, pub)
    return f"{PREFIX}.{_b64(pub)}.{_b64(wrapped)}"


def parse_recipient(bundle: str) -> tuple[bytes, bytes]:
    parts = (bundle or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise CryptoError(_("recovery key is not valid (expected cpgbk1.<public>.<wrapped>)"))
    pub, wrapped = _unb64(parts[1]), _unb64(parts[2])
    if len(pub) != 32 or len(wrapped) < 16 + 12 + 32 + 16:
        raise CryptoError(_("recovery key is not valid"))
    return pub, wrapped


def unwrap(pub: bytes, wrapped: bytes, passphrase: str) -> X25519PrivateKey:
    salt, nonce, ct = wrapped[:16], wrapped[16:28], wrapped[28:]
    try:
        wrapping = Scrypt(salt=salt, **_SCRYPT).derive(passphrase.encode())
        return X25519PrivateKey.from_private_bytes(AESGCM(wrapping).decrypt(nonce, ct, pub))
    except InvalidTag as exc:
        raise CryptoError("wrong passphrase") from exc


def _file_key(shared: bytes, eph_pub: bytes, pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + pub, info=_INFO).derive(shared)


def _nonce(counter: int) -> bytes:
    return counter.to_bytes(12, "big")


def _aad(flag: int, header_digest: bytes) -> bytes:
    return bytes([flag]) + header_digest


class EncryptWriter:
    """File-like sink: everything written is encrypted into `out` for the recipient. Call close() to finish."""

    def __init__(self, out, bundle: str):
        pub, wrapped = parse_recipient(bundle)
        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key = _file_key(eph.exchange(X25519PublicKey.from_public_bytes(pub)), eph_pub, pub)
        header = json.dumps({"v": 1, "pub": _b64(pub), "eph": _b64(eph_pub), "wrapped": _b64(wrapped)}).encode()
        self._digest = hashlib.sha256(header).digest()
        self._out, self._buf, self._n, self._closed = out, bytearray(), 0, False
        out.write(MAGIC + struct.pack(">I", len(header)) + header)
        self._aes = AESGCM(self.key)

    def _emit(self, data: bytes, flag: int) -> None:
        ct = self._aes.encrypt(_nonce(self._n), bytes(data), _aad(flag, self._digest))
        self._n += 1
        self._out.write(bytes([flag]) + struct.pack(">I", len(ct)) + ct)

    def write(self, data: bytes) -> int:
        self._buf += data
        while len(self._buf) > CHUNK:  # always keep at least one byte back: the last chunk must be the flagged one
            self._emit(self._buf[:CHUNK], 0)
            del self._buf[:CHUNK]
        return len(data)

    def close(self) -> None:
        if not self._closed:
            self._emit(self._buf, 1)
            self._closed = True

    def flush(self) -> None:
        pass


class DecryptReader:
    """File-like source over an encrypted backup. Give the passphrase, or the file key when it is already known."""

    def __init__(self, src, passphrase: str | None = None, key: bytes | None = None):
        if src.read(len(MAGIC)) != MAGIC:
            raise CryptoError("not an encrypted backup of this gateway")
        (hlen,) = struct.unpack(">I", self._exact(src, 4))
        if hlen > 4096:
            raise CryptoError("corrupt header")
        header = self._exact(src, hlen)
        self._digest = hashlib.sha256(header).digest()
        if key is None:
            meta = json.loads(header)
            pub, eph_pub = _unb64(meta["pub"]), _unb64(meta["eph"])
            priv = unwrap(pub, _unb64(meta["wrapped"]), passphrase or "")
            key = _file_key(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, pub)
        self._src, self._aes, self._n, self._buf, self._done = src, AESGCM(key), 0, bytearray(), False

    @staticmethod
    def _exact(src, n: int) -> bytes:
        data = src.read(n)
        if len(data) != n:
            raise CryptoError("file is truncated")
        return data

    def _next(self) -> None:
        flag = self._exact(self._src, 1)[0]
        (n,) = struct.unpack(">I", self._exact(self._src, 4))
        if n > CHUNK + 64:
            raise CryptoError("corrupt chunk")
        try:
            self._buf += self._aes.decrypt(_nonce(self._n), self._exact(self._src, n), _aad(flag, self._digest))
        except InvalidTag as exc:
            raise CryptoError("file is corrupt or has been modified") from exc
        self._n += 1
        if flag == 1:
            if self._src.read(1):
                raise CryptoError("unexpected data after the end")
            self._done = True

    def read(self, size: int = -1) -> bytes:
        while not self._done and (size < 0 or len(self._buf) < size):
            self._next()
        if size < 0:
            size = len(self._buf)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out
