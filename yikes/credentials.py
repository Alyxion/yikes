from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .runtime import CredentialGrant


class CredentialUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialValue:
    name: str
    value: str = field(repr=False)
    source: str = ""


class CredentialProvider(Protocol):
    source: str

    def get(self, name: str) -> CredentialValue | None: ...


class StaticCredentialProvider:
    source = "static"

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> CredentialValue | None:
        value = self._values.get(name)
        if value is None:
            return None
        return CredentialValue(name, value, self.source)


class EnvCredentialProvider:
    source = "env"

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}

    def get(self, name: str) -> CredentialValue | None:
        env_name = self._mapping.get(name, name.upper())
        value = os.environ.get(env_name)
        if not value:
            return None
        return CredentialValue(name, value, self.source)


class CallbackCredentialProvider:
    def __init__(self, source: str, callback: Callable[[str], str | None]) -> None:
        self.source = source
        self._callback = callback

    def get(self, name: str) -> CredentialValue | None:
        value = self._callback(name)
        if not value:
            return None
        return CredentialValue(name, value, self.source)


class ClaudeCredentialProvider:
    """Resolve Claude credentials from env or Claude Code's OS credential store."""

    source = "claude"

    def get(self, name: str) -> CredentialValue | None:
        if name not in {"anthropic", "claude", "claude_api"}:
            return None
        value = os.environ.get("ANTHROPIC_API_KEY")
        if value:
            return CredentialValue(name, value, "env")
        try:
            creds = _read_claude_credential_store()
        except Exception:
            return None
        token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return None
        return CredentialValue(name, str(token), self.source)


class CodexCredentialProvider:
    """Resolve Codex auth from env or Codex CLI's auth file."""

    source = "codex"

    def __init__(self, auth_path: Path | None = None) -> None:
        self.auth_path = auth_path or Path.home() / ".codex" / "auth.json"

    def get(self, name: str) -> CredentialValue | None:
        if name not in {"codex", "openai", "codex_auth_json"}:
            return None
        value = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
        if value and name in {"openai"}:
            return CredentialValue(name, value, "env")
        try:
            text = self.auth_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.strip():
            return None
        return CredentialValue("codex_auth_json", text, self.source)


class CredentialBroker:
    """Resolve explicit credential grants without persisting secret values."""

    def __init__(self, providers: list[CredentialProvider] | None = None) -> None:
        self._providers: dict[str, CredentialProvider] = {}
        for provider in providers or (EnvCredentialProvider(), ClaudeCredentialProvider(), CodexCredentialProvider()):
            self.register(provider)

    def register(self, provider: CredentialProvider) -> None:
        self._providers[provider.source] = provider

    def resolve(self, grant: CredentialGrant) -> CredentialValue:
        provider = self._providers.get(grant.source)
        if provider is None:
            raise CredentialUnavailable(f"No credential provider registered for source {grant.source!r}")
        value = provider.get(grant.name)
        if value is None:
            raise CredentialUnavailable(f"Credential {grant.name!r} is unavailable from {grant.source!r}")
        return value

    def resolve_all(self, grants: tuple[CredentialGrant, ...]) -> dict[str, CredentialValue]:
        return {grant.name: self.resolve(grant) for grant in grants}

    def build_secret_env(
        self,
        grants: tuple[CredentialGrant, ...],
        *,
        env_names: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        for grant in grants:
            value = self.resolve(grant)
            env_name = (env_names or {}).get(grant.name, grant.name.upper())
            env[env_name] = value.value
        return env


def _read_claude_credential_store() -> dict:
    system = platform.system()
    if system == "Darwin":
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return json.loads(raw)
    if system == "Linux":
        raw = subprocess.check_output(
            ["secret-tool", "lookup", "service", "Claude Code-credentials"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return json.loads(raw)
    if system == "Windows":
        ps_script = (
            "[System.Runtime.InteropServices.Marshal]::"
            "PtrToStringAuto([System.Runtime.InteropServices.Marshal]::"
            "SecureStringToBSTR((Get-StoredCredential -Target "
            '"Claude Code-credentials").Password))'
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return json.loads(raw)
    raise CredentialUnavailable(f"Unsupported platform for Claude credential store: {system}")


def write_api_key_helper_settings(workspace: Path, credential: CredentialValue) -> tuple[Path, Path]:
    """Write Claude apiKeyHelper files into a workspace.

    This is a compatibility path for current Claude Code. The preferred future
    runtime shape is in-memory process injection, but this helper keeps the
    file-writing explicit and contained.
    """

    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    key_path = claude_dir / "api_key"
    settings_path = claude_dir / "settings.json"
    key_path.write_text(credential.value)
    settings_path.write_text(json.dumps({"apiKeyHelper": f"cat {key_path}"}, indent=2))
    return key_path, settings_path
