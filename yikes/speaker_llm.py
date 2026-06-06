"""LLM + text-to-speech backend for speaker mode.

Speaker mode needs three cheap, well-bounded model interactions:

1. A *fast gate* — a tiny model (Claude Haiku / OpenAI mini) that looks at the
   latest terminal changes and decides whether anything is worth saying out
   loud, drafts a concise spoken line, and flags whether a stronger model
   should reword it.
2. An optional *elaboration* — only when the gate asks for it — where a more
   capable model rewrites the line.
3. *Speech synthesis* — OpenAI's audio API, used only when an OpenAI key is
   present and the configured engine wants it; otherwise the browser speaks the
   text itself for free.

Every call here is defensive: network and parsing failures are turned into a
result object carrying an ``error`` string, never an exception, so a flaky API
can never crash the per-session watcher loop or trigger a retry storm.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .credentials import ClaudeCredentialProvider

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"

# Anthropic OAuth tokens (Claude Code login, no raw API key) need this beta
# header to be accepted by the Messages API.
_OAUTH_BETA = "oauth-2025-04-20"


@dataclass(frozen=True)
class ResolvedKey:
    provider: str  # "anthropic" | "openai"
    api_key: str
    # "x-api-key" (Anthropic API key), "bearer" (OpenAI), "bearer-oauth"
    # (Claude Code OAuth token).
    auth_scheme: str
    source: str = ""


@dataclass(frozen=True)
class SpeakDecision:
    speak: bool
    utterance: str
    needs_complex: bool = False
    is_question: bool = False
    error: str | None = None


@dataclass(frozen=True)
class TextResult:
    text: str
    error: str | None = None


@dataclass(frozen=True)
class AudioResult:
    b64: str
    mime: str
    error: str | None = None


@dataclass(frozen=True)
class VoiceAction:
    """How a spoken utterance should drive the agent."""

    mode: str               # "dictate" | "command"
    text: str = ""          # cleaned text to type (dictate)
    action: str = ""        # "accept" | "select" | "escape" (command)
    value: int = 0          # option number for select
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "text": self.text, "action": self.action, "value": self.value}


def _read_env_value(path: Path, key: str) -> str | None:
    """Read a single KEY=value from a .env-style file (commented lines ignored)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'") or None
    return None


def _anthropic_key() -> ResolvedKey | None:
    try:
        cred = ClaudeCredentialProvider().get("claude")
    except Exception:
        cred = None
    if cred and cred.value:
        scheme = "x-api-key" if cred.source == "env" else "bearer-oauth"
        return ResolvedKey("anthropic", cred.value, scheme, source=cred.source)
    return None


