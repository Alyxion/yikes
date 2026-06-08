"""Self-signed TLS for the web UI so it is served over HTTPS.

Browsers only allow the microphone (and other powerful APIs) on a *secure
context* — HTTPS, or localhost. To make voice input work over the LAN, `yikes
web` serves HTTPS using a self-signed certificate generated here (cached under
~/.yikes). The certificate's SAN covers localhost, 127.0.0.1, and the machine's
LAN IPs so the same cert works from other devices (after a one-time accept).
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path

DEFAULT_YIKES_DIR = Path.home() / ".yikes"
CERT_PATH = DEFAULT_YIKES_DIR / "web-cert.pem"
KEY_PATH = DEFAULT_YIKES_DIR / "web-key.pem"
HOSTS_PATH = DEFAULT_YIKES_DIR / "web-cert.hosts"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _san(hosts: list[str]) -> str:
    return ",".join(f"IP:{h}" if _is_ip(h) else f"DNS:{h}" for h in hosts)


def ensure_cert(hosts: list[str]) -> tuple[Path, Path] | None:
    """Return (cert, key) paths for a self-signed cert covering `hosts`.

    Reuses the cached cert when it already covers the requested host set; otherwise
    regenerates with openssl. Returns None if openssl is unavailable.
    """
    wanted = sorted({"localhost", "127.0.0.1", *(h for h in hosts if h)})
    if CERT_PATH.exists() and KEY_PATH.exists() and HOSTS_PATH.exists():
        try:
            if HOSTS_PATH.read_text(encoding="utf-8").split() == wanted:
                return CERT_PATH, KEY_PATH
        except OSError:
            pass
    if not _generate(wanted):
        return None
    try:
        HOSTS_PATH.write_text("\n".join(wanted) + "\n", encoding="utf-8")
    except OSError:
        pass
    return CERT_PATH, KEY_PATH


def _generate(hosts: list[str]) -> bool:
    openssl = shutil.which("openssl")
    if not openssl:
        return False
    DEFAULT_YIKES_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_PATH), "-out", str(CERT_PATH),
        "-days", "825", "-subj", "/CN=yikes!",
        "-addext", f"subjectAltName={_san(hosts)}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not (CERT_PATH.exists() and KEY_PATH.exists()):
        return False
    for path in (KEY_PATH, CERT_PATH):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return True
