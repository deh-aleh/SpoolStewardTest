"""Rewrite internal (CUPS-side) URIs in IPP responses so clients keep talking to the gateway."""

from urllib.parse import urlsplit

from spoolsteward.ipp.codec import TAG_BEG_COLLECTION, TAG_KEYWORD, TAG_URI, Attr, IppMessage, Val

# attributes describing CUPS internals, never meant for clients
_SKIP = {"device-uri"}


def _rewrite_attrs(attrs: list[Attr], internal_hosts: set[str], ipp_base: str, http_base: str) -> int:
    n = 0
    for attr in attrs:
        if attr.name in _SKIP:
            continue
        for i, (tag, value) in enumerate(attr.values):
            if tag == TAG_BEG_COLLECTION:
                n += _rewrite_attrs(value, internal_hosts, ipp_base, http_base)
            elif tag == TAG_URI and isinstance(value, str):
                new = _rewrite_uri(value, internal_hosts, ipp_base, http_base)
                if new != value:
                    attr.values[i] = attr.values[i]._replace(value=new)
                    n += 1
    return n


def _rewrite_uri(uri: str, internal_hosts: set[str], ipp_base: str, http_base: str) -> str:
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        return uri
    if host is None or host.lower() not in internal_hosts:
        return uri
    if parts.scheme in ("ipp", "ipps"):
        base = ipp_base
    elif parts.scheme in ("http", "https"):
        base = http_base
    else:
        return uri
    tail = parts.path + (f"?{parts.query}" if parts.query else "")
    return base + tail


def rewrite_uris(msg: IppMessage, internal_hosts: set[str], ipp_base: str, http_base: str) -> int:
    """In-place. Returns number of rewritten values."""
    hosts = {h.lower() for h in internal_hosts}
    return sum(_rewrite_attrs(g.attrs, hosts, ipp_base, http_base) for g in msg.groups)


def advertise_auth(msg: IppMessage, tag_printer: int = 0x04) -> None:
    """CUPS says what its own private listener does ("none" / "requesting-user-name"). Clients (Mopria, AirPrint)
    decide from these two attributes whether to send credentials, so describe the gateway: TLS and HTTP Basic."""
    for g in msg.groups:
        if g.tag != tag_printer or g.get("printer-uri-supported") is None:
            continue
        n = len(g.get("printer-uri-supported").values)
        for name, word in (("uri-security-supported", "tls"), ("uri-authentication-supported", "basic")):
            g.set(name, TAG_KEYWORD, word)
            g.get(name).values = [Val(TAG_KEYWORD, word)] * n
