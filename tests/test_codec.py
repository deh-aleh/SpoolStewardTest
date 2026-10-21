from pathlib import Path

import pytest

from spoolsteward.ipp import codec as c

FIX = Path(__file__).parent / "fixtures"


def test_make_request_roundtrip():
    req = c.make_request(c.PRINT_JOB, 7, printer_uri="ipp://x/printers/q", job_name="doc.pdf")
    req.data = b"%PDF-fake"
    out = c.decode(c.encode(req))
    assert out.code == c.PRINT_JOB and out.request_id == 7
    assert out.get(c.TAG_OPERATION, "job-name") == "doc.pdf"
    assert out.get(c.TAG_OPERATION, "printer-uri") == "ipp://x/printers/q"
    assert out.data == b"%PDF-fake"


def test_set_replaces_attribute():
    req = c.make_request(c.PRINT_JOB, 1, requesting_user_name="evil")
    req.group(c.TAG_OPERATION).set("requesting-user-name", c.TAG_NAME, "mario")
    out = c.decode(c.encode(req))
    assert out.get(c.TAG_OPERATION, "requesting-user-name") == "mario"
    assert len(out.group(c.TAG_OPERATION).attrs) == 3


def test_collection_roundtrip():
    inner = [c.Attr("x-dimension", [c.Val(c.TAG_INTEGER, 21000)])]
    col = [
        c.Attr("media-size", [c.Val(c.TAG_BEG_COLLECTION, inner)]),
        c.Attr("media-type", [c.Val(c.TAG_KEYWORD, "stationery"), c.Val(c.TAG_KEYWORD, "plain")]),
    ]
    msg = c.IppMessage((2, 0), 0, 1, [c.Group(c.TAG_PRINTER, [
        c.Attr("media-col", [c.Val(c.TAG_BEG_COLLECTION, col), c.Val(c.TAG_BEG_COLLECTION, col)]),
        c.Attr("copies", [c.Val(c.TAG_INTEGER, -3)]),
        c.Attr("flag", [c.Val(c.TAG_BOOLEAN, True)]),
    ])])
    raw = c.encode(msg)
    back = c.decode(raw)
    assert c.encode(back) == raw
    media = back.group(c.TAG_PRINTER).get("media-col")
    assert len(media.values) == 2
    assert media.values[0].value[0].values[0].value[0].values[0].value == 21000


def test_utf8_title():
    req = c.make_request(c.PRINT_JOB, 1, job_name="report è àù 日本.pdf")
    assert c.decode(c.encode(req)).get(c.TAG_OPERATION, "job-name") == "report è àù 日本.pdf"


def test_truncated_raises():
    raw = c.encode(c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://x/p"))
    with pytest.raises(c.IppError):
        c.decode(raw[:-10])


def test_decode_head_offset_and_data():
    req = c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://x/p")
    req.data = b"DATA"
    raw = c.encode(req)
    msg, off = c.decode_head(raw)
    assert raw[off:] == b"DATA" and msg.data == b""


def test_error_response():
    r = c.decode(c.encode(c.make_error_response(c.STATUS_NOT_POSSIBLE, 9, "limit reached")))
    assert r.code == 0x0404 and r.request_id == 9
    assert r.get(c.TAG_OPERATION, "status-message") == "limit reached"


@pytest.mark.parametrize("name", ["cups_virtual", "hp7300"])
def test_real_capture_roundtrip_is_lossless(name):
    raw = (FIX / f"{name}_resp.bin").read_bytes()
    msg = c.decode(raw)
    assert msg.code == c.STATUS_OK
    assert msg.get(c.TAG_PRINTER, "printer-uri-supported")
    assert c.encode(msg) == raw


@pytest.mark.parametrize("name", ["cups_virtual", "hp7300"])
def test_real_request_decodes(name):
    msg = c.decode((FIX / f"{name}_req.bin").read_bytes())
    assert msg.code == c.GET_PRINTER_ATTRIBUTES
    assert msg.get(c.TAG_OPERATION, "printer-uri")


def _nested(depth: int) -> bytes:
    import struct
    head = struct.pack(">BBHI", 2, 0, c.PRINT_JOB, 1) + bytes([c.TAG_OPERATION])
    head += b"\x47\x00\x12attributes-charset\x00\x05utf-8" + b"\x34\x00\x03col\x00\x00"
    head += (b"\x4a\x00\x00\x00\x01m" + b"\x34\x00\x00\x00\x00") * depth
    return head + b"\x37\x00\x00\x00\x00" * (depth + 1) + b"\x03"


def test_collection_nesting_is_bounded():
    c.decode_head(_nested(10))                       # realistic depth is fine
    with pytest.raises(c.IppError, match="too deep"):
        c.decode_head(_nested(5000))                 # used to be an unhandled RecursionError (HTTP 500)
