"""Minimal IPP/1.1 + 2.x wire codec (RFC 8010). Lossless round trip, collections included."""

import struct
from dataclasses import dataclass, field
from typing import NamedTuple

# --- operations / status codes (subset) ---
PRINT_JOB = 0x0002
VALIDATE_JOB = 0x0004
CREATE_JOB = 0x0005
SEND_DOCUMENT = 0x0006
GET_JOBS = 0x000A
GET_PRINTER_ATTRIBUTES = 0x000B

STATUS_OK = 0x0000
STATUS_BAD_REQUEST = 0x0400
STATUS_FORBIDDEN = 0x0401
# 0x0402 and 0x0403 are defined by the standard (RFC 8011); the gateway answers HTTP 401 for missing credentials
# (see `_unauthorized` in spoolsteward/ipp/gateway.py) and 0x0401 for refusals.
STATUS_NOT_AUTHENTICATED = 0x0402
STATUS_NOT_AUTHORIZED = 0x0403
STATUS_NOT_POSSIBLE = 0x0404
STATUS_TOO_LARGE = 0x0408  # client-error-request-entity-too-large

# --- delimiter tags ---
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED = 0x05
TAG_SUBSCRIPTION = 0x06
TAG_EVENT_NOTIFICATION = 0x07
TAG_RESOURCE = 0x08
TAG_DOCUMENT = 0x09
TAG_SYSTEM = 0x0A

# --- value tags ---
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_RANGE = 0x33  # rangeOfInteger: two big-endian int32
TAG_TEXT_LANG = 0x35
TAG_NAME_LANG = 0x36
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_URI_SCHEME = 0x46
TAG_CHARSET = 0x47
TAG_NATURAL_LANG = 0x48
TAG_MIME = 0x49
TAG_MEMBER_NAME = 0x4A
TAG_BEG_COLLECTION = 0x34
TAG_END_COLLECTION = 0x37

_STR_TAGS = {
    TAG_TEXT, TAG_NAME, TAG_KEYWORD, TAG_URI, TAG_URI_SCHEME,
    TAG_CHARSET, TAG_NATURAL_LANG, TAG_MIME,
}
_OUT_OF_BAND = {0x10, 0x12, 0x13, 0x15, 0x16, 0x17}  # value-less


class IppError(ValueError):
    pass


class Val(NamedTuple):
    tag: int
    value: "int | bool | str | bytes | list[Attr] | None"


@dataclass
class Attr:
    name: str
    values: list[Val] = field(default_factory=list)

    @property
    def first(self):
        return self.values[0].value if self.values else None


@dataclass
class Group:
    tag: int
    attrs: list[Attr] = field(default_factory=list)

    def get(self, name: str) -> Attr | None:
        return next((a for a in self.attrs if a.name == name), None)

    def set(self, name: str, tag: int, value) -> None:
        """Replace attribute `name` with a single value (append if missing)."""
        new = Attr(name, [Val(tag, value)])
        for i, a in enumerate(self.attrs):
            if a.name == name:
                self.attrs[i] = new
                return
        self.attrs.append(new)


@dataclass
class IppMessage:
    version: tuple[int, int]
    code: int  # operation-id (request) or status-code (response)
    request_id: int
    groups: list[Group] = field(default_factory=list)
    data: bytes = b""

    def group(self, tag: int) -> Group | None:
        return next((g for g in self.groups if g.tag == tag), None)

    def get(self, group_tag: int, name: str):
        """First value of attribute `name` in first group with `group_tag`, else None."""
        g = self.group(group_tag)
        a = g.get(name) if g else None
        return a.first if a else None


# ---------------------------------------------------------------- decode

def _decode_value(tag: int, raw: bytes):
    if tag in _STR_TAGS:
        return raw.decode("utf-8", errors="surrogateescape")
    if tag in (TAG_INTEGER, TAG_ENUM):
        if len(raw) != 4:
            raise IppError(f"bad integer length {len(raw)}")
        return struct.unpack(">i", raw)[0]
    if tag == TAG_BOOLEAN:
        if len(raw) != 1:
            raise IppError("bad boolean length")
        return raw[0] != 0
    if tag in _OUT_OF_BAND:
        return None
    return bytes(raw)  # octetString, dateTime, resolution, range, *WithLanguage, unknown


def _encode_value(tag: int, value) -> bytes:
    if tag in _STR_TAGS:
        return value.encode("utf-8", errors="surrogateescape")
    if tag in (TAG_INTEGER, TAG_ENUM):
        return struct.pack(">i", value)
    if tag == TAG_BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag in _OUT_OF_BAND:
        return b""
    return bytes(value)


