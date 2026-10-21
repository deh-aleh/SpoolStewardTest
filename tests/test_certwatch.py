"""Warnings about the TLS certificate: expired, expiring (opt-in), announced address missing, cert-sync.sh failed."""

import ipaddress
import json
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from spoolsteward import certwatch
from spoolsteward.config import settings
from spoolsteward.db.models import Notice
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def make_cert(path, *, days_left=60, dns=("print.example.com",), ips=("192.168.1.50",)):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    san = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(NOW - timedelta(days=100)).not_valid_after(NOW + timedelta(days=days_left))
            .add_extension(x509.SubjectAlternativeName(san), critical=False).sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def test_read_and_cover_names(tmp_path):
    info = certwatch.read_cert(make_cert(tmp_path / "c.pem", dns=("print.example.com", "*.lan")))
    assert certwatch.covers(info, "192.168.1.50") and certwatch.covers(info, "PRINT.example.com")
    assert certwatch.covers(info, "printer.lan") and not certwatch.covers(info, "a.b.lan")   # a wildcard is one label
    assert not certwatch.covers(info, "192.168.1.77") and not certwatch.covers(info, "other.example.com")
    assert certwatch.read_cert(tmp_path / "missing.pem") is None


def test_expired_is_always_reported_expiring_only_when_asked(tmp_path, monkeypatch):
    soon = certwatch.read_cert(make_cert(tmp_path / "a.pem", days_left=5))
    assert certwatch.evaluate(soon, None, None, NOW)["cert_expiring"] is None          # off by default: no false alarm
    monkeypatch.setattr(settings, "cert_expiry_warn_enabled", True)
    monkeypatch.setattr(settings, "cert_expiry_warn_days", 14)
    assert certwatch.evaluate(soon, None, None, NOW)["cert_expiring"]["sync"] == "none"
    far = certwatch.read_cert(make_cert(tmp_path / "b.pem", days_left=40))
    assert certwatch.evaluate(far, None, None, NOW)["cert_expiring"] is None          # renewals come ~30 days before
    ok = certwatch.evaluate(soon, {"ok": True}, None, NOW)["cert_expiring"]
    assert ok["sync"] == "ok"
    assert certwatch.evaluate(soon, {"ok": False, "message": "x"}, None, NOW)["cert_expiring"]["sync"] == "failed"
    old = certwatch.read_cert(make_cert(tmp_path / "c.pem", days_left=-2))
    monkeypatch.setattr(settings, "cert_expiry_warn_enabled", False)
    assert certwatch.evaluate(old, None, None, NOW)["cert_expired"]                    # reported even with the option off


def test_announced_address_must_be_covered_by_something(tmp_path, monkeypatch):
    info = certwatch.read_cert(make_cert(tmp_path / "a.pem", dns=("print.example.com",), ips=("192.168.1.50",)))
    here = {"names": ["print.example.com", "print.local"], "addresses": ["192.168.1.50"]}
    assert certwatch.evaluate(info, None, here, NOW)["cert_name_missing"] is None
    moved = {"names": ["printbox", "printbox.local"], "addresses": ["192.168.1.77"]}   # the router gave a new address
    assert certwatch.evaluate(info, None, moved, NOW)["cert_name_missing"]["addresses"] == ["192.168.1.77"]
    monkeypatch.setattr(settings, "cert_name_check", False)
    assert certwatch.evaluate(info, None, moved, NOW)["cert_name_missing"] is None


def test_sync_failure_comes_from_the_status_file(tmp_path):
    cert = make_cert(tmp_path / "fullchain.pem")
    assert certwatch.sync_status(cert) is None
    (tmp_path / "sync-status.json").write_text(json.dumps({"ts": "2026-10-14T03:00:00+00:00", "ok": False, "message": "ssh: timeout"}))
    out = certwatch.evaluate(certwatch.read_cert(cert), certwatch.sync_status(cert), None, NOW)
    assert out["cert_sync_failed"]["message"] == "ssh: timeout"


def test_parse_announce_rejects_garbage():
    assert certwatch.parse_announce('{"names": ["a.local"], "addresses": ["10.0.0.2"]}') == {"names": ["a.local"], "addresses": ["10.0.0.2"]}
    assert certwatch.parse_announce("nope") is None and certwatch.parse_announce('{"names": [], "addresses": ["x"]}') is None


async def test_notices_appear_in_the_portal_and_close_by_themselves(portal, tmp_path, monkeypatch):
    cert = make_cert(tmp_path / "fullchain.pem", days_left=-3)
    monkeypatch.setattr(settings, "tls_certfile", str(cert))
    portal.app.state.mdns_announce = {"names": ["box.local"], "addresses": ["10.9.9.9"]}
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    certwatch.check_once(portal.app, NOW)
    with portal.sm() as s:
        assert {n.kind for n in s.query(Notice).filter(Notice.dismissed_at.is_(None))} == {"cert_expired", "cert_name_missing"}
    await portal_login(portal, "ada", GOOD)
    page = (await portal.get("/admin/notices")).text
    assert "has expired" in page and "10.9.9.9" in page

    make_cert(cert, days_left=80, ips=("10.9.9.9",))                    # cert-sync.sh put a renewed one
    certwatch.check_once(portal.app, NOW)
    with portal.sm() as s:
        assert s.query(Notice).filter(Notice.dismissed_at.is_(None)).count() == 0
