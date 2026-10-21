"""Pictures of the documentation: every reference points to a real file of docs/images with an alt text, the portal serves
only that folder (allowed types, no way out, safe headers) and the page renderer points the pictures at it."""

import re
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from markdown_it import MarkdownIt
from sqlalchemy import select

from spoolsteward.admin import docs
from spoolsteward.db.models import User
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
WEBP = b"RIFF\x20\0\0\0WEBPVP8 " + b"\0" * 20
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


# --- the pictures of the repository -------------------------------------------------------------------------------

def page_language(root: Path, md: Path) -> str | None:
    """The language of a Markdown file: the folder of a page, or the README at the top. None for the other files."""
    rel = md.relative_to(root).as_posix()
    if rel == "README.md":
        return "en"
    if rel == "README.it.md":
        return "it"
    m = re.fullmatch(r"docs/(en|it)/[^/]+\.md", rel)
    return m.group(1) if m else None


def image_references(text: str):
    """(src, alt text) of every Markdown picture, and the raw HTML parts that hold an <img> tag."""
    md = MarkdownIt("commonmark")          # html on: here a raw <img> must be found, not hidden
    pictures, raw = [], []
    for t in md.parse(text):
        if t.type == "html_block" and "<img" in t.content.lower():
            raw.append(t.content.strip())
        for c in t.children or []:
            if c.type == "image":
                pictures.append((c.attrGet("src") or "", md.renderer.renderInlineAsText(c.children or [], md.options, {})))
            elif c.type == "html_inline" and c.content.lower().startswith("<img"):
                raw.append(c.content)
    return pictures, raw


def problems(root: Path) -> list[str]:
    """Everything wrong with the pictures of a repository tree at `root`, one line each."""
    images, found, used = root / "docs" / docs.IMAGES, [], set()
    sources = sorted((root / "docs").rglob("*.md")) + [p for p in (root / "README.md", root / "README.it.md") if p.is_file()]
    for md in sources:
        where = md.relative_to(root).as_posix()
        lang = page_language(root, md)
        pictures, raw = image_references(md.read_text(encoding="utf-8"))
        if md.parent.name == lang:   # a page the portal shows: raw HTML is text there
            found += [f"{where}: raw HTML picture (the portal shows it as text), use ![alt](path): {r[:60]}" for r in raw]
        for r in raw:                # the README at the top (GitHub only): its <picture> files must exist too
            for src in re.findall(r'\b(?:src|srcset)="([^"]+)"', r):
                if not urlsplit(src).scheme and not (md.parent / unquote(src)).is_file():
                    found.append(f"{where}: {src}: missing file")
            found += [f"{where}: <img> without alt text" for tag in re.findall(r"<img\b[^>]*>", r, re.I)
                      if not re.search(r'\balt="[^"]+"', tag)]
        for src, alt in pictures:
            what = f"{where}: {src}"
            if not alt.strip():
                found.append(f"{what}: empty alt text")
            u = urlsplit(src)
            if u.scheme or u.netloc or u.query or u.fragment or src.startswith("/"):
                found.append(f"{what}: not a relative path to a file of docs/images")
                continue
            target = Path(md.parent, unquote(u.path))
            try:
                rel = target.resolve().relative_to(images.resolve()).as_posix()
            except ValueError:
                found.append(f"{what}: outside docs/images")
                continue
            if not target.is_file():
                found.append(f"{what}: missing file")
                continue
            used.add(rel)
            if rel.split("/")[0] in docs.LANGS and lang and rel.split("/")[0] != lang:
                found.append(f"{what}: a picture of the other language")
            if lang and md.parent.name == lang and docs.rewrite_image(src, lang) is None:
                found.append(f"{what}: the portal cannot show this path")
    for f in sorted(images.rglob("*")) if images.is_dir() else []:
        rel = f.relative_to(images).as_posix()
        if f.is_dir() and not f.is_symlink():
            if "/" not in rel and rel not in docs.LANGS:
                found.append(f"docs/images/{rel}: folders are only {', '.join(docs.LANGS)}")
            continue
        if rel == "README.md":
            continue
        if f.is_symlink():
            found.append(f"docs/images/{rel}: a symbolic link")
        elif not docs.image_name(rel) or rel != rel.lower():
            found.append(f"docs/images/{rel}: name or type not allowed (lower case, a-z 0-9 '-', .png .jpg .webp .svg)")
        elif f.stat().st_size > docs.MAX_IMAGE_BYTES:
            found.append(f"docs/images/{rel}: larger than {docs.MAX_IMAGE_BYTES} bytes")
        elif not docs._looks_like(f.read_bytes()[:4096], f.suffix.lower()):
            found.append(f"docs/images/{rel}: the content is not what the extension says")
        elif rel not in used:
            found.append(f"docs/images/{rel}: no page uses it")
    for lang in docs.LANGS:
        folder = root / "docs" / lang
        found += [f"docs/{lang}/{p.name}: only pages here, pictures go to docs/images"
                  for p in (sorted(folder.iterdir()) if folder.is_dir() else []) if p.suffix != ".md"]
    return found


