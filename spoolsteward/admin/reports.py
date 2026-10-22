"""Usage report for administrators: who printed most in a period, optionally split by printer, as a page, a CSV or a PDF.

Counts the prints that used (or are using) paper: completed, accepted and held ones, the same states the quotas count.
Only the active audit is read: rows already moved to a compressed archive are not included (the page says so)."""

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spoolsteward.timeutil import local_tz
from spoolsteward.admin.common import current_admin, flash, render
from spoolsteward.admin.routes import _csv_safe
from spoolsteward.config import settings
from spoolsteward.db.models import AuditArchive, Group, JobKind, JobLog, JobState, User
from spoolsteward.i18n import _

router = APIRouter(prefix="/admin")
COUNTED = (JobState.completed, JobState.accepted, JobState.held)
MAX_TOP = 1000
DEFAULT_DAYS = 30


PERIODS = ("today", "last7", "month", "last-month", "last30", "year")


def period_range(name: str, today: date) -> tuple[date, date] | None:
    """The first and last day of a ready-made period (the buttons above the dates), None for an unknown name."""
    if name == "today":
        return today, today
    if name == "last7":
        return today - timedelta(days=6), today
    if name == "last30":
        return today - timedelta(days=DEFAULT_DAYS - 1), today
    if name == "month":
        return today.replace(day=1), today
    if name == "last-month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if name == "year":
        return today.replace(month=1, day=1), today
    return None


def parse_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def window(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, day after end 00:00) in the installation's time zone, as UTC."""
    tz = local_tz()
    return (datetime.combine(start, time.min, tz).astimezone(UTC),
            datetime.combine(end + timedelta(days=1), time.min, tz).astimezone(UTC))


def build(db: Session, start: date, end: date, exclude: list[int], top: int | None, detail: bool) -> dict:
    """{"users": [{"name", "group", "jobs", "pages", "printers": [{"printer", "jobs", "pages"}]}], "total": {...}, "cut": n}.
    Users are ordered by pages, then jobs, then name. `top` keeps the first N (the total is always of everybody counted)."""
    lo, hi = window(start, end)
    who = func.coalesce(User.username, JobLog.user_name, "?")
    stmt = (select(who, Group.name, JobLog.printer, func.count(JobLog.id), func.coalesce(func.sum(JobLog.pages), 0))
            .select_from(JobLog).join(User, User.id == JobLog.user_id, isouter=True).join(Group, Group.id == User.group_id, isouter=True)
            .where(JobLog.kind == JobKind.print, JobLog.state.in_(COUNTED), JobLog.ts >= lo, JobLog.ts < hi)
            .group_by(who, Group.name, JobLog.printer))
    if exclude:
        stmt = stmt.where((User.group_id.is_(None)) | (User.group_id.not_in(exclude)))
    users: dict[str, dict] = {}
    for name, group, printer, jobs, pages in db.execute(stmt):
        u = users.setdefault(name, {"name": name, "group": group or "", "jobs": 0, "pages": 0, "printers": []})
        u["jobs"] += jobs
        u["pages"] += int(pages)
        u["printers"].append({"printer": printer or "?", "jobs": jobs, "pages": int(pages)})
    ordered = sorted(users.values(), key=lambda u: (-u["pages"], -u["jobs"], u["name"]))
    total = {"jobs": sum(u["jobs"] for u in ordered), "pages": sum(u["pages"] for u in ordered), "users": len(ordered)}
    for u in ordered:
        u["printers"].sort(key=lambda p: (-p["pages"], -p["jobs"], p["printer"]))
        if not detail:
            u["printers"] = []
    cut = max(len(ordered) - top, 0) if top else 0
    return {"users": ordered[:top] if top else ordered, "total": total, "cut": cut}


def archive_note(db: Session, start: date) -> bool:
    """True when the period may reach rows that were archived (older than the oldest active row)."""
    if not db.scalar(select(func.count(AuditArchive.id)).where(AuditArchive.source_table == "job_log")):
        return False
    oldest = db.scalar(select(func.min(JobLog.ts)))
    return oldest is None or window(start, start)[0] < (oldest if oldest.tzinfo else oldest.replace(tzinfo=UTC))


def to_csv(data: dict, detail: bool) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["rank", "user", "group"] + (["printer"] if detail else []) + ["jobs", "pages"])
    for i, u in enumerate(data["users"], 1):
        if detail:
            for p in u["printers"]:
                w.writerow([i, _csv_safe(u["name"]), _csv_safe(u["group"]), _csv_safe(p["printer"]), p["jobs"], p["pages"]])
        else:
            w.writerow([i, _csv_safe(u["name"]), _csv_safe(u["group"]), u["jobs"], u["pages"]])
    return out.getvalue()


def _latin(text: str) -> str:
    """The PDF uses the standard fonts, which only hold Western letters: anything else becomes '?' (the CSV has it all)."""
    return escape(str(text).encode("latin-1", "replace").decode("latin-1"))


