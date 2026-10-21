"""Builds the PDFs the gateway prints on its own (test page, periodic maintenance page) with no dependency: a few
lines of text, ink-level bars and colour swatches. Everything printed comes from the caller, nothing is specific to
a printer model."""

import re
from spoolsteward import i18n
from spoolsteward.i18n import N_, _

W, H, MARGIN = 595, 842, 48  # A4 in points; the printer's driver scales it to the paper it has
CYAN, MAGENTA, YELLOW, BLACK = (0, .68, .94), (.93, 0, .55), (1, .95, 0), (0, 0, 0)
SWATCHES = [(N_("Cyan"), CYAN), (N_("Magenta"), MAGENTA), (N_("Yellow"), YELLOW), (N_("Black"), BLACK),
            (N_("Red"), (.9, .1, .1)), (N_("Green"), (.1, .7, .2)), (N_("Blue"), (.1, .2, .85))]
# Languages the pages may be printed in besides English. The built-in fonts only cover Latin-1 text, so Russian and
# Chinese would need a font file embedded in the PDF: until that exists every page is English, whatever the admin's language.
FONT_LANGUAGES: frozenset[str] = frozenset()


def pdf_language() -> str:
    """The language a page generated now is written in (call it inside the request of the admin who asked)."""
    return i18n.current() if i18n.current() in FONT_LANGUAGES else i18n.SOURCE
_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


def _esc(text: str) -> str:
    raw = text.encode("cp1252", "replace").decode("cp1252")
    raw = "".join(ch if ch.isprintable() else " " for ch in raw)
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def rgb(color: str | None) -> tuple[float, float, float]:
    m = _HEX.match(color or "")
    if not m:
        return (.53, .53, .53)
    v = int(m.group(1), 16)
    return ((v >> 16) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255)


class _Canvas:
    def __init__(self):
        self.pages: list[list[str]] = []
        self.y = 0.0
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = H - MARGIN

    def need(self, height: float):
        if self.y - height < MARGIN:
            self.new_page()

    @property
    def ops(self) -> list[str]:
        return self.pages[-1]

    def text(self, x: float, size: int, text: str, bold: bool = False, gray: float = 0):
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {gray} g {x:.1f} {self.y:.1f} Td ({_esc(text)}) Tj ET")

    def line(self, size: int, text: str, bold: bool = False, gray: float = 0, x: float = MARGIN):
        self.need(size * 1.5)
        self.y -= size * 1.35
        self.text(x, size, text, bold, gray)

    def rect(self, x: float, y: float, w: float, h: float, color: tuple[float, float, float]):
        r, g, b = color
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f")

    def skip(self, points: float):
        self.y -= points


def _fit(text: str, size: int, width: float) -> str:
    limit = max(int(width / (size * 0.5)), 8)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def build(title: str, subtitle: str = "", *, sections: list[tuple[str, list[tuple[str, str]]]] = (),
          ink: list[dict] = (), ink_title: str = "Ink left", swatches: str = "", stripes: bool = False) -> bytes:
    """`sections`: [(heading, [(label, value)])]. `ink`: [{name, level, color}] drawn as bars.
    `swatches`: "" (none), "color" (ink colours) or "mono" (black and greys). `stripes`: fine lines across the page,
    which make every nozzle fire."""
    c = _Canvas()
    c.line(20, _fit(title, 20, W - 2 * MARGIN), bold=True)
    if subtitle:
        c.line(10, _fit(subtitle, 10, W - 2 * MARGIN), gray=.35)
    c.skip(8)
    if ink:
        c.need(24 + 22 * len(ink))
        c.line(13, ink_title, bold=True)
        for i in ink:
            c.need(22)
            c.skip(18)
            level = i.get("level")
            known = isinstance(level, int) and level >= 0
            c.text(MARGIN, 10, _fit(str(i.get("name", "")), 10, 150))
            bx, bw = MARGIN + 160, 260
            c.rect(bx, c.y - 1, bw, 9, (.88, .88, .88))
            if known:
                c.rect(bx, c.y - 1, bw * min(level, 100) / 100, 9, rgb(i.get("color")))
            c.text(bx + bw + 10, 10, f"{level}%" if known else "?")
        c.skip(6)
    for heading, rows in sections:
        rows = [(a, b) for a, b in rows if b not in (None, "")]
        if not rows:
            continue
        c.need(40)
        c.skip(6)
        c.line(13, heading, bold=True)
        for label, value in rows:
            c.need(16)
            c.skip(14)
            c.text(MARGIN, 10, _fit(str(label), 10, 190), gray=.3)
            c.text(MARGIN + 200, 10, _fit(str(value), 10, W - MARGIN - 200 - MARGIN))
    if stripes:
        c.need(70)
        c.skip(12)
        for k in range(14):
            c.rect(MARGIN, c.y - k * 4, W - 2 * MARGIN, 1.4 + (k % 3) * .6, BLACK)
        c.skip(14 * 4 + 6)
    if swatches:
        colors = [(_(n), c_) for n, c_ in SWATCHES] if swatches == "color" else \
            [(_("Black"), BLACK)] + [(_("Grey %(p)s%%", p=p), (1 - p / 100,) * 3) for p in (75, 50, 25, 10)]
        for name, col in colors:
            c.need(44)
            c.skip(40)
            c.rect(MARGIN, c.y, W - 2 * MARGIN, 34, col)
            lum = .3 * col[0] + .59 * col[1] + .11 * col[2]
            c.ops.append(f"BT /F1 9 Tf {1 if lum < .5 else 0} g {MARGIN + 6} {c.y + 12:.1f} Td ({_esc(name)}) Tj ET")
    return _pdf(c.pages)


def _pdf(pages: list[list[str]]) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"")  # 1: catalog, 2: page tree (filled below)
    add(b"")
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for ops in pages:
        data = "\n".join(ops).encode("cp1252", "replace")
        content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Contents {content} 0 R "
                        f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> >>".encode()))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
