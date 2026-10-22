"""Security headers of the answers.

Content-Security-Policy for the portal pages: they carry no inline CSS or JavaScript (styles and scripts are files under
/admin/static), so the policy can forbid every inline style and script: a script injected into a page by mistake (a name, a
title, a message) is not run.

Every answer also says `nosniff` (the browser must believe the type it is told, also for a downloaded scan), and, on HTTPS,
Strict-Transport-Security (SPOOLSTEWARD_HSTS_SECONDS). The pages add: no referrer outside the site, no camera / microphone /
location, never in a frame.
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


# only for pages (HTML): the IPP and eSCL answers are not shown by a browser
PAGE_HEADERS = (
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"x-frame-options", b"DENY"),
)


def hsts() -> tuple[bytes, bytes] | None:
    """The header that tells a browser to use only HTTPS for this server for a while, or None when it is switched off (0)."""
    return (b"strict-transport-security", b"max-age=%d" % settings.hsts_seconds) if settings.hsts_seconds > 0 else None


def header() -> tuple[bytes, bytes] | None:
    """The header to add to a page, or None when the administrator switched it off."""
    mode = settings.content_security_policy
    if mode == "off":
        return None
    name = b"content-security-policy-report-only" if mode == "report-only" else b"content-security-policy"
    return name, POLICY.encode()


class SecurityHeaders:
    """ASGI middleware: adds the policy and the page headers to every HTML answer (the IPP and eSCL answers are not pages), and
    `nosniff` and, on HTTPS only, HSTS to every answer. A header a route already set is not set twice."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        secure = scope.get("scheme") in ("https", "wss")

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                have = {k.lower() for k, _v in headers}
                is_page = any(k.lower() == b"content-type" and v.lower().startswith(b"text/html") for k, v in headers)
                extra = [(b"x-content-type-options", b"nosniff")]
                if secure and hsts() is not None:
                    extra.append(hsts())     # a browser ignores it on plain HTTP, and the spec says not to send it there
                if is_page:
                    extra += [h for h in (header(), *PAGE_HEADERS) if h is not None]
                message = {**message, "headers": [*headers, *(h for h in extra if h[0] not in have)]}
            await send(message)

        await self.app(scope, receive, send_with_policy)
