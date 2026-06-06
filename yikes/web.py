from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .app_core import YikesAppController
from .speaker import ConnectionHub, SpeakerService
from .terminal_bridge import WebTerminalManager, handle_terminal_ws
from .web_auth import LoginThrottle, WebAuthConfig, developer_mode_from_env
from .web_handler import WebMessageHandler

try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError:  # pragma: no cover - dependency guard
    FastAPI = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment]
    FileResponse = None  # type: ignore[assignment]
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
    app.state.yikes_speaker = SpeakerService(app.state.yikes)
    app.state.yikes.speaker_public = app.state.yikes_speaker.public_state
    app.state.yikes_web_handler = WebMessageHandler(
        app.state.yikes, app.state.yikes_terminals, app.state.yikes_speaker
    )
    app.state.yikes_auth = auth or WebAuthConfig.load(developer_mode=developer_mode_from_env())
    app.state.yikes_login_throttle = LoginThrottle()
    from .process_manager import ManagedProcessManager

    app.state.yikes.process_manager = ManagedProcessManager()
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
    async def login(request: Request, key: str | None = None, next: str = "/") -> HTMLResponse:
        # Loading the page (no key) is never throttled; only guesses are.
        if not key:
            return HTMLResponse(_login_page(invalid=False, locked=False), status_code=200)

        throttle: LoginThrottle = app.state.yikes_login_throttle
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        wait = throttle.retry_after(client, now)
        if wait > 0:
            response = HTMLResponse(_login_page(invalid=True, locked=True), status_code=429)
            response.headers["Retry-After"] = str(int(wait) + 1)
            return response

        if app.state.yikes_auth.verify_login_key(key):
            throttle.record_success(client)
            response = RedirectResponse(_safe_next(next), status_code=303)
            response.set_cookie(
                app.state.yikes_auth.cookie_name,
                app.state.yikes_auth.issue_cookie(),
                httponly=True,
                samesite="lax",
                max_age=app.state.yikes_auth.cookie_ttl_seconds,
            )
            return response

        delay = throttle.record_failure(client, now)
        # Small constant latency per failed guess; the (longer) lockout below is
        # what actually throttles repeated attempts.
        await asyncio.sleep(0.5)
        response = HTMLResponse(_login_page(invalid=True, locked=False), status_code=401)
        response.headers["Retry-After"] = str(int(delay) + 1)
        return response

    @app.get("/")
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        # Cache-bust local assets so edited JS/CSS load on a normal refresh.
        version = str(int(_static_stamp()))
        html = html.replace("/static/yikes-web.js", f"/static/yikes-web.js?v={version}")
        html = html.replace("/static/yikes-web.css", f"/static/yikes-web.css?v={version}")
        return HTMLResponse(html)

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return app.state.yikes.state()

    @app.get("/file")
    async def serve_file(path: str):
        """Serve a local image referenced by the agent (e.g. a pasted image).

        Auth is enforced by the cookie middleware above (same as /login), so only
        an authenticated browser can fetch it. Restricted to existing image files
        to keep this from being a general file-read surface.
        """
        return _serve_local_image(path)

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        if not app.state.yikes_auth.verify_cookie(websocket.cookies.get(app.state.yikes_auth.cookie_name)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        # A hub serializes all writes to this socket, so the speaker service can
        # push unsolicited "speak" events while a request/response is in flight.
        hub = ConnectionHub(websocket)
        app.state.yikes_speaker.register_connection(hub)
        try:
            await hub.send_json({"type": "state", "state": app.state.yikes.state()})
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await hub.send_json({"type": "error", "message": "Invalid JSON message."})
                    continue
                mtype = str(message.get("type", "state"))
                if mtype == "submit":
                    await app.state.yikes_web_handler.stream_submit(hub, message)
                    continue
                if mtype == "voice.interpret":
                    # Slow (LLM) — run concurrently so it never blocks the loop.
                    asyncio.create_task(app.state.yikes_web_handler.interpret_voice(hub, message))
                    continue
                if mtype == "voice.utterance":
                    # Slow (STT + LLM) — run concurrently off the receive loop.
                    asyncio.create_task(app.state.yikes_web_handler.transcribe_voice(hub, message))
                    continue
                response = await app.state.yikes_web_handler.handle(message)
                await hub.send_json(response)
        except WebSocketDisconnect:
            return
        finally:
            app.state.yikes_speaker.unregister_connection(hub)

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

    @app.on_event("shutdown")
    async def _stop_pane_processes() -> None:
        speaker = getattr(app.state, "yikes_speaker", None)
        if speaker is not None:
            speaker.stop_all()
        manager = getattr(app.state.yikes, "process_manager", None)
        if manager is not None:
            manager.stop_all()

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


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico", ".tif", ".tiff"}
_MAX_IMAGE_BYTES = 50 * 1024 * 1024


def _serve_local_image(path: str):
    """Validate and stream a local image file (used by the authenticated /file route)."""
    import mimetypes

    raw = (path or "").strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not resolved.is_file():
        return JSONResponse({"error": "not a file"}, status_code=404)
    mime, _ = mimetypes.guess_type(str(resolved))
    is_image = (mime or "").startswith("image/") or resolved.suffix.lower() in _IMAGE_SUFFIXES
    if not is_image:
        return JSONResponse({"error": "not an image"}, status_code=415)
    try:
        if resolved.stat().st_size > _MAX_IMAGE_BYTES:
            return JSONResponse({"error": "image too large"}, status_code=413)
    except OSError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(resolved, media_type=mime or "application/octet-stream")


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


def _login_page(*, invalid: bool, locked: bool = False) -> str:
    if locked:
        error = "<p class='error'>Too many attempts — wait a moment and try again.</p>"
    elif invalid:
        error = "<p class='error'>Invalid or expired login key.</p>"
    else:
        error = ""
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


app = create_app()
