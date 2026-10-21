"""The documentation inside the portal: the Markdown pages of docs/ shown as web pages. Administrators see all of them; every
other user only the few pages in USER_PAGES (the guide for users and the Windows guide), never the rest (architecture, .env...).

The files are the same ones read on GitHub, so nothing is written twice. Safety rules:
  * `lang` is exactly "en" or "it"; `page` must be the name of a .md file that is really in the folder (the list comes from a
    directory listing, the URL text is never joined to a path);
  * raw HTML written in a Markdown file is shown as text, never passed on (markdown-it `html: False`), and links keep
    markdown-it's own check (no `javascript:` and the like);
  * links are rewritten so they work in the portal: the other pages stay in /docs, anything else in the repository
    (source files, the README) goes to GitHub, at the tag of the version that runs."""

import posixpath
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markdown_it import MarkdownIt
from markdown_it.token import Token
from starlette.concurrency import run_in_threadpool

import app as app_package
from app import i18n
from app.admin.common import current_user, render
from app.backup.core import app_version
from app.db.models import User

router = APIRouter()
LANGS = ("en", "it")
# what a user who is not an administrator may read (page names, per language); administrators read everything
USER_PAGES = {"en": {"user-guide", "windows"}, "it": {"guida-utente", "windows"}}
REPO = "https://github.com/deh-aleh/SpoolSteward"
# the image puts the docs here; deploy/install.sh puts them in $PREFIX/docs (its default PREFIX); a source tree has them next to app/
DOCS_DIRS = (Path("/opt/spoolsteward/docs"),
             Path(app_package.__file__).resolve().parents[1] / "docs")
EXTERNAL = {"target": "_blank", "rel": "noopener noreferrer"}


def docs_root() -> Path | None:
    """The first folder that holds the documentation, or None when this installation has none."""
    for folder in DOCS_DIRS:
        if (folder / "en").is_dir() or (folder / "it").is_dir():
            return folder
    return None


def page_files(root: Path, lang: str) -> dict[str, Path]:
    """{name without .md: file} of the pages of one language: the only names a URL may ask for."""
    try:
        return {p.stem: p for p in sorted((root / lang).iterdir()) if p.suffix == ".md" and p.is_file()}
    except OSError:
        return {}


def make_markdown() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def slugify(text: str) -> str:
    """GitHub's heading anchors: lower case, spaces become '-', punctuation goes (letters of any language, digits, '-' and '_' stay)."""
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def _plain(inline: Token) -> str:
    return "".join(c.content for c in (inline.children or []) if c.type in ("text", "code_inline"))


def _github(path: str, fragment: str, folder: bool = False) -> str:
    version = app_version()
    ref = f"v{version}" if version != "unknown" else "main"  # no installed metadata (a source tree): the tag may not exist
    return f"{REPO}/{'tree' if folder else 'blob'}/{ref}/{quote(path)}" + (f"#{fragment}" if fragment else "")


def rewrite_link(href: str, lang: str, names: dict[str, set[str]]) -> tuple[str, bool]:
    """(new address, opens in a new tab). `names` = the pages that exist, per language. Anything that is not a plain
    page address or an http(s) one becomes '#': a link from a file must never turn into something live."""
    if href.startswith("#"):
        return href, False
    try:
        u = urlsplit(href)
    except ValueError:
        return "#", False
    if u.scheme in ("http", "https") and u.netloc:
        return href, True
    if u.scheme or u.netloc or not u.path or u.path.startswith("/"):
        return "#", False
    repo_path = posixpath.normpath(posixpath.join("docs", lang, u.path))  # where the file really is, from the repository root
    if repo_path.startswith(".."):
        return "#", False
    frag = quote(u.fragment, safe="-_.~")
    if repo_path == "docs/README.md":
        return f"/docs/{lang}/" + (f"#{frag}" if frag else ""), False
    m = re.fullmatch(r"docs/(en|it)/([^/]+)\.md", repo_path)
    if m and m.group(2) in names.get(m.group(1), ()):
        return f"/docs/{m.group(1)}/{quote(m.group(2))}" + (f"#{frag}" if frag else ""), False
    return _github(repo_path, frag, folder=u.path.endswith("/")), True


