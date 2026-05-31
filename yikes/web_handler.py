from __future__ import annotations

import asyncio
from typing import Any

from .app_core import YikesAppController
from .terminal_bridge import WebTerminalManager


class WebMessageHandler:
    """Own websocket command dispatch for the browser control surface."""

    def __init__(self, controller: YikesAppController, terminals: WebTerminalManager) -> None:
        self.controller = controller
        self.terminals = terminals

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_type = str(message.get("type", "state"))
        try:
            if msg_type == "state":
                return {"type": "state", "state": self.controller.state()}
            if msg_type == "submit":
                text = str(message.get("text", ""))
                state = await asyncio.to_thread(self.controller.submit, text)
                return {"type": "state", "state": state}
            if msg_type == "suggest":
                text = str(message.get("text", ""))
                return {"type": "suggestions", "items": self.controller.suggestions(text)}
            if msg_type == "new.open":
                return {"type": "state", "state": self.controller.open_new_session()}
            if msg_type == "new.update":
                changes = message.get("changes", {})
                if not isinstance(changes, dict):
                    changes = {}
                return {"type": "state", "state": self.controller.update_new_session(**changes)}
            if msg_type == "new.confirm":
                changes = message.get("changes", {})
                if isinstance(changes, dict) and changes:
                    self.controller.update_new_session(**changes)
                state = await asyncio.to_thread(self.controller.confirm_new_session)
                return {"type": "state", "state": state}
            if msg_type == "new.cancel":
                return {"type": "state", "state": self.controller.cancel_new_session()}
            if msg_type == "config.update":
                changes = message.get("changes", {})
                if not isinstance(changes, dict):
                    changes = {}
                return {"type": "state", "state": self.controller.update_active_config(**changes)}
            if msg_type == "session.switch":
                return {"type": "state", "state": self.controller.switch_session(str(message.get("session_id", "")))}
            if msg_type == "session.close":
                return {"type": "state", "state": self.controller.close_session(str(message.get("session_id", "")))}
            if msg_type == "session.close_all":
                return {"type": "state", "state": self.controller.close_all()}
            if msg_type == "dir.list":
                return {"type": "dir.entries", "data": self.controller.directory_entries(_optional_text(message.get("root")))}
            if msg_type == "pane.add":
                return {
                    "type": "state",
                    "state": self.controller.add_pane(
                        str(message.get("session_id", "")),
                        str(message.get("value", "")),
                        title=_optional_text(message.get("title")),
                    ),
                }
            if msg_type == "process.start":
                return {
                    "type": "state",
                    "state": self.controller.start_pane_process(
                        str(message.get("session_id", "")), str(message.get("pane_id", ""))
                    ),
                }
            if msg_type == "process.stop":
                return {
                    "type": "state",
                    "state": self.controller.stop_pane_process(
                        str(message.get("session_id", "")), str(message.get("pane_id", ""))
                    ),
                }
            if msg_type == "term.open":
                return self._open_terminal(message)
            if msg_type == "term.resize":
                data = self.controller.resize_terminal(
                    _optional_text(message.get("session_id")),
                    cols=_int_or_default(message.get("cols"), 120),
                    rows=_int_or_default(message.get("rows"), 34),
                )
                return {"type": "term.resized", "data": data}
            if msg_type == "term.close":
                self.terminals.close(str(message.get("terminal_id", "")))
                return {"type": "state", "state": self.controller.state()}
        except Exception as exc:
            state = self.controller.state()
            state["error"] = str(exc)
            return {"type": "state", "state": state}
        return {"type": "error", "message": f"Unknown message type: {msg_type}"}

    async def stream_submit(self, websocket: Any, message: dict[str, Any]) -> None:
        text = str(message.get("text", ""))
        started = self.controller.begin_submit(text)
        await websocket.send_json({"type": "state", "state": self.controller.state()})
        if not started:
            return
        task = asyncio.create_task(asyncio.to_thread(self.controller.finish_submit))
        while not task.done():
            await asyncio.sleep(0.4)
            await websocket.send_json({"type": "state", "state": self.controller.state()})
        state = await task
        await websocket.send_json({"type": "state", "state": state})

    def _open_terminal(self, message: dict[str, Any]) -> dict[str, Any]:
        attached = self.controller.attach_command(_optional_text(message.get("session_id")))
        if attached is None:
            return {"type": "error", "message": "No attachable tmux session is selected."}
        session_id, command = attached
        cols = _int_or_default(message.get("cols"), 120)
        rows = _int_or_default(message.get("rows"), 34)
        self.controller.resize_terminal(session_id, cols=cols, rows=rows)
        terminal = self.terminals.spawn(
            session_id=session_id,
            command=command,
            cols=cols,
            rows=rows,
        )
        return {
            "type": "term.opened",
            "terminal_id": terminal.terminal_id,
            "session_id": session_id,
            "title": terminal.title,
        }


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
