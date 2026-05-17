from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import AgentSettings, Backend, ImageAttachment


@dataclass(frozen=True)
class DriverRequest:
    backend: Backend
    prompt: str
    cwd: Path
    cwd_explicit: bool
    timeout: float
    model: str | None
    settings: AgentSettings
    session_id: str | None = None
    attachments: tuple[ImageAttachment, ...] = ()


class AgentDriver(Protocol):
    """Executable driver for a backend/runtime combination."""

    def ask(self, request: DriverRequest) -> str: ...
