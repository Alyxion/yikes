from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4


DEFAULT_EVENT_STORE = Path.home() / ".yikes" / "events"


@dataclass(frozen=True)
class EventRecord:
    session_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    """Append-only JSONL event store keyed by yikes! session ID."""

    def __init__(self, store_dir: Path | None = None) -> None:
        self.store_dir = (store_dir or DEFAULT_EVENT_STORE).expanduser()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def append(self, session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> EventRecord:
        seq = self.next_seq(session_id)
        record = EventRecord(session_id=session_id, type=event_type, payload=payload or {}, seq=seq)
        with self._path(session_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_json()) + "\n")
        return record

    def list(self, session_id: str, *, after_seq: int | None = None) -> list[EventRecord]:
        path = self._path(session_id)
        if not path.exists():
            return []
        records: list[EventRecord] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = _event_from_json(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if after_seq is not None and record.seq <= after_seq:
                continue
            records.append(record)
        return records

    def next_seq(self, session_id: str) -> int:
        records = self.list(session_id)
        if not records:
            return 1
        return records[-1].seq + 1

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _path(self, session_id: str) -> Path:
        return self.store_dir / f"{session_id}.jsonl"


def _event_from_json(data: dict[str, Any]) -> EventRecord:
    return EventRecord(
        session_id=str(data["session_id"]),
        type=str(data["type"]),
        payload=dict(data.get("payload", {})),
        seq=int(data["seq"]),
        ts=float(data["ts"]),
        id=str(data["id"]),
    )
