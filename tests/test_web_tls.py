from __future__ import annotations

import shutil
import subprocess

import pytest

import yikes.web_tls as web_tls
from yikes.web_auth import WebAuthConfig

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")


def _point_at(tmp_path, monkeypatch):
    monkeypatch.setattr(web_tls, "DEFAULT_YIKES_DIR", tmp_path)
    monkeypatch.setattr(web_tls, "CERT_PATH", tmp_path / "web-cert.pem")
    monkeypatch.setattr(web_tls, "KEY_PATH", tmp_path / "web-key.pem")
    monkeypatch.setattr(web_tls, "HOSTS_PATH", tmp_path / "web-cert.hosts")


def _cert_text(cert):
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"], capture_output=True, text=True
    ).stdout


def test_ensure_cert_generates_with_expected_san(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    result = web_tls.ensure_cert(["192.168.1.50"])
    assert result is not None
    cert, key = result
    assert cert.exists() and key.exists()
    text = _cert_text(cert)
    assert "192.168.1.50" in text and "127.0.0.1" in text and "localhost" in text


def test_ensure_cert_reuses_when_hosts_unchanged(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    cert, _ = web_tls.ensure_cert(["192.168.1.50"])
    stamp = cert.stat().st_mtime_ns
    web_tls.ensure_cert(["192.168.1.50"])           # same set → no regen
    assert cert.stat().st_mtime_ns == stamp


def test_ensure_cert_regenerates_when_hosts_change(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    cert, _ = web_tls.ensure_cert(["192.168.1.50"])
    web_tls.ensure_cert(["10.0.0.5"])               # new host set → regen
    assert "10.0.0.5" in _cert_text(cert)


def test_login_url_scheme():
    auth = WebAuthConfig(secret="s", login_key="k", developer_mode=False)
    assert auth.login_url(host="h", port=8760, scheme="https").startswith("https://h:8760/login?")
    assert auth.login_url(host="h", port=8760).startswith("http://h:8760/login?")