def test_every_picture_of_the_docs_is_there_and_has_an_alt_text():
    found = problems(ROOT)
    assert not found, "\n".join(found)


def test_the_picture_checker_finds_each_mistake(tmp_path):
    (tmp_path / "docs" / "images" / "en").mkdir(parents=True)
    (tmp_path / "docs" / "it").mkdir()
    (tmp_path / "docs" / "images" / "en" / "ok.png").write_bytes(PNG)
    (tmp_path / "docs" / "images" / "en" / "unused.png").write_bytes(PNG)
    (tmp_path / "docs" / "images" / "en" / "fake.png").write_bytes(b"<html>")
    (tmp_path / "docs" / "images" / "Big Name.png").write_bytes(PNG)
    (tmp_path / "docs" / "images" / "README.md").write_text("# notes\n")
    (tmp_path / "docs" / "it" / "photo.png").write_bytes(PNG)
    (tmp_path / "docs" / "it" / "pagina.md").write_text(
        "# P\n\n![Fine](../images/en/ok.png)\n\n![](../images/en/ok.png)\n\n![Gone](../images/it/gone.png)\n\n"
        "![Out](../../secret.png)\n\n![Web](https://example.com/x.png)\n\n![Fake](../images/en/fake.png)\n\n<img src=x>\n")
    (tmp_path / "README.md").write_text('<picture>\n  <img src="docs/images/none.svg">\n</picture>\n\n# R\n')
    found = "\n".join(problems(tmp_path))
    assert "pagina.md: ../images/en/ok.png: a picture of the other language" in found
    assert "pagina.md: ../images/en/ok.png: empty alt text" in found
    assert "../images/it/gone.png: missing file" in found
    assert "../../secret.png: outside docs/images" in found
    assert "https://example.com/x.png: not a relative path" in found
    assert "docs/it/pagina.md: raw HTML picture" in found
    assert "README.md: docs/images/none.svg: missing file" in found and "README.md: <img> without alt text" in found
    assert "docs/images/en/unused.png: no page uses it" in found
    assert "docs/images/en/fake.png: the content is not what the extension says" in found
    assert "docs/images/Big Name.png: name or type not allowed" in found
    assert "docs/it/photo.png: only pages here" in found
    assert "docs/images/README.md" not in found


