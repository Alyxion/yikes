from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .app_core import YikesAppController
from .terminal_bridge import WebTerminalManager, handle_terminal_ws
from .web_auth import WebAuthConfig, developer_mode_from_env

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError:  # pragma: no cover - dependency guard
    FastAPI = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment]
    HTMLResponse = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    RedirectResponse = None  # type: ignore[assignment]
    StaticFiles = None  # type: ignore[assignment]


STATIC_DIR = Path(__file__).with_name("web_static")


def create_app(
    controller: YikesAppController | None = None,
    *,
    auth: WebAuthConfig | None = None,
    use_stage: bool = True,
):
    """Create the yikes! web UI ASGI app.

    The returned FastAPI application is deliberately thin. All business state
    and command handling lives in :class:`YikesAppController`, so a llming-com
    session can reuse the same object without depending on this HTTP shell.
    """

    if FastAPI is None or HTMLResponse is None or StaticFiles is None:
        raise RuntimeError("The web UI requires fastapi and uvicorn. Run `poetry install`.")

    app = FastAPI(title="yikes!")
    app.state.yikes = controller or YikesAppController()
    app.state.yikes_terminals = WebTerminalManager()
    app.state.yikes_auth = auth or WebAuthConfig.load(developer_mode=developer_mode_from_env())
    _mount_llming_stage(app, use_stage=use_stage)

    @app.middleware("http")
    async def require_auth_cookie(request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/login":
            return await call_next(request)
        if not app.state.yikes_auth.verify_cookie(request.cookies.get(app.state.yikes_auth.cookie_name)):
            if _wants_html(request):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "login required"}, status_code=401)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/login")
    async def login(key: str | None = None, next: str = "/") -> HTMLResponse:
        if app.state.yikes_auth.verify_login_key(key):
            response = RedirectResponse(_safe_next(next), status_code=303)
            response.set_cookie(
                app.state.yikes_auth.cookie_name,
                app.state.yikes_auth.issue_cookie(),
                httponly=True,
                samesite="lax",
                max_age=app.state.yikes_auth.cookie_ttl_seconds,
            )
            return response
        status = 401 if key else 200
        return HTMLResponse(_login_page(invalid=bool(key)), status_code=status)

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return app.state.yikes.state()

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        if not app.state.yikes_auth.verify_cookie(websocket.cookies.get(app.state.yikes_auth.cookie_name)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_json({"type": "state", "state": app.state.yikes.state()})
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON message."})
                    continue
                response = await _handle_message(app.state.yikes, app.state.yikes_terminals, message)
                await websocket.send_json(response)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/terminal/{terminal_id}")
    async def terminal(websocket: WebSocket, terminal_id: str) -> None:
        if not app.state.yikes_auth.verify_cookie(websocket.cookies.get(app.state.yikes_auth.cookie_name)):
            await websocket.close(code=1008)
            return
        await handle_terminal_ws(websocket, app.state.yikes_terminals, terminal_id)

    @app.websocket("/dev/reload")
    async def dev_reload(websocket: WebSocket) -> None:
        if not app.state.yikes_auth.developer_mode:
            await websocket.close(code=1008)
            return
        if not app.state.yikes_auth.verify_cookie(websocket.cookies.get(app.state.yikes_auth.cookie_name)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        stamp = _static_stamp()
        try:
            while True:
                await asyncio.sleep(0.75)
                next_stamp = _static_stamp()
                if next_stamp != stamp:
                    await websocket.send_json({"type": "reload"})
                    stamp = next_stamp
        except WebSocketDisconnect:
            return

    return app


def _mount_llming_stage(app: Any, *, use_stage: bool) -> None:
    if not use_stage:
        return
    try:
        from llming_stage import Stage
        from .llming import build_session_router
    except Exception:
        return
    try:
        stage = Stage(app, title="yikes!", root=STATIC_DIR, dev=developer_mode_from_env())
        stage_session = stage.session(app_name="yikes", command_prefix="/cmd")
        stage_session.session_router.include(build_session_router("yikes"))
        app.state.yikes_stage = stage
    except Exception:
        return


def _wants_html(request: Any) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or request.url.path == "/"


def _safe_next(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith("/login"):
        return "/"
    return value


def _static_stamp() -> float:
    stamp = 0.0
    for path in STATIC_DIR.rglob("*"):
        if path.is_file():
            try:
                stamp = max(stamp, path.stat().st_mtime)
            except OSError:
                pass
    return stamp


def _login_page(*, invalid: bool) -> str:
    error = "<p class='error'>Invalid or expired login key.</p>" if invalid else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>yikes! login</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f1115; color: #e9edf3; font: 16px system-ui, sans-serif; }}
    main {{ width: min(440px, calc(100vw - 32px)); border: 1px solid #272d35; background: #15181d; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    p {{ color: #9ba5b5; line-height: 1.5; }}
    .error {{ color: #ff8fa1; }}
    input {{ width: 100%; min-height: 42px; border: 1px solid #2f7de1; background: #10141a; color: #e9edf3; padding: 0 10px; }}
    button {{ margin-top: 12px; width: 100%; min-height: 42px; border: 0; background: #2f7de1; color: white; font: inherit; cursor: pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>yikes!</h1>
    <p>This local control surface requires an authenticated browser cookie.</p>
    {error}
    <form method="get" action="/login">
      <input name="key" placeholder="Login key" autocomplete="off" autofocus>
      <input type="hidden" name="next" value="/">
      <button type="submit">Continue</button>
    </form>
  </main>
</body>
</html>"""


async def _handle_message(
    controller: YikesAppController,
    terminals: WebTerminalManager,
    message: dict[str, Any],
) -> dict[str, Any]:
    msg_type = str(message.get("type", "state"))
    try:
        if msg_type == "state":
            return {"type": "state", "state": controller.state()}
        if msg_type == "submit":
            text = str(message.get("text", ""))
            state = await asyncio.to_thread(controller.submit, text)
            return {"type": "state", "state": state}
        if msg_type == "suggest":
            text = str(message.get("text", ""))
            return {"type": "suggestions", "items": controller.suggestions(text)}
        if msg_type == "new.open":
            return {"type": "state", "state": controller.open_new_session()}
        if msg_type == "new.update":
            changes = message.get("changes", {})
            if not isinstance(changes, dict):
                changes = {}
            return {"type": "state", "state": controller.update_new_session(**changes)}
        if msg_type == "new.confirm":
            state = await asyncio.to_thread(controller.confirm_new_session)
            return {"type": "state", "state": state}
        if msg_type == "new.cancel":
            return {"type": "state", "state": controller.cancel_new_session()}
        if msg_type == "session.switch":
            return {"type": "state", "state": controller.switch_session(str(message.get("session_id", "")))}
        if msg_type == "session.close":
            return {"type": "state", "state": controller.close_session(str(message.get("session_id", "")))}
        if msg_type == "session.close_all":
            return {"type": "state", "state": controller.close_all()}
        if msg_type == "dir.list":
            return {"type": "dir.entries", "data": controller.directory_entries(_optional_text(message.get("root")))}
        if msg_type == "term.open":
            attached = controller.attach_command(_optional_text(message.get("session_id")))
            if attached is None:
                return {"type": "error", "message": "No attachable tmux session is selected."}
            session_id, command = attached
            terminal = terminals.spawn(
                session_id=session_id,
                command=command,
                cols=_int_or_default(message.get("cols"), 120),
                rows=_int_or_default(message.get("rows"), 34),
            )
            return {
                "type": "term.opened",
                "terminal_id": terminal.terminal_id,
                "session_id": session_id,
                "title": terminal.title,
            }
        if msg_type == "term.close":
            terminals.close(str(message.get("terminal_id", "")))
            return {"type": "state", "state": controller.state()}
    except Exception as exc:
        state = controller.state()
        state["error"] = str(exc)
        return {"type": "state", "state": state}
    return {"type": "error", "message": f"Unknown message type: {msg_type}"}


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


app = create_app()