def to_pdf(data: dict, start: date, end: date, excluded: list[str], top: int | None, detail: bool) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title="Print report", author="SpoolSteward")
    t = data["total"]
    story = [Paragraph("Print report", styles["Title"]),
             Paragraph(f"Period: {start.isoformat()} to {end.isoformat()} ({_latin(settings.timezone)})", styles["Normal"]),
             Paragraph(f"Users counted: {t['users']} &middot; jobs: {t['jobs']} &middot; pages: {t['pages']}", styles["Normal"])]
    if excluded:
        story.append(Paragraph("Groups left out: " + _latin(", ".join(excluded)), styles["Normal"]))
    if data["cut"]:
        story.append(Paragraph(f"Only the first {top} users are listed ({data['cut']} more are in the totals).", styles["Normal"]))
    story.append(Spacer(1, 6 * mm))
    head = ["#", "User", "Group"] + (["Printer"] if detail else []) + ["Jobs", "Pages"]
    rows, bold = [head], []
    for i, u in enumerate(data["users"], 1):
        rows.append([str(i), _latin(u["name"]), _latin(u["group"])] + ([""] if detail else []) + [str(u["jobs"]), str(u["pages"])])
        if detail:
            bold.append(len(rows) - 1)
            for p in u["printers"]:
                rows.append(["", "", ""] + [_latin(p["printer"]), str(p["jobs"]), str(p["pages"])])
    cells = [[Paragraph(c, styles["BodyText"]) if c and not c.isdigit() else c for c in r] for r in rows[1:]]
    table = Table([head] + cells, repeatRows=1, hAlign="LEFT")
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17181D")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("ALIGN", (-2, 0), (-1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    style += [("BACKGROUND", (0, r), (-1, r), colors.HexColor("#F4EFE6")) for r in bold]
    table.setStyle(TableStyle(style))
    story.append(table)
    doc.build(story)
    return buf.getvalue()


def _params(start: str, end: str, top: str, request: Request) -> tuple[date, date, int | None] | None:
    today = datetime.now(local_tz()).date()
    s = parse_day(start) if start.strip() else today - timedelta(days=DEFAULT_DAYS - 1)
    e = parse_day(end) if end.strip() else today
    if s is None or e is None:
        flash(request, _("The dates must look like 2026-10-31"), "err")
        return None
    if e < s:
        flash(request, _("The end of the period is before its start"), "err")
        return None
    n = None
    if top.strip():
        if not (top.strip().isascii() and top.strip().isdigit() and 1 <= int(top) <= MAX_TOP):
            flash(request, _("The number of users must be between 1 and %(max)s", max=MAX_TOP), "err")
            return None
        n = int(top)
    return s, e, n


@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request, start: str = "", end: str = "", top: str = "20", detail: int = 0,
                      period: str = "", exclude: list[int] = Query(default=[]), admin: User = Depends(current_admin)):
    def load(s: date | None, e: date | None, n: int | None):
        with request.app.state.sessionmaker() as db:
            groups = [(g.id, g.name) for g in db.scalars(select(Group).order_by(Group.name))]
            if s is None:
                return groups, None, False
            return groups, build(db, s, e, exclude, n, bool(detail)), archive_note(db, s)

    today = datetime.now(local_tz()).date()
    if (preset := period_range(period, today)):          # a period button was pressed: it wins over the typed dates
        start, end = preset[0].isoformat(), preset[1].isoformat()
    p = _params(start, end, top, request)
    groups, data, archived = await run_in_threadpool(load, *(p or (None, None, None)))
    return render(request, "report.html", admin, groups=groups, data=data, archived=archived, exclude=exclude, detail=bool(detail),
                  f={"start": start or (p[0] if p else today - timedelta(days=DEFAULT_DAYS - 1)).isoformat(),
                     "end": end or (p[1] if p else today).isoformat(), "top": top}, max_top=MAX_TOP,
                  periods=[(n, period_range(n, today) == (p[0], p[1]) if p else False) for n in PERIODS])


@router.get("/report/download")
async def report_download(request: Request, format: str = "csv", start: str = "", end: str = "", top: str = "20", detail: int = 0,
                          exclude: list[int] = Query(default=[]), admin: User = Depends(current_admin)):
    p = _params(start, end, top, request)
    if p is None or format not in ("csv", "pdf"):
        from spoolsteward.admin.common import back
        return back("/admin/report")
    s, e, n = p

    def load():
        with request.app.state.sessionmaker() as db:
            names = [g.name for g in db.scalars(select(Group).where(Group.id.in_(exclude)).order_by(Group.name))] if exclude else []
            return build(db, s, e, exclude, n, bool(detail)), names

    data, names = await run_in_threadpool(load)
    name = f"print-report-{s.isoformat()}-{e.isoformat()}"
    if format == "csv":
        return Response(to_csv(data, bool(detail)), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    pdf = await run_in_threadpool(to_pdf, data, s, e, names, n, bool(detail))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
