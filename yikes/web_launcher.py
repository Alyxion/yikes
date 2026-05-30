from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from .web_auth import WebAuthConfig


@dataclass(frozen=True)
class WebLaunchResult:
    url: str
    started: bool
    message: str


def launch_web_ui(
    *,
    host: str = "0.0.0.0",
    port: int = 8760,
    cwd: Path | None = None,
    developer_mode: bool = False,
    persistent_auth: bool = True,
    open_browser: bool = True,
) -> WebLaunchResult:
    root = (cwd or Path.cwd()).expanduser()
    auth = WebAuthConfig.load(developer_mode=developer_mode, persist_auth=persistent_auth)
    started = False
    if not _port_open(host, port):
        env = os.environ.copy()
        env["YIKES_WEB_DEV"] = "1" if developer_mode else "0"
        log = Path(os.environ.get("YIKES_WEB_LOG", "/tmp/yikes-web.log"))
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "yikes.web_server",
                "--host",
                host,
                "--port",
                str(port),
                "--cwd",
                str(root),
                "--dev" if developer_mode else "--no-dev",
                "--persistent-auth" if persistent_auth else "--no-persistent-auth",
            ],
            cwd=str(root),
            env=env,
            stdout=log.open("a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = True
        _wait_for_port(host, port, timeout=5.0)
    url = auth.login_url(host=host, port=port)
    if open_browser:
        webbrowser.open(url)
    state = "started" if started else "opened"
    return WebLaunchResult(url=url, started=started, message=f"Web UI {state}: {url}")


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _wait_for_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.1)
    return False
