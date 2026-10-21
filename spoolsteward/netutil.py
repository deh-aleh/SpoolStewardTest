"""Network helpers shared by the portal, the IPP gateway and the scanner proxy."""

from fastapi import Request


def client_ip(request: Request) -> str:
    """The address of the direct peer ("?" if the server can not tell)."""
    return request.client.host if request.client else "?"
