"""Archived (compressed) audit logs: listed, readable, downloadable. There is deliberately no way to delete them."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.i18n import _
from app.admin.common import back, current_admin, flash, get_db, render, verify_csrf
from app.audit import archive
from app.audit.adminlog import record
from app.config import settings
from app.db.models import AuditArchive, User

log = logging.getLogger("app.admin")
router = APIRouter(prefix="/admin/archive")
VIEW_PAGE = 100


def _get(db: Session, archive_id: int) -> AuditArchive:
    a = db.get(AuditArchive, archive_id)
    if a is None:
        raise HTTPException(404)
    return a


def _host_path(a: AuditArchive) -> str:
    base = settings.audit_archive_host_path.rstrip("/")
    return f"{base}/{Path(a.path).name}" if base else ""


@router.get("", response_class=HTMLResponse)
def listing(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditArchive).order_by(AuditArchive.id.desc())).all()
    return render(
        request, "archive.html", admin,
        rows=[{"a": a, "status": archive.archive_status(a), "name": Path(a.path).name, "host": _host_path(a)}
              for a in rows],
        directory=settings.audit_archive_dir, host_directory=settings.audit_archive_host_path,
        max_mb=settings.audit_max_mb,
        sizes={t: archive.estimate_bytes(db, t) for t in archive.TABLES},
    )


@router.get("/{archive_id}/view", response_class=HTMLResponse)
def view(request: Request, archive_id: int, page: int = 1, q: str = "", admin: User = Depends(current_admin),
         db: Session = Depends(get_db)):
    a = _get(db, archive_id)
    path = Path(a.path)
    if not path.exists():
        flash(request, _("The archive file is missing on disk"), "err")
        return back("/admin/archive")
    page = max(page, 1)
    rows, total = archive.read_page(path, page, VIEW_PAGE, q)
    return render(request, "archive_view.html", admin, a=a, fname=path.name, rows=rows, total=total, page=page,
                  pages=max(1, -(-total // VIEW_PAGE)), q=q)


@router.get("/{archive_id}/download")
def download(archive_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    a = _get(db, archive_id)  # the path comes from the registry, never from the request
    path = Path(a.path)
    if not path.is_file():
        raise HTTPException(404, "archive file missing")
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.post("/{archive_id}/verify", dependencies=[Depends(verify_csrf)])
async def verify(request: Request, archive_id: int, admin: User = Depends(current_admin)):
    sm = request.app.state.sessionmaker

    def work():
        with sm() as db:
            a = _get(db, archive_id)
            status = archive.archive_status(a, verify=True)
            record(db, db.get(User, admin.id), "archive.verify", "audit_archive", a.id, status=status)
            db.commit()
            return a.id, Path(a.path).name, status

    _id, name, status = await run_in_threadpool(work)
    flash(request, f"{name}: " + (_("integrity OK (sha256 matches)") if status == "ok" else status.upper()),
          "ok" if status == "ok" else "err")
    return back("/admin/archive")
