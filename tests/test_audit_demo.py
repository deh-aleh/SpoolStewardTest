"""Demo you can look at: prints on the real virtual printer (CUPS test container) with a very long file name and
comment, then saves the rendered /admin/jobs page as tests/_out/audit-demo.html (+ a variant with the popups open).

    pytest -m demo tests/test_audit_demo.py     ->  open tests/_out/audit-demo.html in a browser
"""

import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import JobKind, JobLog, JobState
from tests.conftest import CUPS_TEST_URL, csrf_of_page, make_user, portal_login, requires_cups
from tests.test_audit_ui import GOOD, LONG_COMMENT, LONG_NAME, PDF

OUT = Path(__file__).parent / "_out"
pytestmark = pytest.mark.demo  # skipped by default (addopts); run with: pytest -m demo


@requires_cups
async def test_print_on_the_virtual_printer_and_save_the_audit_page(portal):
    portal.app.state.upstream = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    portal.app.state.queues_cache = None
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    try:
        await portal_login(portal, "ann", GOOD)
        tok = await csrf_of_page(portal, "/me")
        for name, comment in ((LONG_NAME, LONG_COMMENT), ("breve.pdf", ""), ("contratto-fornitura-2026.pdf", "urgente")):
            r = await portal.post("/me/print", data={"queue": "virtual", "copies": "2", "csrf_token": tok, "comment": comment[:50],
                                                       "color": "monochrome"},
                                  files={"file": (name, PDF, "application/pdf")})
            assert r.status_code == 303
        with portal.sm() as s:
            jobs = s.scalars(select(JobLog).order_by(JobLog.id)).all()
            assert [j.state for j in jobs] == [JobState.accepted] * 3 and all(j.cups_job_id for j in jobs)   # really sent to CUPS
            # what a scan from the browser and a denied job look like next to them
            s.add(JobLog(user_name="ann", kind=JobKind.scan, printer="scanner", title="scansione-20261011-0930", pages=3,
                         state=JobState.completed, options_json={"via": "web", "source": "adf", "format": "pdf", "dpi": 300}))
            s.add(JobLog(user_name="ann", kind=JobKind.scan, printer="scanner", title="scansione-20261011-0935", pages=1,
                         state=JobState.completed, options_json={"via": "web", "source": "platen", "format": "pdf", "dpi": 300}))
            s.add(JobLog(user_name="ann", printer="virtual", title="tesi.pdf", state=JobState.denied,
                         denied_reason="weekly page limit would be exceeded (used 48, this document 20, limit 50)"))
            s.add(JobLog(user_name="ann", printer="virtual", title="da-ipp.txt", state=JobState.completed, pages=1,
                         options_json={"color": "monochrome", "sides": "two-sided-long-edge", "document_format": "text/plain"}))
            s.commit()
        portal.app.state.scanner_device_cache = (time.monotonic(), "virtual")
        await portal.post("/logout", data={"csrf_token": tok})
        await portal_login(portal, "boss", GOOD)
        html = (await portal.get("/admin/jobs")).text
    finally:
        await portal.app.state.upstream.aclose()
    OUT.mkdir(exist_ok=True)
    html = html.replace("setTimeout(tick,", "void(0&&tick,")  # a saved file must not reload itself
    (OUT / "audit-demo.html").write_text(html)
    # variant for a screenshot: every popup shown, as if all the "…" and "ⓘ" buttons had been clicked
    (OUT / "audit-demo-popups.html").write_text(html.replace("<dialog>", "<dialog open style=\"position:static;margin:8px 0;box-shadow:0 2px 12px rgba(0,0,0,.25)\">"))
    assert "relazione-trimestrale" in html and html.count("<dialog") >= 4


from tests import test_dynamic  # noqa: E402

two_scanners = test_dynamic.two_scanners   # fixture: two fake scanners, found by pytest under this name


async def test_save_the_other_pages_too(two_scanners):
    """tests/_out/scan-demo.html (two scanners, one A4 with feeder, one A3), key-demo.html (copy buttons), actions-demo.html."""
    OUT.mkdir(exist_ok=True)
    cl = two_scanners
    (OUT / "scan-demo.html").write_text((await cl.get("/me/scan")).text)
    tok = await csrf_of_page(cl, "/me/keys")
    (OUT / "key-demo.html").write_text((await cl.post("/me/keys", data={"csrf_token": tok, "label": "portatile"})).text)
    await cl.post("/logout", data={"csrf_token": tok})
    make_user(cl.sm, "boss", password=GOOD, admin=True)
    make_user(cl.sm, "emma", password=GOOD, admin=True)
    await portal_login(cl, "boss", GOOD)
    (OUT / "actions-demo.html").write_text((await cl.get("/admin/actions")).text)
    (OUT / "print-demo.html").write_text((await cl.get("/me/print")).text)