def test_every_file_of_the_docs_folder_is_in_the_release_package(tmp_path):
    out = subprocess.run(["sh", str(ROOT / "scripts" / "make-release-package.sh"), "3.2.1", str(tmp_path)], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    with tarfile.open(tmp_path / "spoolsteward-3.2.1.tar.gz") as tf:
        names = set(tf.getnames())
    wanted = {f.relative_to(ROOT).as_posix() for f in (ROOT / "docs").rglob("*")
              if f.is_file() and not any(p.startswith(".") for p in f.relative_to(ROOT).parts)}
    assert "docs/images/README.md" in wanted
    assert not {f for f in wanted if f"spoolsteward-3.2.1/{f}" not in names}


# --- the address that serves them ----------------------------------------------------------------------------------

@pytest.fixture
def pictures(tmp_path, monkeypatch):
    """A docs folder of its own: one page and a few pictures."""
    root = tmp_path / "docs"
    (root / "images" / "en" / "sub").mkdir(parents=True)
    (root / "en").mkdir()
    (root / "en" / "pics.md").write_text("# Pics\n\n![The profile page](../images/en/me.png \"Profile\")\n")
    (root / "en" / "secret.png").write_bytes(PNG)                       # in the docs, not in docs/images
    (root / "images" / "arch.png").write_bytes(PNG)
    (root / "images" / "en" / "me.png").write_bytes(PNG)
    (root / "images" / "en" / "sub" / "step-1.jpg").write_bytes(JPEG)
    (root / "images" / "en" / "photo.webp").write_bytes(WEBP)
    (root / "images" / "flow.svg").write_bytes(SVG)
    (root / "images" / "en" / "anim.gif").write_bytes(b"GIF89a")
    (root / "images" / "en" / ".hidden.png").write_bytes(PNG)
    (root / "images" / "README.md").write_text("# notes\n")
    (root / "images" / "en" / "fake.png").write_text("<html><script>alert(1)</script></html>")
    (root / "images" / "en" / "big.png").write_bytes(PNG + b"\0" * docs.MAX_IMAGE_BYTES)
    (root / "images" / "en" / "folder.png").mkdir()
    (tmp_path / "outside.png").write_bytes(PNG)
    (root / "images" / "en" / "out.png").symlink_to(tmp_path / "outside.png")
    (root / "images" / "en" / "inside.png").symlink_to(root / "images" / "en" / "me.png")
    (root / "images" / "en" / "sneaky.png").symlink_to(root / "images" / "en" / ".hidden.png")
    monkeypatch.setattr(docs, "DOCS_DIRS", (root,))
    return root


async def login(portal, name, admin=False):
    make_user(portal.sm, name, password=GOOD, admin=admin)
    with portal.sm() as s:
        s.scalars(select(User).where(User.username == name)).one().language = "en"
        s.commit()
    await portal_login(portal, name, GOOD)
    return portal


@pytest.mark.parametrize("path, media_type", [
    ("en/me.png", "image/png"), ("arch.png", "image/png"), ("en/sub/step-1.jpg", "image/jpeg"),
    ("en/photo.webp", "image/webp"), ("flow.svg", "image/svg+xml"), ("en/inside.png", "image/png"),
])
async def test_a_picture_is_served_with_its_type_and_safe_headers(portal, pictures, path, media_type):
    await login(portal, "boss", admin=True)
    r = await portal.get(f"/docs/images/{path}")
    assert r.status_code == 200 and r.headers["content-type"] == media_type
    assert r.content == (pictures / "images" / path).read_bytes()
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"   # an SVG opened by itself runs nothing
    assert r.headers["cache-control"].startswith("private")


async def test_every_logged_in_user_sees_the_pictures_and_nobody_else(portal, pictures):
    r = await portal.get("/docs/images/en/me.png", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    await login(portal, "ann")                                       # not an administrator: the user guide has pictures too
    assert (await portal.get("/docs/images/en/me.png")).status_code == 200


@pytest.mark.parametrize("path", [
    "en/missing.png", "en/anim.gif", "en/.hidden.png", ".hidden.png", "README.md", "en", "en/", "", "en/folder.png",
    "en/fake.png", "en/big.png", "en/out.png", "en/sneaky.png", "en/me.png.txt", "en/a%20b.png", "en/me.png%00.txt",
    "..%2Fen%2Fsecret.png", "en%2F..%2F..%2Fen%2Fsecret.png", "%2e%2e/en/secret.png", "en//me.png", "en/sub/x/y/z.png",
])
async def test_anything_else_is_404(portal, pictures, path):
    await login(portal, "boss", admin=True)
    assert (await portal.get(f"/docs/images/{path}")).status_code == 404


@pytest.mark.parametrize("rel", ["../en/secret.png", "en/../../en/secret.png", "/etc/passwd.png", "en/./me.png",
                                 "en\\me.png", "en/me.png\0", "./en/me.png"])
def test_reading_never_leaves_the_folder(pictures, rel):
    assert docs.read_image(rel) is None


def test_a_link_to_the_whole_images_folder_is_refused(tmp_path, monkeypatch):
    root, elsewhere = tmp_path / "docs", tmp_path / "elsewhere"
    (root / "en").mkdir(parents=True)
    elsewhere.mkdir()
    (elsewhere / "a.png").write_bytes(PNG)
    (root / "images").symlink_to(elsewhere)
    monkeypatch.setattr(docs, "DOCS_DIRS", (root,))
    assert docs.read_image("a.png") is None


async def test_a_page_shows_its_picture_through_the_portal(portal, pictures):
    await login(portal, "boss", admin=True)
    r = await portal.get("/docs/en/pics")
    img = re.search(r'<img [^>]*src="/docs/images/[^>]*>', r.text).group(0)
    assert f'src="/docs/images/en/me.png?v={docs.app_version()}"' in img
    assert 'alt="The profile page"' in img and 'title="Profile"' in img and 'loading="lazy"' in img
    assert (await portal.get(re.search(r'src="([^"]+)"', img).group(1).replace("&amp;", "&"))).status_code == 200
    assert "img-src 'self' data:" in r.headers["content-security-policy"]   # the page policy allows it as it is
    index = (await portal.get("/docs/en/")).text
    assert 'href="/docs/en/pics"' in index and "images" not in index.split('class="doctiles"')[1]


# --- the page renderer ---------------------------------------------------------------------------------------------

def render(text, lang="en"):
    return docs.render_markdown(text, lang, {"en": {"backup"}, "it": {"backup"}})[0]


def test_a_picture_of_the_images_folder_points_at_the_portal():
    v = docs.app_version()
    h = render('![The *print* key](../images/en/me-print-key.png "Key") ![Diagram](../images/architecture.svg)')
    assert f'<img src="/docs/images/en/me-print-key.png?v={v}" alt="The print key" title="Key" loading="lazy"' in h
    assert f'src="/docs/images/architecture.svg?v={v}"' in h
    assert f'src="/docs/images/it/a.png?v={v}"' in render("![A](../images/it/a.png)", "it")


def test_a_picture_from_another_site_becomes_a_link():
    h = render("![Logo](https://example.com/logo.png) and [![Badge](https://example.com/b.svg)](https://example.com)")
    assert "<img" not in h
    assert '<a href="https://example.com/logo.png" target="_blank" rel="noopener noreferrer">Logo</a>' in h
    assert '<a href="https://example.com" target="_blank" rel="noopener noreferrer">Badge</a>' in h   # no link inside a link


def test_a_picture_that_is_not_a_file_of_the_images_folder_is_never_shown():
    version = docs.app_version()
    ref = f"v{version}" if version != "unknown" else "main"
    h = render("![Icon](../../spoolsteward/admin/static/apple-touch-icon.png)")
    assert "<img" not in h and f'href="https://github.com/deh-aleh/SpoolSteward/blob/{ref}/spoolsteward/admin/static/apple-touch-icon.png"' in h
    for src in ("data:image/png;base64,iVBORw0KGgo=", "/docs/images/en/a.png", "../images/en/.hidden.png", "../images/en/a.gif",
                "../images/en/a.png?x=1", "../images/../../etc/passwd.png", "javascript:alert(1)", "//evil.example/a.png",
                "images/en/a.png"):
        h = render(f"![Alt]({src})")
        assert "<img" not in h, src
        assert "Alt" in h, src                                           # the alt text stays readable
