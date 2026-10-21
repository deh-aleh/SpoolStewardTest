import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from app.admin.accounts import router as accounts_router
from app.admin.audit_archive import router as archive_router
from app.admin.backups import router as backups_router
from app.admin.notices import router as notices_router
from app.admin.updates import router as update_router
from app.admin.controls import router as controls_router
from app.admin.docs import router as docs_router
from app.admin.me import router as me_router
from app.admin.me_notices import router as me_notices_router
from app.admin.printing import router as print_router
from app.admin.reports import router as reports_router
from app.admin.requests import router as requests_router
from app.admin.routes import auth_router, router as admin_router
from app.audit.archive import repair, run_archiver
from app.auth.bootstrap import ensure_orchestrator
from app.auth.cache import CachedAuth
from app.auth.radius import RadiusProvider
from app.auth.groups import ensure_system_groups
from app.backup.core import app_version
from app.backup.service import run_scheduler as run_backups
from app.certwatch import run_checker as run_cert_check
from app.keywatch import run_keys
from app import secretcheck
from app.printerwatch import run_printerwatch
from app.titles import run_titles
from app.updates import run_checker as run_update_check
from app.auth.throttle import Throttle
from app.config import settings
from app.i18n import LanguageMiddleware
from app.secheaders import SecurityHeaders
from app.db.session import SessionLocal, init_db
from app.admin.printers import router as printers_router
from app.policy import discovery
from app.policy.keepalive import run_scheduler as run_keepalive
from app.escl import make_client as make_scan_client, me_router as scan_me_router, router as escl_router
from app.ipp.gateway import router as ipp_router
from app.policy.reconcile import run_forever


def make_upstream() -> httpx.AsyncClient:
    """CUPS client. unix:///run/cups/cups.sock keeps CUPS off the network and off other local users."""
    timeout = httpx.Timeout(30.0, read=None)
    if settings.cups_url.startswith("unix://"):
        transport = httpx.AsyncHTTPTransport(uds=settings.cups_url.removeprefix("unix://"))
        return httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=timeout)
    return httpx.AsyncClient(base_url=settings.cups_url, timeout=timeout)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.sessionmaker = SessionLocal
    # RADIUS is optional: without it only local accounts exist
    app.state.auth = CachedAuth(
        RadiusProvider(settings.radius_host, settings.radius_port, settings.radius_secret, settings.radius_timeout),
        settings.auth_cache_seconds,
    ) if settings.radius_enabled else None
    app.state.throttle = Throttle(limit=5, window=300, path=settings.lockout_file or None)
    app.state.upstream = make_upstream()
    app.state.scan = make_scan_client()
    app.state.setup_code = await run_in_threadpool(ensure_orchestrator, SessionLocal)
    await run_in_threadpool(ensure_system_groups, SessionLocal)
    await run_in_threadpool(secretcheck.run, SessionLocal)
    try:
        await run_in_threadpool(repair, SessionLocal, Path(settings.audit_archive_dir))
    except OSError:
        logging.getLogger(__name__).exception("audit archive folder not usable: %s", settings.audit_archive_dir)
    tasks = [asyncio.create_task(run_forever(app, settings.reconcile_interval)),
             asyncio.create_task(run_archiver(app)),
             asyncio.create_task(run_backups(app)),
             asyncio.create_task(run_keepalive(app)),
             asyncio.create_task(run_update_check(app)),
             asyncio.create_task(run_titles(app)),
             asyncio.create_task(run_keys(app)),
             asyncio.create_task(run_cert_check(app)),
             asyncio.create_task(run_printerwatch(app)),
             asyncio.create_task(discovery.run_scheduler(app))]
    yield
    for t in tasks:
        t.cancel()
    app.state.throttle.save(force=True)
    await app.state.upstream.aclose()
    if app.state.scan is not None:
        await app.state.scan.aclose()


app = FastAPI(title="SpoolSteward", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

_secret = settings.secret_key
if not _secret:
    _secret = secrets.token_urlsafe(48)
    logging.getLogger(__name__).warning("SPOOLSTEWARD_SECRET_KEY not set: admin sessions reset on every restart")
app.add_middleware(SecurityHeaders)  # added first = innermost: the policy goes on the page the routes made
app.add_middleware(LanguageMiddleware)  # inside the session middleware, which fills scope["session"]
app.add_middleware(
    SessionMiddleware, secret_key=_secret, max_age=8 * 3600, same_site="strict",
    https_only=not settings.allow_insecure_auth, session_cookie="spoolsteward_session",
)

@app.get("/", include_in_schema=False)
async def root():
    """Someone opened the server address in a browser: send them to the admin portal."""
    return RedirectResponse("/admin/", status_code=307)


@app.get("/admin", include_in_schema=False)
async def admin_no_slash():
    # without this the POST-only IPP catch-all makes Starlette answer 405 instead of redirecting
    return RedirectResponse("/admin/", status_code=307)


@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request):
    """200 when the DB and CUPS answer. No error text. Version and printer counts only if SPOOLSTEWARD_HEALTHZ_INFO lists them."""
    def db_ok() -> bool:
        with request.app.state.sessionmaker() as s:
            return s.execute(text("select 1")).scalar() == 1

    try:
        ok = await run_in_threadpool(db_ok)
        ok = ok and (await request.app.state.upstream.get("/", headers={"Host": "localhost"})).status_code < 500
    except Exception:
        ok = False
    body: dict = {"status": "ok" if ok else "degraded"}
    wanted = {w.strip().lower() for w in settings.healthz_info.split(",")}   # what the administrator chose to publish
    if "version" in wanted:
        body["version"] = app_version()
    if "printers" in wanted and ok:
        body["printers"] = await _printer_counts(request.app)
    return JSONResponse(body, status_code=200 if ok else 503)


async def _printer_counts(app) -> dict:
    """Queues by how they are connected: USB (ipp-usb bridge or CUPS' usb backend), network, virtual (writes PDF files)."""
    from app.cups_client import list_queues
    from app.policy.discovery import transport

    try:
        queues = await list_queues(app, probe=False)
    except Exception:
        return {}
    usb = sum(1 for q in queues if not q["virtual"] and transport(q["device_uri"]) is not None)
    virtual = sum(1 for q in queues if q["virtual"])
    return {"usb": usb, "network": len(queues) - usb - virtual, "virtual": virtual, "total": len(queues)}


# the icon and favicon are public: the login page needs them before anybody is logged in
app.mount("/admin/static", StaticFiles(directory=Path(__file__).parent / "admin" / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse("/admin/static/favicon.ico", status_code=301)


# IPP router is a catch-all POST; admin routers must be included before it.
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(accounts_router)
app.include_router(printers_router)
app.include_router(requests_router)
app.include_router(reports_router)
app.include_router(docs_router)
app.include_router(archive_router)
app.include_router(backups_router)
app.include_router(update_router)
app.include_router(notices_router)
app.include_router(controls_router)
app.include_router(me_notices_router)
app.include_router(me_router)
app.include_router(scan_me_router)
app.include_router(escl_router)
app.include_router(print_router)
app.include_router(ipp_router)