def _openai_key(cwd: Path | None) -> ResolvedKey | None:
    value = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    if value:
        return ResolvedKey("openai", value, "bearer", source="env")
    # The web server does not load the project's .env into the environment, so
    # look it up explicitly next to the working directory.
    if cwd is not None:
        from_env = _read_env_value(Path(cwd).expanduser() / ".env", "OPENAI_API_KEY")
        if from_env:
            return ResolvedKey("openai", from_env, "bearer", source=".env")
    try:
        data = json.loads((Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8"))
        candidate = data.get("OPENAI_API_KEY")
        if isinstance(candidate, str) and candidate.strip():
            return ResolvedKey("openai", candidate.strip(), "bearer", source="codex")
    except Exception:
        pass
    return None


def resolve_provider_keys(cwd: Path | None = None) -> dict[str, ResolvedKey]:
    """Resolve whichever of {anthropic, openai} keys are available right now."""
    keys: dict[str, ResolvedKey] = {}
    anthropic = _anthropic_key()
    if anthropic is not None:
        keys["anthropic"] = anthropic
    openai = _openai_key(cwd)
    if openai is not None:
        keys["openai"] = openai
    return keys


_GATE_SYSTEM = (
    "You narrate a coding agent's terminal to a user who is NOT looking at the "
    "screen. You will be given the most recent changes in that terminal. Decide "
    "whether anything is worth saying out loud right now.\n"
    "Speak ONLY for things that matter to the user: the agent finished a task, "
    "it is asking a question or waiting for a decision/approval, it hit an error, "
    "or it reached a meaningful result. Do NOT speak for routine progress, "
    "spinners, partial output, or noise. Do NOT speak for empty pleasantries or "
    "idle small talk like 'let me know what's next' — if there is no real new "
    "information or decision needed, set speak=false.\n"
    "When you do speak, write a complete, natural spoken update: usually one "
    "sentence, but use up to {max_words} words if that is what it takes to be "
    "clear and coherent. Never emit a clipped or truncated fragment — finish the "
    "thought. Summarize the substance (what happened, what is needed) rather than "
    "quoting the screen. If the agent is asking the user something, phrase it as "
    "that question. Never mention terminals, screens, or this tool.\n"
    "Set needs_complex=true only when the situation is subtle enough that a "
    "stronger model should reword it.\n"
    'Respond with ONLY a JSON object: {{"speak": bool, "utterance": string, '
    '"is_question": bool, "needs_complex": bool}}.'
)

_INTERPRET_SYSTEM = (
    "You convert a user's spoken words into ONE action for controlling a terminal "
    "coding agent (Claude Code / Codex).\n"
    "The user is EITHER dictating text to type into the agent's prompt, OR giving a "
    "control command: accept/confirm the current prompt, select a numbered menu "
    "option, or cancel/escape.\n"
    'Respond with ONLY JSON: {"mode":"dictate"|"command", "text": string, '
    '"action": "accept"|"select"|"escape"|"", "value": number}.\n'
    "For dictate, put the cleaned spoken text in `text` (action empty, value 0). For "
    "a command, set action (and value = the option number for select), text empty.\n"
    "Examples: 'accept that' → command/accept; 'go with option two' → command/select "
    "value 2; 'never mind, cancel' → command/escape; 'add a docstring to the parser' "
    "→ dictate with that text."
)

_ELABORATE_SYSTEM = (
    "You refine a spoken update for a user who cannot see a coding agent's "
    "screen. Keep it natural and faithful to the draft, a complete thought (never "
    "clipped), using up to {max_words} words only if needed for clarity. If it is "
    "a question, keep it a clear question. Reply with ONLY the sentence(s), no "
    "quotes, no preamble."
)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


class SpeakerLLM:
    """Resolve credentials and run the gate / elaboration / TTS calls."""

    def __init__(self, cwd: Path | None = None, *, timeout: float = 20.0) -> None:
        self._cwd = cwd
        self._timeout = timeout
        self._keys = resolve_provider_keys(cwd)

    def refresh_keys(self) -> None:
        self._keys = resolve_provider_keys(self._cwd)

    def available_providers(self) -> dict[str, bool]:
        return {
            "anthropic": "anthropic" in self._keys,
            "openai": "openai" in self._keys,
        }

    def has_any_provider(self) -> bool:
        return bool(self._keys)

    def has_openai(self) -> bool:
        return "openai" in self._keys

    def _pick_provider(self, preference: str) -> str | None:
        if preference in self._keys:
            return preference
        if preference != "auto":
            return None  # explicit choice but key missing
        # auto: OpenAI first (it is also the only TTS provider), else Claude.
        for name in ("openai", "anthropic"):
            if name in self._keys:
                return name
        return None

    # -- the three model interactions ------------------------------------

    async def decide(self, context: str, *, awaiting: bool, config: Any) -> SpeakDecision:
        provider = self._pick_provider(config.llm_provider)
        if provider is None:
            return SpeakDecision(False, "", error="No usable LLM key for speaker mode.")
        system = _GATE_SYSTEM.format(max_words=config.max_words)
        user = self._gate_user_prompt(context, awaiting=awaiting)
        if provider == "openai":
            raw = await self._openai_chat(
                config.fast_model_openai, system, user, max_tokens=220, json_mode=True
            )
        else:
            raw = await self._anthropic_message(
                config.fast_model_anthropic, system, user, max_tokens=220
            )
        if raw.error:
            return SpeakDecision(False, "", error=raw.error)
        data = _extract_json(raw.text)
        if not isinstance(data, dict):
            return SpeakDecision(False, "", error="gate returned non-JSON")
        return SpeakDecision(
            speak=bool(data.get("speak", False)),
            utterance=str(data.get("utterance", "")).strip(),
            needs_complex=bool(data.get("needs_complex", False)),
            is_question=bool(data.get("is_question", False)) or awaiting,
        )

    async def interpret(self, transcript: str, *, config: Any) -> VoiceAction:
        provider = self._pick_provider(config.llm_provider)
        if provider is None:
            return VoiceAction("dictate", text=transcript, error="No usable LLM key for voice input.")
        if provider == "openai":
            raw = await self._openai_chat(
                config.fast_model_openai, _INTERPRET_SYSTEM, transcript, max_tokens=160, json_mode=True
            )
        else:
            raw = await self._anthropic_message(
                config.fast_model_anthropic, _INTERPRET_SYSTEM, transcript, max_tokens=160
            )
        if raw.error:
            return VoiceAction("dictate", text=transcript, error=raw.error)
        data = _extract_json(raw.text)
        if not isinstance(data, dict):
            return VoiceAction("dictate", text=transcript, error="interpret returned non-JSON")
        if str(data.get("mode")) == "command":
            action = str(data.get("action") or "")
            if action not in {"accept", "select", "escape"}:
                return VoiceAction("dictate", text=transcript)
            try:
                value = int(data.get("value") or 0)
            except (TypeError, ValueError):
                value = 0
            if action == "select" and not (1 <= value <= 9):
                return VoiceAction("dictate", text=transcript)
            return VoiceAction("command", action=action, value=value)
        return VoiceAction("dictate", text=str(data.get("text") or transcript).strip())

    async def elaborate(self, context: str, draft: str, *, config: Any) -> TextResult:
        provider = self._pick_provider(config.llm_provider)
        if provider is None:
            return TextResult(draft)
        system = _ELABORATE_SYSTEM.format(max_words=config.max_words)
        user = f"Recent changes:\n{context}\n\nDraft sentence:\n{draft}"
        if provider == "openai":
            return await self._openai_chat(
                config.complex_model_openai, system, user, max_tokens=120, json_mode=False
            )
        return await self._anthropic_message(
            config.complex_model_anthropic, system, user, max_tokens=120
        )

    async def transcribe(self, audio_b64: str, mime: str, *, config: Any) -> TextResult:
        """Transcribe recorded speech with OpenAI (Whisper / gpt-4o-transcribe)."""
        key = self._keys.get("openai")
        if key is None:
            return TextResult("", error="No OpenAI key for speech-to-text.")
        try:
            audio = base64.b64decode(audio_b64)
        except Exception:
            return TextResult("", error="Could not decode recorded audio.")
        if not audio:
            return TextResult("", error="Empty recording.")
        ext = (
            "webm" if "webm" in mime
            else "ogg" if "ogg" in mime
            else "mp3" if ("mpeg" in mime or "mp3" in mime)
            else "mp4" if "mp4" in mime
            else "wav"
        )
        try:
            import httpx
        except ModuleNotFoundError:  # pragma: no cover - dependency guard
            return TextResult("", error="httpx is not installed.")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    OPENAI_STT_URL,
                    headers={"authorization": f"Bearer {key.api_key}"},
                    files={"file": (f"speech.{ext}", audio, mime or "audio/webm")},
                    data={"model": config.stt_model, "response_format": "json"},
                )
            if resp.status_code >= 400:
                return TextResult("", error=f"OpenAI STT {resp.status_code}: {resp.text[:160]}")
            return TextResult(str(resp.json().get("text", "")).strip())
        except Exception as exc:
            return TextResult("", error=f"OpenAI STT request failed: {exc}")

    async def synthesize(self, text: str, *, config: Any) -> AudioResult:
        key = self._keys.get("openai")
        if key is None:
            return AudioResult("", "", error="No OpenAI key for text-to-speech.")
        payload = {
            "model": config.tts_model,
            "voice": config.voice,
            "input": text,
            "response_format": "mp3",
        }
        try:
            import httpx
        except ModuleNotFoundError:  # pragma: no cover - dependency guard
            return AudioResult("", "", error="httpx is not installed for text-to-speech.")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    OPENAI_TTS_URL,
                    headers={"authorization": f"Bearer {key.api_key}", "content-type": "application/json"},
                    json=payload,
                )
            if resp.status_code >= 400:
                return AudioResult("", "", error=f"OpenAI TTS {resp.status_code}: {resp.text[:160]}")
            return AudioResult(base64.b64encode(resp.content).decode("ascii"), "audio/mpeg")
        except Exception as exc:
            return AudioResult("", "", error=f"OpenAI TTS request failed: {exc}")

    # -- raw provider calls ----------------------------------------------

    @staticmethod
    def _gate_user_prompt(context: str, *, awaiting: bool) -> str:
        hint = (
            "The agent appears to be waiting for the user to choose or confirm "
            "something.\n\n"
            if awaiting
            else ""
        )
        return f"{hint}Most recent terminal changes:\n\n{context}"

    async def _anthropic_message(self, model: str, system: str, user: str, *, max_tokens: int) -> TextResult:
        key = self._keys.get("anthropic")
        if key is None:
            return TextResult("", error="No Anthropic key available.")
        try:
            import httpx
        except ModuleNotFoundError:  # pragma: no cover - dependency guard
            return TextResult("", error="httpx is not installed.")
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if key.auth_scheme == "bearer-oauth":
            headers["authorization"] = f"Bearer {key.api_key}"
            headers["anthropic-beta"] = _OAUTH_BETA
        else:
            headers["x-api-key"] = key.api_key
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
            if resp.status_code >= 400:
                return TextResult("", error=f"Anthropic {resp.status_code}: {resp.text[:160]}")
            data = resp.json()
            parts = data.get("content") or []
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            return TextResult(text.strip())
        except Exception as exc:
            return TextResult("", error=f"Anthropic request failed: {exc}")

    async def _openai_chat(
        self, model: str, system: str, user: str, *, max_tokens: int, json_mode: bool
    ) -> TextResult:
        key = self._keys.get("openai")
        if key is None:
            return TextResult("", error="No OpenAI key available.")
        try:
            import httpx
        except ModuleNotFoundError:  # pragma: no cover - dependency guard
            return TextResult("", error="httpx is not installed.")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    OPENAI_CHAT_URL,
                    headers={"authorization": f"Bearer {key.api_key}", "content-type": "application/json"},
                    json=payload,
                )
            if resp.status_code >= 400:
                return TextResult("", error=f"OpenAI {resp.status_code}: {resp.text[:160]}")
            data = resp.json()
            choices = data.get("choices") or []
            text = choices[0].get("message", {}).get("content", "") if choices else ""
            return TextResult(str(text).strip())
        except Exception as exc:
            return TextResult("", error=f"OpenAI request failed: {exc}")
