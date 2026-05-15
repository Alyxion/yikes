from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TOKEN_STORE = Path.home() / ".yikes" / "tokens.json"


@dataclass
class TokenRecord:
    token_hash: str
    label: str
    permanent: bool
    created_at: float
    expires_at: float | None
    last_used: float | None = None


class TokenStore:
    """Host-side bearer token store.

    Plaintext tokens are returned only at creation time. Disk state stores
    SHA-256 hashes and metadata.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_TOKEN_STORE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tokens: list[TokenRecord] = []
        self._load()

    def create_temporary(self, label: str = "Temporary", duration_seconds: int = 300) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._tokens.append(
            TokenRecord(
                token_hash=self._hash(token),
                label=label,
                permanent=False,
                created_at=now,
                expires_at=now + duration_seconds,
            )
        )
        self._save()
        return token

    def create_permanent(self, label: str = "Permanent Key") -> str:
        self._tokens = [
            token
            for token in self._tokens
            if not (token.permanent and token.label == label)
        ]
        token = secrets.token_urlsafe(32)
        self._tokens.append(
            TokenRecord(
                token_hash=self._hash(token),
                label=label,
                permanent=True,
                created_at=time.time(),
                expires_at=None,
            )
        )
        self._save()
        return token

    def verify(self, token: str) -> bool:
        token_hash = self._hash(token)
        now = time.time()
        self._tokens = [
            record
            for record in self._tokens
            if record.permanent or (record.expires_at is not None and record.expires_at > now)
        ]
        for record in self._tokens:
            if hmac.compare_digest(record.token_hash, token_hash):
                record.last_used = now
                self._save()
                return True
        self._save()
        return False

    def revoke_all_temporary(self) -> int:
        before = len(self._tokens)
        self._tokens = [record for record in self._tokens if record.permanent]
        self._save()
        return before - len(self._tokens)

    def regenerate_permanent(self, label: str = "Permanent Key") -> str:
        return self.create_permanent(label)

    def list_tokens(self) -> list[dict[str, object]]:
        now = time.time()
        return [
            {
                "label": record.label,
                "permanent": record.permanent,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "expired": (
                    not record.permanent
                    and record.expires_at is not None
                    and record.expires_at < now
                ),
                "last_used": record.last_used,
            }
            for record in self._tokens
        ]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self._tokens = [TokenRecord(**record) for record in data.get("tokens", [])]
        except (json.JSONDecodeError, TypeError):
            self._tokens = []

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "tokens": [
                        {
                            "token_hash": record.token_hash,
                            "label": record.label,
                            "permanent": record.permanent,
                            "created_at": record.created_at,
                            "expires_at": record.expires_at,
                            "last_used": record.last_used,
                        }
                        for record in self._tokens
                    ]
                },
                indent=2,
            )
        )