def render_markdown(text: str, lang: str, names: dict[str, set[str]]) -> tuple[str, str]:
    """(html, title). The html comes from markdown-it with raw HTML switched off, so it is safe to show as it is."""
    md = make_markdown()
    tokens = md.parse(text)
    title, used, i = "", {}, 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open" and i + 1 < len(tokens) and tokens[i + 1].type == "inline":
            plain = _plain(tokens[i + 1])
            if t.tag == "h1" and not title:
                title = plain
            slug = slugify(plain)
            if slug:
                n = used.get(slug, 0)
                used[slug] = n + 1
                t.attrSet("id", slug if n == 0 else f"{slug}-{n}")
        elif t.type == "inline":
            for c in t.children or []:
                if c.type == "link_open":
                    new, external = rewrite_link(c.attrGet("href") or "", lang, names)
                    c.attrSet("href", new)
                    if external:
                        for k, v in EXTERNAL.items():
                            c.attrSet(k, v)
        i += 1
    # the first paragraph under the title is the "Documentation index · English · Italiano" line: the page has its own
    for j, t in enumerate(tokens[:6]):
        if t.type == "paragraph_open" and tokens[j + 1].children and tokens[j + 1].children[0].type == "link_open" \
                and tokens[j + 1].children[0].attrGet("href") == "/docs/" + lang + "/":
            del tokens[j:j + 3]
            break
    return md.renderer.render(tokens, md.options, {}), title


def counterpart(text: str, lang: str, names: dict[str, set[str]]) -> str | None:
    """The same page in the other language, taken from the 'English · Italiano' line every page starts with."""
    other = "it" if lang == "en" else "en"
    m = re.search(rf"\]\(\.\./{other}/([^)/#\s]+)\.md\)", text[:1500])
    return m.group(1) if m and m.group(1) in names.get(other, ()) else None


def page_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:5]:
            if line.startswith("# "):
                return line[2:].strip().replace("`", "")
    except OSError:
        pass
    return path.stem


def index_entries(root: Path, lang: str, files: dict[str, Path]) -> list[tuple[str, str]]:
    """[(name, title)] in the order of docs/README.md (its table has one row per topic), then any page the table forgot."""
    order: list[str] = []
    try:
        readme = (root / "README.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        readme = ""
    col = 1 if lang == "en" else 2
    for line in readme.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        m = re.search(r"\]\((?:\./)?" + lang + r"/([^)/#]+)\.md\)", cells[col]) if len(cells) > col else None
        if m and m.group(1) in files and m.group(1) not in order:
            order.append(m.group(1))
    order += [n for n in files if n not in order]
    return [(n, page_title(files[n])) for n in order]


def build(lang: str, page: str | None, admin: bool = True) -> dict:
    """Everything the template needs; raises 404 for an unknown language or page. `kind` = "missing" when no docs are installed."""
    if lang not in LANGS:
        raise HTTPException(404)
    root = docs_root()
    if root is None:
        return {"kind": "missing", "doc_lang": lang}
    all_files = {code: {n: p for n, p in page_files(root, code).items() if admin or n in USER_PAGES[code]} for code in LANGS}
    names = {code: set(files) for code, files in all_files.items()}
    files = all_files[lang]
    other = "it" if lang == "en" else "en"
    if not page:
        return {"kind": "index", "doc_lang": lang, "other": other, "entries": index_entries(root, lang, files),
                "switch": f"/docs/{other}/", "title": ""}
    path = files.get(page)  # only a name found in the folder listing gets here
    if path is None:
        raise HTTPException(404)
    text = path.read_text(encoding="utf-8", errors="replace")
    html, title = render_markdown(text, lang, names)
    twin = counterpart(text, lang, names)
    return {"kind": "page", "doc_lang": lang, "other": other, "html": html, "title": title or page,
            "switch": f"/docs/{other}/" + (quote(twin) if twin else "")}


async def _show(request: Request, user: User, lang: str, page: str | None):
    ctx = await run_in_threadpool(build, lang, page, user.is_admin)
    return render(request, "docs.html", user, status_code=404 if ctx["kind"] == "missing" else 200, **ctx)


@router.get("/docs")
async def docs_home(user: User = Depends(current_user)):
    """Opens the documentation in the language of the portal (Italian, otherwise English)."""
    return RedirectResponse("/docs/" + ("it" if i18n.current().startswith("it") else "en") + "/", status_code=303)


@router.get("/docs/windows", include_in_schema=False)
async def docs_windows(user: User = Depends(current_user)):
    """The Windows guide in the language of the portal (the profile page links here)."""
    lang = "it" if i18n.current().startswith("it") else "en"
    return RedirectResponse(f"/docs/{lang}/windows", status_code=303)


@router.get("/docs/{lang}/", response_class=HTMLResponse)
async def docs_index(request: Request, lang: str, user: User = Depends(current_user)):
    return await _show(request, user, lang, None)


@router.get("/docs/{lang}/{page}", response_class=HTMLResponse)
async def docs_page(request: Request, lang: str, page: str, user: User = Depends(current_user)):
    return await _show(request, user, lang, page)
