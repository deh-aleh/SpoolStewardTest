"""Content-Security-Policy for the portal pages.

The pages carry no inline CSS or JavaScript (styles and scripts are files under /admin/static), so the policy can forbid
every inline style and script: a script injected into a page by mistake (a name, a title, a message) is not run.
"""

from spoolsteward.config import settings

POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "form-action 'self'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
))


def header() -> tuple[bytes, bytes] | None:
    """The header to add to a page, or None when the administrator switched it off."""
    mode = settings.content_security_policy
    if mode == "off":
        return None
    name = b"content-security-policy-report-only" if mode == "report-only" else b"content-security-policy"
    return name, POLICY.encode()


class SecurityHeaders:
    """ASGI middleware: adds the policy to every HTML answer (the IPP and eSCL answers are not pages)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                is_page = any(k.lower() == b"content-type" and v.lower().startswith(b"text/html") for k, v in headers)
                extra = header() if is_page else None
                if extra is not None:
                    message = {**message, "headers": [*headers, extra]}
            await send(message)

        await self.app(scope, receive, send_with_policy)
