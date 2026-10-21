from typing import Protocol


class AuthProvider(Protocol):
    name: str

    async def authenticate(self, username: str, password: str) -> bool: ...
