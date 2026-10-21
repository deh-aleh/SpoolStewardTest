from spoolsteward.ipp import codec as c
from spoolsteward.ipp.rewrite import rewrite_uris

HOSTS = {"localhost"}


def _msg(*attrs):
    return c.IppMessage((2, 0), 0, 1, [c.Group(c.TAG_PRINTER, list(attrs))])


def _uri(name, *vals):
    return c.Attr(name, [c.Val(c.TAG_URI, v) for v in vals])


def test_rewrites_internal_ipp_and_http():
    m = _msg(
        _uri("printer-uri-supported", "ipp://localhost/printers/q"),
        _uri("printer-more-info", "http://localhost:631/printers/q?x=1"),
    )
    n = rewrite_uris(m, HOSTS, "ipp://gw:8631", "http://gw:8631")
    g = m.group(c.TAG_PRINTER)
    assert n == 2
    assert g.get("printer-uri-supported").first == "ipp://gw:8631/printers/q"
    assert g.get("printer-more-info").first == "http://gw:8631/printers/q?x=1"


def test_leaves_external_and_device_uri():
    m = _msg(_uri("device-uri", "ipp://localhost:60000/ipp/print"),
             _uri("printer-icons", "https://cdn.example.com/i.png"))
    assert rewrite_uris(m, HOSTS, "ipp://gw", "http://gw") == 0


def test_rewrites_inside_collections_and_multivalue():
    inner = [_uri("x-uri", "ipp://localhost/printers/q")]
    m = _msg(c.Attr("col", [c.Val(c.TAG_BEG_COLLECTION, inner)]),
             _uri("many", "ipp://localhost/a", "ipp://other/b"))
    assert rewrite_uris(m, HOSTS, "ipp://gw", "http://gw") == 2
    assert m.group(c.TAG_PRINTER).get("many").values[1].value == "ipp://other/b"


def test_advertise_auth_describes_the_gateway_not_cups():
    from spoolsteward.ipp import codec
    from spoolsteward.ipp.rewrite import advertise_auth
    g = codec.Group(codec.TAG_PRINTER, [
        codec.Attr("printer-uri-supported", [codec.Val(codec.TAG_URI, "ipps://h/printers/q")]),
        codec.Attr("uri-security-supported", [codec.Val(codec.TAG_KEYWORD, "none")]),
        codec.Attr("uri-authentication-supported", [codec.Val(codec.TAG_KEYWORD, "requesting-user-name")]),
    ])
    msg = codec.IppMessage((2, 0), 0, 1, [g])
    advertise_auth(msg)
    assert [v.value for v in g.get("uri-security-supported").values] == ["tls"]
    assert [v.value for v in g.get("uri-authentication-supported").values] == ["basic"]