class _Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.buf):
            raise IppError("truncated IPP message")
        chunk = self.buf[self.pos:end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def peek(self) -> int:
        if self.pos >= len(self.buf):
            raise IppError("truncated IPP message")
        return self.buf[self.pos]


MAX_COLLECTION_DEPTH = 16  # real printers use 3-4; unbounded recursion = RecursionError = unhandled 500


def _read_collection(r: _Reader, depth: int = 0) -> list[Attr]:
    """Called after begCollection header consumed; reads members up to endCollection."""
    if depth > MAX_COLLECTION_DEPTH:
        raise IppError("collections nested too deep")
    members: list[Attr] = []
    while True:
        tag = r.u8()
        name_len = r.u16()
        r.take(name_len)  # name must be empty here
        raw = r.take(r.u16())
        if tag == TAG_END_COLLECTION:
            return members
        if tag != TAG_MEMBER_NAME:
            raise IppError(f"expected memberAttrName, got 0x{tag:02x}")
        attr = Attr(raw.decode("utf-8", errors="surrogateescape"))
        while True:  # one or more value tags for this member
            vtag = r.peek()
            if vtag in (TAG_MEMBER_NAME, TAG_END_COLLECTION):
                break
            r.u8()
            r.take(r.u16())  # empty name
            vraw = r.take(r.u16())
            if vtag == TAG_BEG_COLLECTION:
                attr.values.append(Val(vtag, _read_collection(r, depth + 1)))
            else:
                attr.values.append(Val(vtag, _decode_value(vtag, vraw)))
        members.append(attr)


def decode_head(buf: bytes) -> tuple[IppMessage, int]:
    """Parse header + attribute groups. Returns (message, offset_of_data)."""
    r = _Reader(buf)
    major, minor = r.u8(), r.u8()
    code = r.u16()
    request_id = struct.unpack(">I", r.take(4))[0]
    msg = IppMessage((major, minor), code, request_id)
    group: Group | None = None
    attr: Attr | None = None
    while True:
        tag = r.u8()
        if tag == TAG_END:
            break
        if tag < 0x10:  # delimiter
            group = Group(tag)
            msg.groups.append(group)
            attr = None
            continue
        if group is None:
            raise IppError("attribute before group delimiter")
        name = r.take(r.u16()).decode("utf-8", errors="surrogateescape")
        raw = r.take(r.u16())
        if tag == TAG_BEG_COLLECTION:
            value = _read_collection(r, 1)
        else:
            value = _decode_value(tag, raw)
        if name:
            attr = Attr(name, [Val(tag, value)])
            group.attrs.append(attr)
        else:
            if attr is None:
                raise IppError("additional value without attribute")
            attr.values.append(Val(tag, value))
    return msg, r.pos


def decode(buf: bytes) -> IppMessage:
    msg, off = decode_head(buf)
    msg.data = bytes(buf[off:])
    return msg


# ---------------------------------------------------------------- encode

def _encode_attr_values(out: bytearray, name: str, values: list[Val]) -> None:
    for i, (tag, value) in enumerate(values):
        nb = name.encode("utf-8", errors="surrogateescape") if i == 0 else b""
        if tag == TAG_BEG_COLLECTION:
            out += struct.pack(">BH", tag, len(nb)) + nb + struct.pack(">H", 0)
            for member in value:
                mname = member.name.encode("utf-8", errors="surrogateescape")
                out += struct.pack(">BHH", TAG_MEMBER_NAME, 0, len(mname)) + mname
                _encode_attr_values(out, "", member.values)
            out += struct.pack(">BHH", TAG_END_COLLECTION, 0, 0)
        else:
            raw = _encode_value(tag, value)
            out += struct.pack(">BH", tag, len(nb)) + nb + struct.pack(">H", len(raw)) + raw


def encode(msg: IppMessage) -> bytes:
    out = bytearray(struct.pack(">BBHI", *msg.version, msg.code, msg.request_id))
    for g in msg.groups:
        out.append(g.tag)
        for a in g.attrs:
            _encode_attr_values(out, a.name, a.values)
    out.append(TAG_END)
    out += msg.data
    return bytes(out)


# ---------------------------------------------------------------- helpers

def make_request(op: int, request_id: int = 1, **op_attrs: str) -> IppMessage:
    """Request with standard operation attributes; extra str attrs (hyphen via '_')."""
    g = Group(TAG_OPERATION, [
        Attr("attributes-charset", [Val(TAG_CHARSET, "utf-8")]),
        Attr("attributes-natural-language", [Val(TAG_NATURAL_LANG, "en")]),
    ])
    for k, v in op_attrs.items():
        tag = TAG_URI if k.endswith("uri") else TAG_NAME if k.endswith("name") else TAG_MIME
        g.attrs.append(Attr(k.replace("_", "-"), [Val(tag, v)]))
    return IppMessage((2, 0), op, request_id, [g])


def make_error_response(status: int, request_id: int, message: str = "") -> IppMessage:
    g = Group(TAG_OPERATION, [
        Attr("attributes-charset", [Val(TAG_CHARSET, "utf-8")]),
        Attr("attributes-natural-language", [Val(TAG_NATURAL_LANG, "en")]),
    ])
    if message:
        g.attrs.append(Attr("status-message", [Val(TAG_TEXT, message)]))
    return IppMessage((2, 0), status, request_id, [g])
