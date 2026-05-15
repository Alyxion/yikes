from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .app_core import YikesAppController


@dataclass
class YikesLlmingBridge:
    """Small adapter for hosting yikes! inside a llming-com session.

    The bridge intentionally exposes the same command names and payload shapes
    as the standalone web UI. A llming-stage view can call these handlers over
    llming-com's `SessionRouter`; the business object remains the shared
    `YikesAppController`.
    """

    controller: YikesAppController

    def state(self) -> dict[str, Any]:
        return self.controller.state()

    def submit(self, text: str) -> dict[str, Any]:
        return self.controller.submit(text)

    def suggestions(self, text: str) -> list[dict[str, str]]:
        return self.controller.suggestions(text)

    def open_new(self) -> dict[str, Any]:
        return self.controller.open_new_session()

    def update_new(self, **changes: object) -> dict[str, Any]:
        return self.controller.update_new_session(**changes)

    def confirm_new(self) -> dict[str, Any]:
        return self.controller.confirm_new_session()

    def switch_session(self, session_id: str) -> dict[str, Any]:
        return self.controller.switch_session(session_id)

    def close_all(self) -> dict[str, Any]:
        return self.controller.close_all()


def build_session_router(prefix: str = "yikes"):
    """Build a llming-com SessionRouter for yikes!, when llming-com is present."""

    try:
        from llming_com import LlmingSessionData, SessionRouter
    except ModuleNotFoundError as exc:  # pragma: no cover - optional integration
        raise RuntimeError("llming-com is not installed in this environment.") from exc

    @dataclass
    class YikesSessionData(LlmingSessionData):  # type: ignore[misc,valid-type]
        bridge: YikesLlmingBridge | None = None

        @classmethod
        async def create(cls, session, context=None):  # type: ignore[no-untyped-def]
            return cls(bridge=YikesLlmingBridge(YikesAppController()))

    router = SessionRouter(prefix=prefix)

    async def _bridge(session) -> YikesLlmingBridge:  # type: ignore[no-untyped-def]
        data = await YikesSessionData.current(session=session)
        if data is None or data.bridge is None:
            return YikesLlmingBridge(YikesAppController())
        return data.bridge

    @router.handler("state")
    async def state(session) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).state()

    @router.handler("submit")
    async def submit(session, text: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).submit(text)

    @router.handler("suggest")
    async def suggest(session, text: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {"items": (await _bridge(session)).suggestions(text)}

    @router.handler("new_open")
    async def new_open(session) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).open_new()

    @router.handler("new_update")
    async def new_update(session, changes: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).update_new(**changes)

    @router.handler("new_confirm")
    async def new_confirm(session) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).confirm_new()

    @router.handler("switch")
    async def switch(session, session_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).switch_session(session_id)

    @router.handler("close_all")
    async def close_all(session) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return (await _bridge(session)).close_all()

    return router
