"""Print from the browser: the way to print from a phone (or any device) with no printer set-up at all.

The user is already signed in to the portal, so no print key is needed. The job goes through exactly the same
checks as an IPP job: quota (atomic), audit, identity `gw:<user>`.
"""

import logging
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.timeutil import local_tz
from app.admin.common import back, check_csrf, current_user, flash, render
from app.config import settings
from app.cups_client import list_queues
from app.db.models import User
from app.ipp import codec
from app.policy import audit, control, ordering, permissions, pages as pages_mod
from app.i18n import _

log = logging.getLogger("app.print")
router = APIRouter(prefix="/me")
COMMENT_MAX = 50
MULTIPART_OVERHEAD = 256 * 1024  # headers/boundaries/small fields around the file


def detect_type(data: bytes) -> str | None:
    """What the file REALLY is. The browser's claim and the file name are not trusted."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # Plain text: no NUL bytes anywhere, valid UTF-8, and no control characters other than tab/newline/CR/form feed.
    # (Binary formats start with ASCII-looking magic like "PK\x03\x04" or "MZ\x90", so those controls give them away.)
    if data and b"\x00" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if all(ch in "\t\n\r\x0c" or ch >= " " and ch != "\x7f" for ch in text):
            return "text/plain"
    return None


def clean_filename(raw: str | None) -> str:
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable())[:200].strip(" .")
    return name or "document"


@router.get("/print", response_class=HTMLResponse)
async def print_form(request: Request, user: User = Depends(current_user)):
    with request.app.state.sessionmaker() as db:
        blocked = {n for n, c in control.controls(db).items() if c.blocked}
    every = [q for q in await list_queues(request.app) if q["name"].lower() not in blocked]  # blocked = not offered
    pos = await run_in_threadpool(ordering.positions, request.app.state.sessionmaker, user.id)  # their own order, if they set one
    perms = await run_in_threadpool(permissions.load, request.app.state.sessionmaker, user)
    allowed = permissions.allowed_queues(perms, every)  # a printer the account may not use is not offered at all
    queues = ordering.arrange([q for q in allowed if q.get("online", True)], pos)
    offline = [q["name"] for q in allowed if not q.get("online", True)]  # the device does not answer: do not offer it
    outside = perms.window if perms.window is not None and not permissions.in_window(perms.window, datetime.now(UTC), local_tz()) else None
    return render(request, "print.html", user, queues=queues, offline=offline, max_mb=settings.web_upload_mb,
                  comment_max=COMMENT_MAX, max_copies=settings.web_max_copies, perms=perms,
                  forbidden_all=bool(every) and not allowed, outside_hours=permissions.describe_window(outside) if outside else "",
                  queues_js={q["name"]: {"media": q.get("media", []), "media_default": q.get("media_default", ""), "color": bool(q.get("color")),
                                         "duplex": bool(q.get("duplex"))} for q in queues})


class _Refuse(Exception):
    """A form problem the person is told about with a message on the page they came from."""

    def __init__(self, message: str, to: str = "/me/print"):
        super().__init__(message)
        self.message, self.to = message, to


@dataclass
class _Job:
    """What the form asked for, checked and ready to be sent."""

    queue: str
    queue_info: dict
    copies: int
    title: str
    comment: str
    media: str
    orientation: str
    color: str
    sides: str
    ranges: list
    data: bytes
    mime: str
    ipp_orientation: str = ""   # what is asked of CUPS; for landscape PDFs the page itself is turned instead (see landscape_pdf)
    counted: int = 0
    pages: int | None = None
    opts: dict = field(default_factory=dict)


@router.post("/print")
async def print_submit(request: Request, user: User = Depends(current_user)):
    try:
        form, upload, data, mime = await _read_upload(request)
        job = await _check_form(request, form, upload, data, mime)
    except _Refuse as stop:
        flash(request, stop.message, "err")
        return back(stop.to)
    sm = request.app.state.sessionmaker
    # the same decision the IPP gateway takes (printer, hours, colour, sides); the form already hides what is not allowed
    perms = await run_in_threadpool(permissions.load, sm, user)
    perm = permissions.decide(perms, job.queue, {"color": job.queue_info.get("color"), "duplex": job.queue_info.get("duplex")},
                              {k: v for k, v in (("print-color-mode", job.color), ("sides", job.sides)) if v},
                              settings.permission_mode, audit.now_utc(), local_tz()) if perms.restricted else None
    if perm is not None and perm.changed:
        job.opts["adjusted"] = list(perm.changed)
    decision, log_id = await run_in_threadpool(_reserve, sm, user, job.queue, job.title, job.opts, job.pages, perm)
    if not decision.allowed:
        return await _refused(request, sm, user, job.queue, decision, perm)
    req = _ipp_request(user, job, perm)
    if perm is not None and perm.changed:
        await run_in_threadpool(permissions.notify_adjusted, sm, user.id, job.queue, perm)
    out = await _send(request, sm, log_id, job.queue, req)
    await _report(request, sm, user, log_id, job, out)
    return back("/me/print")


async def _read_upload(request: Request):
    """The size and CSRF checks, then the file itself and what it really is."""
    limit = settings.web_upload_mb << 20
    # Checked BEFORE the body is parsed: the parser spools the whole upload to disk.
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        raise HTTPException(411, "Content-Length required")
    if int(declared) > limit + MULTIPART_OVERHEAD:
        raise _Refuse(_("The file is larger than %(mb)s MB", mb=settings.web_upload_mb))
    form = await request.form(max_files=1, max_fields=16)
    check_csrf(request, str(form.get("csrf_token", "")))

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise _Refuse(_("Choose a file to print"))
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise _Refuse(_("The file is larger than %(mb)s MB", mb=settings.web_upload_mb))
    if not data:
        raise _Refuse(_("The file is empty"))
    mime = detect_type(data)
    if mime is None:
        raise _Refuse(_("Unsupported file: PDF, JPEG, PNG, GIF or plain text only"))
    return form, upload, data, mime


async def _check_form(request: Request, form, upload, data: bytes, mime: str) -> _Job:
    """Every field of the form, in the order the person would meet the problems."""
    queues = {q["name"]: q for q in await list_queues(request.app)}
    queue = str(form.get("queue", ""))
    if queue in queues and not queues[queue].get("online", True):
        raise _Refuse(_("%(printer)s does not answer (off or unplugged?): choose another one or try again later", printer=queue))
    if queue not in queues:
        raise _Refuse(_("Choose a printer from the list"))
    copies_raw = str(form.get("copies", "1")).strip()
    if not (copies_raw.isascii() and copies_raw.isdigit() and 1 <= int(copies_raw) <= settings.web_max_copies):
        raise _Refuse(_("Copies must be between 1 and %(max)s", max=settings.web_max_copies))
    copies = int(copies_raw)
    title = clean_filename(getattr(upload, "filename", None))
    comment = "".join(ch for ch in str(form.get("comment", "")) if ch.isprintable()).strip()
    if len(comment) > COMMENT_MAX:
        raise _Refuse(_("The comment can be at most %(n)s characters long", n=COMMENT_MAX))

    queue_info = queues[queue]
    media, orientation, color, sides, ranges = _check_choices(form, queue_info)
    job = _Job(queue, queue_info, copies, title, comment, media, orientation, color, sides, ranges, data, mime,
               ipp_orientation=orientation)
    await _prepare_document(job)
    job.opts = _audit_options(form, job)
    return job


def _check_choices(form, queue_info: dict):
    media = str(form.get("media", ""))
    if media and media not in {m["id"] for m in queue_info.get("media", [])}:
        raise _Refuse(_("Paper size not available on this printer"))
    orientation = str(form.get("orientation", ""))
    if orientation not in ("", "portrait", "landscape"):
        raise HTTPException(400, "invalid orientation")
    color = str(form.get("color", ""))
    if color not in ("", "color", "monochrome"):
        raise HTTPException(400, "invalid colour mode")
    sides = str(form.get("sides", ""))
    if sides not in ("", "one-sided", "two-sided-long-edge", "two-sided-short-edge"):
        raise HTTPException(400, "invalid sides")
    try:
        ranges = pages_mod.parse_ranges(str(form.get("pages", "")))
    except ValueError as exc:
        raise _Refuse(str(exc))
    return media, orientation, color, sides, ranges


async def _prepare_document(job: _Job) -> None:
    """Turn a landscape PDF, count the pages that will really be printed (ranges and copies)."""
    if job.orientation == "landscape" and not job.mime.startswith("image/"):
        if job.mime != "application/pdf":
            raise _Refuse(_("Landscape is available for PDF files and images, not for text files"))
        try:
            job.data = await run_in_threadpool(pages_mod.landscape_pdf, job.data)
        except ValueError:
            log.warning("web print: could not turn the pages of %r", job.title)
            raise _Refuse(_("This PDF can not be turned to landscape (protected or damaged): choose automatic orientation"))
        job.ipp_orientation = ""

    total = await run_in_threadpool(pages_mod.count_pages, job.data, job.mime)
    try:
        job.counted = pages_mod.pages_in_ranges(job.ranges, total)
    except ValueError as exc:
        raise _Refuse(str(exc))
    job.pages = job.counted * job.copies if job.counted else None


def _audit_options(form, job: _Job) -> dict:
    opts = {"via": "web", "document_format": job.mime, "copies": job.copies}
    if job.comment:
        opts["comment"] = job.comment  # shown under the file name in the audit
    if job.media:
        opts["media"] = job.media
    if job.orientation:
        opts["orientation"] = job.orientation
    if job.color:
        opts["color"] = job.color
    if job.sides:
        opts["sides"] = job.sides
    if job.ranges:
        opts["page_ranges"] = str(form.get("pages", "")).strip()[:100]
    if job.counted:
        opts["pages_counted"] = job.pages
    return opts


async def _refused(request: Request, sm, user: User, queue: str, decision, perm) -> RedirectResponse:
    """The job was not admitted: say why, and where the person should look next."""
    if perm is not None and not perm.allowed and decision.reason == perm.reason:
        await run_in_threadpool(permissions.notify_refused, sm, user.id, queue, perm)
        flash(request, decision.text(), "err")  # not a quota matter either: stay on the print page
        return back("/me/print")
    if decision.reason in (control.BLOCKED_REASON, control.MAINTENANCE_REASON):
        flash(request, decision.text(), "err")  # not a quota matter: stay on the print page
        return back("/me/print")
    if decision.quota:
        await run_in_threadpool(permissions.notify_quota, sm, user.id, queue, decision.quota)
    flash(request, _("%(reason)s. You can ask for more quota below.", reason=decision.text()), "err")
    return back("/me")


def _ipp_request(user: User, job: _Job, perm) -> codec.IppMessage:
    """The Print-Job request for CUPS, with the options of the form and the corrections of the permissions."""
    req = codec.make_request(codec.PRINT_JOB, 1, printer_uri=f"ipp://localhost/printers/{job.queue}",
                             requesting_user_name=settings.cups_user_prefix + user.username,
                             job_name=job.title, document_format=job.mime)
    job_attrs = []
    if job.copies > 1:
        job_attrs.append(codec.Attr("copies", [codec.Val(codec.TAG_INTEGER, job.copies)]))
    if job.media:
        job_attrs.append(codec.Attr("media", [codec.Val(codec.TAG_KEYWORD, job.media)]))
    if job.ipp_orientation:
        job_attrs.append(codec.Attr("orientation-requested", [codec.Val(codec.TAG_ENUM, 3 if job.ipp_orientation == "portrait" else 4)]))
    if job.color:
        job_attrs.append(codec.Attr("print-color-mode", [codec.Val(codec.TAG_KEYWORD, job.color)]))
    if job.sides:
        job_attrs.append(codec.Attr("sides", [codec.Val(codec.TAG_KEYWORD, job.sides)]))
    if perm is not None:  # colour / sides corrected by the permissions replace what the form asked for
        job_attrs = [a for a in job_attrs if a.name not in perm.set_attrs]
        job_attrs += [codec.Attr(n, [codec.Val(codec.TAG_KEYWORD, v)]) for n, v in perm.set_attrs.items()]
    if job.ranges:
        job_attrs.append(codec.Attr("page-ranges", [codec.Val(codec.TAG_RANGE, struct.pack(">ii", lo, hi or 2147483647))
                                                    for lo, hi in job.ranges]))
    if job_attrs:
        req.groups.append(codec.Group(codec.TAG_JOB, job_attrs))
    req.data = job.data
    return req


async def _send(request: Request, sm, log_id: int, queue: str, req: codec.IppMessage) -> codec.IppMessage | None:
    """Post the job to CUPS. None = CUPS did not answer in a way that can be read."""
    try:
        resp = await request.app.state.upstream.post(
            f"/printers/{queue}", content=codec.encode(req), headers={"Content-Type": "application/ipp", "Host": "localhost"})
        return codec.decode(resp.content) if resp.headers.get("content-type", "").startswith("application/ipp") else None
    except (httpx.HTTPError, codec.IppError):
        log.exception("web print: CUPS unreachable")
        return None
    except BaseException:
        await run_in_threadpool(_finalize, sm, log_id, False, None, "aborted before reaching CUPS")
        raise


async def _report(request: Request, sm, user: User, log_id: int, job: _Job, out: codec.IppMessage | None) -> None:
    """Close the audit row with what CUPS said and tell the person."""
    ok = out is not None and out.code < 0x0100
    job_id = out.get(codec.TAG_JOB, "job-id") if ok else None
    reason = None if ok else f"CUPS status {'0x%04x' % out.code if out else 'unreachable'}"
    await run_in_threadpool(_finalize, sm, log_id, ok, job_id, reason)
    if ok:
        log.info("web print user=%s queue=%s job=%s file=%r", user.username, job.queue, job_id, job.title)
        flash(request, _("Sent to %(printer)s: %(title)s", printer=job.queue, title=job.title)
              + (_(" — %(n)s page(s)", n=job.pages) if job.pages else ""))
    else:
        flash(request, _("The print server did not accept the file. Try another format (PDF works best)."), "err")


def _reserve(sm, user, queue, title, options, pages=None, deny=None):
    with sm() as session:
        return audit.reserve(session, user, queue, title, options, local_tz(), pages, deny=deny)


def _finalize(sm, log_id, ok, job_id, reason):
    with sm() as session:
        audit.finalize(session, log_id, ok, job_id, reason)
