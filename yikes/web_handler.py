from __future__ import annotations

import asyncio
from typing import Any

from .app_core import YikesAppController
from .terminal_bridge import WebTerminalManager


class WebMessageHandler:
    """Own websocket command dispatch for the browser control surface."""

    def __init__(
        self,
        controller: YikesAppController,
        terminals: WebTerminalManager,
        speaker: Any | None = None,
    ) -> None:
        self.controller = controller
        self.terminals = terminals
        self.speaker = speaker

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
            if msg_type == "train.capture":
                # Capture blocks ~0.5s (rapid snapshots); keep the event loop free.
                data = await asyncio.to_thread(
                    self.controller.capture_training_sample,
                    str(message.get("session_id", "")),
                    str(message.get("label", "")),
                    _optional_text(message.get("notes")),
                )
                return {"type": "train.captured", "data": data}
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
            if msg_type == "term.input":
                ok = self.controller.send_terminal_input(
                    _optional_text(message.get("session_id")),
                    text=_optional_text(message.get("text")),
                    key=_optional_text(message.get("key")),
                )
                return {"type": "term.input.ack", "ok": ok}
            if msg_type == "voice.interpret":
                transcript = str(message.get("transcript", ""))
                if self.speaker is not None:
                    result = await self.speaker.interpret_voice(transcript)
                else:
                    result = {"mode": "dictate", "text": transcript}
                return {"type": "voice.interpret.result", "req_id": message.get("req_id"), **result}
            if msg_type == "speaker.toggle":
                if self.speaker is not None:
                    self.speaker.set_enabled(
                        str(message.get("session_id", "")), bool(message.get("enabled", False))
                    )
                return {"type": "state", "state": self.controller.state()}
            if msg_type == "speaker.config":
                changes = message.get("changes", {})
                if self.speaker is not None and isinstance(changes, dict) and changes:
                    self.speaker.update_config(**changes)
                return {"type": "state", "state": self.controller.state()}
        except Exception as exc:
            state = self.controller.state()
            state["error"] = str(exc)
            return {"type": "state", "state": state}
        return {"type": "error", "message": f"Unknown message type: {msg_type}"}

    async def interpret_voice(self, sink: Any, message: dict[str, Any]) -> None:
        """Run voice intent classification off the receive loop (it calls an LLM).

        Done as a background task so a ~1s model call never blocks other
        messages (state polls, further utterances) on the same connection.
        """
        transcript = str(message.get("transcript", ""))
        if self.speaker is not None:
            result = await self.speaker.interpret_voice(transcript)
        else:
            result = {"mode": "dictate", "text": transcript}
        await sink.send_json({"type": "voice.interpret.result", "req_id": message.get("req_id"), **result})

    async def transcribe_voice(self, sink: Any, message: dict[str, Any]) -> None:
        """Transcribe recorded audio (OpenAI) and route it — off the receive loop."""
        audio = str(message.get("audio", ""))
        mime = str(message.get("mime", "audio/webm"))
        if self.speaker is not None:
            result = await self.speaker.transcribe_and_interpret(audio, mime)
        else:
            result = {"transcript": "", "mode": "dictate", "text": ""}
        await sink.send_json({"type": "voice.utterance.result", "req_id": message.get("req_id"), **result})

    async def stream_submit(self, sink: Any, message: dict[str, Any]) -> None:
        text = str(message.get("text", ""))
        started = self.controller.begin_submit(text)
        await sink.send_json({"type": "state", "state": self.controller.state()})
        if not started:
            return
        task = asyncio.create_task(asyncio.to_thread(self.controller.finish_submit))
        while not task.done():
            await asyncio.sleep(0.4)
            await sink.send_json({"type": "state", "state": self.controller.state()})
        state = await task
        await sink.send_json({"type": "state", "state": state})

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
