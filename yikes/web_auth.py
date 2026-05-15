from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode


COOKIE_NAME = "yikes_web_auth"
DEFAULT_COOKIE_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class WebAuthConfig:
    secret: str
    login_key: str
    developer_mode: bool
    cookie_name: str = COOKIE_NAME
    cookie_ttl_seconds: int = DEFAULT_COOKIE_TTL_SECONDS

    @classmethod
    def load(
        cls,
        *,
        developer_mode: bool,
        env_path: Path | None = None,
    ) -> "WebAuthConfig":
        if developer_mode:
            path = _env_path(env_path)
            values = _read_env(path)
            changed = False
            secret = values.get("YIKES_WEB_SECRET", "")
            login_key = values.get("YIKES_WEB_LOGIN_KEY", "")
            if not secret:
                secret = secrets.token_urlsafe(48)
                values["YIKES_WEB_SECRET"] = secret
                changed = True
            if not login_key:
                login_key = secrets.token_urlsafe(32)
                values["YIKES_WEB_LOGIN_KEY"] = login_key
                changed = True
            if values.get("YIKES_WEB_DEV") != "1":
                values["YIKES_WEB_DEV"] = "1"
                changed = True
            if changed:
                _write_env(path, values)
            return cls(secret=secret, login_key=login_key, developer_mode=True)

        return cls(
            secret=secrets.token_urlsafe(48),
            login_key=secrets.token_urlsafe(32),
            developer_mode=False,
        )

    def login_url(self, *, host: str, port: int, next_path: str = "/") -> str:
        query = urlencode({"key": self.login_key, "next": next_path})
        return f"http://{host}:{port}/login?{query}"

    def verify_login_key(self, key: str | None) -> bool:
        if not key:
            return False
        return secrets.compare_digest(key, self.login_key)

    def issue_cookie(self) -> str:
        expires = int(time.time()) + self.cookie_ttl_seconds
        nonce = secrets.token_urlsafe(16)
        payload = f"{expires}.{nonce}"
        sig = _sign(self.secret, payload)
        return f"{payload}.{sig}"

    def verify_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        parts = value.split(".")
        if len(parts) != 3:
            return False
        expires_text, nonce, sig = parts
        payload = f"{expires_text}.{nonce}"
        if not secrets.compare_digest(sig, _sign(self.secret, payload)):
            return False
        try:
            expires = int(expires_text)
        except ValueError:
            return False
        return expires >= int(time.time())


def developer_mode_from_env(default: bool = True) -> bool:
    value = os.environ.get("YIKES_WEB_DEV")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "dev"}


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _env_path(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser()
    override = os.environ.get("YIKES_WEB_ENV")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / ".env"


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# yikes! local web authentication",
        "# This file is intentionally ignored by git.",
        *[f"{key}={value}" for key, value in sorted(values.items())],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
