import asyncio
import logging
from pathlib import Path

from pyrad import packet
from pyrad.client import Client, Timeout
from pyrad.dictionary import Dictionary

log = logging.getLogger(__name__)
_DICT = Path(__file__).with_name("dictionary")


class RadiusProvider:
    name = "radius"

    def __init__(self, host: str, port: int, secret: str, timeout: int = 5, retries: int = 1):
        self.host, self.port = host, port
        self.secret = secret.encode()
        self.timeout = timeout
        self.retries = max(1, retries)  # pyrad: number of attempts, 0 would send nothing

    def _check(self, username: str, password: str) -> bool:
        client = Client(
            server=self.host, authport=self.port, secret=self.secret,
            dict=Dictionary(str(_DICT)), timeout=self.timeout, retries=self.retries,
        )
        req = client.CreateAuthPacket(code=packet.AccessRequest, User_Name=username)
        req["User-Password"] = req.PwCrypt(password)
        req["NAS-Identifier"] = "spoolsteward"
        req.add_message_authenticator()  # required by modern servers (BlastRADIUS mitigation)
        try:
            reply = client.SendPacket(req)
        except (Timeout, OSError) as exc:
            log.error("RADIUS unreachable: %s", exc)
            return False  # fail closed
        if reply.code == packet.AccessAccept:
            return True
        why = "; ".join(reply.get("Reply-Message") or []) or f"code {reply.code}"
        log.warning("RADIUS rejected user=%r: %s", username, why)  # never log the password
        return False

    async def authenticate(self, username: str, password: str) -> bool:
        if not username or not password:
            return False
        return await asyncio.to_thread(self._check, username, password)
