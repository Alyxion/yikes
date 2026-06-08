import json
import re
import shlex
import sys
import asyncio
import contextlib
import hashlib
import os
import threading
import time
from pathlib import Path

from .domain import AgentSettings, Backend, ChatOptions, Complexity, Driver, McpServer
from .errors import YikesError
from .events import DEFAULT_EVENT_STORE, EventLog
from .prompt_profile import (
    DEFAULT_PROMPT_PROFILE_PATH,
    load_prompt_profile,
    merge_prompt_profile_text,
    prompt_profile_generation_prompt,
)
from .runtime import DEFAULT_RUNTIME_STORE
from .sandbox import DEFAULT_SANDBOX_STORE
from .services import ChatService
from .session_inventory import SessionInventory, SessionLifecycle, TmuxSessionController
from .tokens import DEFAULT_TOKEN_STORE
from .drivers import ask_backend

try:
    import typer
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal dev envs
    typer = None


if typer:
    app = typer.Typer(
        add_completion=False,
        help="yikes! terminal app and chatbot smoke tools.",
    )
    tmux_app = typer.Typer(help="Named tmux session automation.")
    prompt_profile_app = typer.Typer(help="Manage the shared local prompt profile.")
    app.add_typer(tmux_app, name="tmux")
    app.add_typer(prompt_profile_app, name="prompt-profile")

    @app.command("chat-smoke")
    def chat_smoke(
        backend: Backend = typer.Option(..., "--backend", "-b"),
        driver: Driver = typer.Option(..., "--driver", "-d"),
        cwd: Path | None = typer.Option(None, "--cwd"),
        timeout: float = typer.Option(180.0, "--timeout"),
        model: str | None = typer.Option(None, "--model"),
        complexity: Complexity = typer.Option(Complexity.MEDIUM, "--complexity"),
        web_search: bool = typer.Option(True, "--web-search/--no-web-search"),
        tmux: bool = typer.Option(False, "--tmux/--no-tmux", help="Use real interactive tmux transport where supported."),
        capture: bool = typer.Option(True, "--capture/--no-capture", help="Use managed answer capture for tmux chat turns."),
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        try:
            settings = _settings_from_cli(web_search, tmux, capture, read_dir, write_dir, mcp)
            result = ChatService().run_goal_flow(
                backend,
                driver,
                cwd=cwd,
                timeout=timeout,
                model=model,
                complexity=complexity,
                settings=settings,
            )
        except YikesError as exc:
            typer.echo(f"yikes: {exc}", err=True)
            raise typer.Exit(1) from exc

        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "backend": result.backend.value,
                        "driver": result.driver.value,
                        "turns": result.turns,
                        "remembered_name": result.remembered_name,
                    }
                )
            )
            return
        for turn in result.turns:
            typer.echo(turn)

    @app.command("tui")
    def tui(
        backend: Backend | None = typer.Option(None, "--backend", "-b"),
        driver: str | None = typer.Option(None, "--driver", "-d", help="Interactive chat mode: direct or tmux."),
        cwd: Path | None = typer.Option(None, "--cwd"),
        timeout: float = typer.Option(180.0, "--timeout"),
        model: str | None = typer.Option(None, "--model"),
        complexity: Complexity | None = typer.Option(None, "--complexity"),
        web_search: bool | None = typer.Option(None, "--web-search/--no-web-search"),
        tmux: bool | None = typer.Option(None, "--tmux/--no-tmux"),
        capture: bool | None = typer.Option(None, "--capture/--no-capture", help="Use managed answer capture for tmux chat turns."),
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
    ) -> None:
        from .tui import run_tui

        parsed_driver = _parse_tui_driver(driver)
        settings = _settings_from_cli(web_search, tmux, capture, read_dir, write_dir, mcp) if _has_settings_cli(web_search, tmux, capture, read_dir, write_dir, mcp) else None
        run_tui(
            backend=backend,
            driver=parsed_driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
        )

    @app.command("sessions")
    def sessions(
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        typer.echo(SessionInventory(runtime_store=runtime_store, sandbox_store=sandbox_store).format())

    @app.command("close")
    def close(
        session_id: str = typer.Argument(..., help="yikes! session ID to close."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        result = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store).close(session_id)
        typer.echo(result.message)
        if not result.closed:
            raise typer.Exit(1)

    @app.command("attach")
    def attach(
        session_id: str = typer.Argument(..., help="yikes! session ID to overtake."),
        print_only: bool = typer.Option(False, "--print-only", help="Print attach command instead of execing it."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        command = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store).attach_command(session_id)
        if command is None:
            typer.echo(f"Session not found or not attachable: {session_id}", err=True)
            raise typer.Exit(1)
        if print_only:
            typer.echo(shlex.join(command))
            return
        os.execvp(command[0], command)

    @app.command("label")
    def label(
        session_id: str = typer.Argument(..., help="yikes! session ID (see `yikes sessions`)."),
        icon: str | None = typer.Option(None, "--icon", help="Emoji icon for the session."),
        name: str | None = typer.Option(None, "--name", "-n", help="Custom display name (empty string clears it)."),
        description: str | None = typer.Option(None, "--description", "-d", help="Description (empty string clears it)."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        """Set a session's icon, name and/or description (shared with the web UI)."""
        from .runtime import DurableSessionManager
        from .session_icons import SessionIcons

        resolved = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store).resolve_session_id(session_id) or session_id
        store = SessionIcons(DurableSessionManager(runtime_store))
        if store.update(resolved, icon=icon, name=name, description=description) is None:
            typer.echo(f"Session not found: {session_id}", err=True)
            raise typer.Exit(1)
        meta = store.meta_for(resolved)
        typer.echo(
            f"{meta.get('icon', '')} {resolved} — name: {meta.get('name') or '(default)'}"
            + (f" · {meta['description']}" if meta.get("description") else "")
        )

    @app.command("claude")
    def claude(
        name: str | None = typer.Option(None, "--name", "-n", help="Session name. Default: directory basename."),
        isolated: bool | None = typer.Option(None, "--isolated/--no-isolated", "-i/-I", help="Run isolated in Docker."),
        new: bool = typer.Option(False, "--new", help="Replace any existing session with this name."),
        model: str | None = typer.Option(None, "--model", help="Backend model name."),
        port: list[str] | None = typer.Option(None, "--port", "-p", help="Publish a port when isolated (HOST or HOST:CONTAINER). Repeatable."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        message: str | None = typer.Option(None, "--message", "-m", help="Initial prompt to pre-fill in a new session."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the pre-flight panel prompt and start immediately."),
    ) -> None:
        _launch(Backend.CLAUDE, name=name, isolated=isolated, new=new, model=model, port=port, cwd=cwd, message=message, yes=yes)

    @app.command("codex")
    def codex(
        name: str | None = typer.Option(None, "--name", "-n", help="Session name. Default: directory basename."),
        isolated: bool | None = typer.Option(None, "--isolated/--no-isolated", "-i/-I", help="Run isolated in Docker."),
        new: bool = typer.Option(False, "--new", help="Replace any existing session with this name."),
        model: str | None = typer.Option(None, "--model", help="Backend model name."),
        port: list[str] | None = typer.Option(None, "--port", "-p", help="Publish a port when isolated (HOST or HOST:CONTAINER). Repeatable."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        message: str | None = typer.Option(None, "--message", "-m", help="Initial prompt to pre-fill in a new session."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the pre-flight panel prompt and start immediately."),
    ) -> None:
        _launch(Backend.CODEX, name=name, isolated=isolated, new=new, model=model, port=port, cwd=cwd, message=message, yes=yes)

    @app.command("init")
    def init(
        cwd: Path | None = typer.Option(None, "--cwd", help="Where to write yikes.toml. Default: current directory."),
        force: bool = typer.Option(False, "--force", help="Overwrite an existing yikes.toml."),
    ) -> None:
        from .project_config import CONFIG_NAME, starter_toml

        target = (cwd or Path.cwd()).expanduser() / CONFIG_NAME
        if target.exists() and not force:
            typer.echo(f"yikes: {target} already exists (use --force to overwrite)", err=True)
            raise typer.Exit(1)
        target.write_text(starter_toml())
        typer.echo(_fmt(f"wrote {target}", "success"))

    @app.command("setup")
    def setup(
        backend: Backend | None = typer.Option(None, "--backend", "-b", help="Backend to run the scan. Default: yikes.toml or claude."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        message: str | None = typer.Option(None, "--message", "-m", help="What you want to build, to guide the scan."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Write yikes.toml without confirmation."),
    ) -> None:
        from .project_config import load_project_config

        project_dir = (cwd or Path.cwd()).expanduser()
        resolved_backend = _resolve_setup_backend(backend, _config_backend(load_project_config(project_dir)))
        if not _run_setup(resolved_backend, project_dir, assume_yes=yes, goal=message):
            raise typer.Exit(1)

    @app.command("menu")
    def menu() -> None:
        _run_menu()

    @tmux_app.command("start")
    def tmux_start(
        name: str = typer.Argument(..., help="Stable yikes! tmux session name."),
        backend: Backend = typer.Option(Backend.CODEX, "--backend", "-b", help="Agent backend to launch."),
        cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Working directory for the session."),
        model: str | None = typer.Option(None, "--model", help="Backend model name."),
        replace: bool = typer.Option(False, "--replace", help="Kill and recreate an existing session with this name."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        try:
            result = TmuxSessionController(runtime_store=runtime_store, sandbox_store=sandbox_store).start(
                name,
                backend=backend,
                cwd=cwd,
                model=model,
                replace=replace,
            )
        except YikesError as exc:
            typer.echo(f"yikes: {exc}", err=True)
            raise typer.Exit(1) from exc
        payload = {
            "id": result.id,
            "name": result.name,
            "backend": result.backend,
            "socket": result.socket,
            "session": result.session,
            "created": result.created,
            "replaced": result.replaced,
        }
        if json_output:
            typer.echo(json.dumps(payload))
            return
        action = "replaced" if result.replaced else "started" if result.created else "already running"
        typer.echo(f"{action}: {result.name} ({result.id}) {result.backend} @ {result.socket}")

    @tmux_app.command("state")
    def tmux_state(
        name: str = typer.Argument(..., help="Session ID or stable tmux session name."),
        lines: int = typer.Option(120, "--lines", help="Terminal lines to include with --output."),
        output: bool = typer.Option(False, "--output", help="Print the captured terminal output."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        controller = TmuxSessionController(runtime_store=runtime_store, sandbox_store=sandbox_store)
        try:
            session_id, activity, snapshot = controller.state(name)
        except YikesError as exc:
            typer.echo(f"yikes: {exc}", err=True)
            raise typer.Exit(1) from exc
        snapshot = "\n".join((snapshot or "").splitlines()[-lines:])
        payload = {"id": session_id, "activity": activity.to_json(), "output": snapshot if output else None}
        if json_output:
            typer.echo(json.dumps(payload))
            return
        typer.echo(f"{session_id}: {activity.state} ({activity.reason})")
        if output and snapshot:
            typer.echo(snapshot)

    @tmux_app.command("send")
    def tmux_send(
        name: str = typer.Argument(..., help="Session ID or stable tmux session name."),
        text: str = typer.Argument(..., help="Text to paste into the session."),
        submit: bool = typer.Option(True, "--submit/--no-submit", help="Press Enter after pasting."),
        wait: bool = typer.Option(False, "--wait", help="Wait until the terminal settles or asks for selection."),
        timeout: float = typer.Option(180.0, "--timeout", help="Maximum wait time in seconds."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        controller = TmuxSessionController(runtime_store=runtime_store, sandbox_store=sandbox_store)
        result = controller.send(name, text, submit=submit)
        if not result.closed:
            typer.echo(result.message, err=True)
            raise typer.Exit(1)
        activity = controller.wait(name, timeout=timeout) if wait else None
        if json_output:
            typer.echo(json.dumps({"ok": True, "message": result.message, "activity": activity.to_json() if activity else None}))
            return
        typer.echo(result.message)
        if activity is not None:
            typer.echo(f"state: {activity.state} ({activity.reason})")

    @tmux_app.command("wait")
    def tmux_wait(
        name: str = typer.Argument(..., help="Session ID or stable tmux session name."),
        timeout: float = typer.Option(180.0, "--timeout", help="Maximum wait time in seconds."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        controller = TmuxSessionController(runtime_store=runtime_store, sandbox_store=sandbox_store)
        try:
            activity = controller.wait(name, timeout=timeout)
        except YikesError as exc:
            typer.echo(f"yikes: {exc}", err=True)
            raise typer.Exit(1) from exc
        if json_output:
            typer.echo(json.dumps(activity.to_json()))
            return
        typer.echo(f"{activity.state}: {activity.reason}")
        if activity.reason.startswith("timeout after"):
            raise typer.Exit(1)

    @tmux_app.command("kill")
    def tmux_kill(
        name: str = typer.Argument(..., help="Session ID or stable tmux session name."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        result = TmuxSessionController(runtime_store=runtime_store, sandbox_store=sandbox_store).kill(name)
        typer.echo(result.message)
        if not result.closed:
            raise typer.Exit(1)

    @prompt_profile_app.command("ensure")
    def prompt_profile_ensure(
        path: Path | None = typer.Option(None, "--path", help="Profile path. Defaults to the shared user-local profile."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        profile = load_prompt_profile(path)
        target = (path or DEFAULT_PROMPT_PROFILE_PATH).expanduser()
        payload = {
            "path": str(target),
            "version": profile.version,
            "setup_variants": len(profile.setup_variants),
            "boundary_templates": len(profile.boundary_templates),
            "marker_pairs": len(profile.marker_pairs),
            "shared_for": ["codex", "claude"],
        }
        if json_output:
            typer.echo(json.dumps(payload))
            return
        typer.echo(
            f"prompt profile ready: {target} "
            f"({payload['setup_variants']} setup, {payload['boundary_templates']} boundary, "
            f"{payload['marker_pairs']} marker pairs; shared for Codex and Claude)"
        )

    @prompt_profile_app.command("generate")
    def prompt_profile_generate(
        backend: Backend = typer.Option(Backend.CODEX, "--backend", "-b", help="Backend CLI used once to propose variants."),
        path: Path | None = typer.Option(None, "--path", help="Profile path. Defaults to the shared user-local profile."),
        count: int = typer.Option(10, "--count", help="Target number of variants to request."),
        model: str | None = typer.Option(None, "--model", help="Optional backend model."),
        timeout: float = typer.Option(180.0, "--timeout", help="Generation timeout in seconds."),
        replace: bool = typer.Option(False, "--replace", help="Replace instead of extending the existing profile."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Print generated JSON without writing it."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        existing = load_prompt_profile(path)
        prompt = prompt_profile_generation_prompt(existing, count=count)
        raw = ask_backend(
            backend,
            Driver.DIRECT,
            prompt,
            cwd=Path.cwd(),
            timeout=timeout,
            model=model,
            settings=AgentSettings(web_search_enabled=False),
        )
        if dry_run:
            typer.echo(raw)
            return
        updated = merge_prompt_profile_text(raw, path=path, replace=replace)
        target = (path or DEFAULT_PROMPT_PROFILE_PATH).expanduser()
        payload = {
            "path": str(target),
            "backend": backend.value,
            "setup_variants": len(updated.setup_variants),
            "boundary_templates": len(updated.boundary_templates),
            "marker_pairs": len(updated.marker_pairs),
            "shared_for": ["codex", "claude"],
        }
        if json_output:
            typer.echo(json.dumps(payload))
            return
        typer.echo(
            f"updated shared prompt profile: {target} "
            f"({payload['setup_variants']} setup, {payload['boundary_templates']} boundary, "
            f"{payload['marker_pairs']} marker pairs)"
        )

    @app.command("close-all")
    def close_all(
        runtime: str | None = typer.Option(None, "--runtime", "-r", help="Runtime filter: docker, tmux, remote-server, or all."),
        backend: str | None = typer.Option(None, "--backend", "-b", help="Backend filter: claude, codex, or all."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        lifecycle = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store)
        summaries = lifecycle.matching(runtime=runtime, backend=backend)
        if not summaries:
            typer.echo("No matching sessions to close.")
            return
        typer.echo(_fmt(f"This will close {len(summaries)} session(s):", "header"))
        for summary in summaries:
            typer.echo(f"  {summary.id}  {_fmt(f'{summary.runtime}/{summary.backend}', 'accent')}  {summary.state}")
        if not yes:
            if not sys.stdin.isatty():
                typer.echo("yikes: refusing to close sessions non-interactively; pass --yes", err=True)
                raise typer.Exit(1)
            from . import interactive

            if not interactive.confirm(f"Close {len(summaries)} session(s)?", default=False):
                typer.echo("aborted")
                return
        results = [lifecycle.close(summary.id) for summary in summaries]
        for result in results:
            typer.echo(result.message)
        closed = sum(1 for result in results if result.closed)
        typer.echo(_fmt(f"Closed {closed}/{len(results)} sessions.", "success"))

    @app.command("capture", hidden=True)
    def capture(
        label: str = typer.Argument(..., help="True state: idle, awaiting-selection, thinking, streaming, unknown."),
        session: str | None = typer.Argument(None, help="Session id/name. Default: the running session for this directory."),
        notes: str | None = typer.Option(None, "--notes", help="Free-text note stored with the sample."),
        frames: int = typer.Option(4, "--frames", help="Number of rapid snapshots to capture."),
        span: float = typer.Option(0.5, "--span", help="Seconds the frames are spread across (catches spinner animation)."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Directory used to auto-pick a session."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        """Record a labeled training sample of a live session's terminal state."""
        from .training_capture import CaptureError, capture_sample

        try:
            result = capture_sample(
                label,
                session,
                cwd=cwd,
                notes=notes,
                frames=frames,
                span=span,
                runtime_store=runtime_store,
                sandbox_store=sandbox_store,
            )
        except CaptureError as exc:
            typer.echo(f"capture: {exc}", err=True)
            raise typer.Exit(1) from exc
        flag = "" if result.predicted == result.label else _fmt(f"  (yikes predicted: {result.predicted})", "warn")
        typer.echo(_fmt(f"Captured {result.frame_count} frame(s) → {result.label}{flag}", "success"))
        typer.echo(f"  {result.backend} {result.backend_version}")
        typer.echo(f"  {result.path}")

    @app.command("token")
    def token(
        name: str = typer.Option("browser", "--name", "-n", help="Human label for this token."),
        ttl: int = typer.Option(3600, "--ttl", help="Temporary token lifetime in seconds."),
        permanent: bool = typer.Option(False, "--permanent", help="Create or rotate a permanent token."),
        store: Path = typer.Option(DEFAULT_TOKEN_STORE, "--store", help="Token store path."),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        from .tokens import TokenStore

        token_store = TokenStore(store)
        value = token_store.create_permanent(name) if permanent else token_store.create_temporary(name, ttl)
        if json_output:
            typer.echo(json.dumps({"token": value, "label": name, "permanent": permanent}))
            return
        typer.echo(value)

    @app.command("server")
    def server(
        host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Defaults to loopback."),
        port: int = typer.Option(8989, "--port", "-p", help="WebSocket port."),
        auth: bool = typer.Option(True, "--auth/--no-auth", help="Require bearer-token auth."),
        token_store: Path = typer.Option(DEFAULT_TOKEN_STORE, "--token-store", help="Bearer-token store path."),
        event_store: Path = typer.Option(DEFAULT_EVENT_STORE, "--event-store", help="Session event-log directory."),
        bootstrap_token_env: str | None = typer.Option(
            None,
            "--bootstrap-token-env",
            help="Read an initial bearer token from this environment variable and hash it into the token store.",
        ),
    ) -> None:
        from .remote import RemoteCommandHandler, RemoteServerConfig, YikesRemoteServer
        from .tokens import TokenStore

        tokens = TokenStore(token_store)
        if bootstrap_token_env:
            bootstrap_token = os.environ.get(bootstrap_token_env)
            if bootstrap_token:
                tokens.add_existing(bootstrap_token, label=f"bootstrap:{bootstrap_token_env}", permanent=True)
        if auth and not tokens.list_tokens():
            typer.echo(
                f"No bearer tokens found in {tokens.path}. Create one with: yikes token --store {tokens.path}",
                err=True,
            )
        config = RemoteServerConfig(host=host, port=port, require_token=auth)
        handler = RemoteCommandHandler(token_store=tokens, event_log=EventLog(event_store), require_token=auth)
        remote_server = YikesRemoteServer(handler, config)
        typer.echo(f"yikes! server listening on {config.websocket_url} (auth: {'on' if auth else 'off'})")
        asyncio.run(remote_server.serve_forever())

    @app.command("web")
    def web(
        host: str = typer.Option("0.0.0.0", "--host", help="Bind address. Default serves all interfaces; use 127.0.0.1 for loopback only."),
        port: int = typer.Option(8760, "--port", "-p", help="HTTP port."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Default start directory for new sessions."),
        dev: bool = typer.Option(False, "--dev/--no-dev", help="Enable development reload endpoints."),
        persistent_auth: bool = typer.Option(True, "--persistent-auth/--ephemeral-auth", help="Reuse the local login key across restarts."),
        open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the web UI in the default browser."),
        url_only: bool = typer.Option(False, "--url", help="Print the login URL (with key) and exit; do not start the server."),
    ) -> None:
        from .web_auth import WebAuthConfig

        root = (cwd or Path.cwd()).expanduser()
        # Auth key is user-global (not per-directory), so it's the same wherever
        # `yikes web` is launched and `--url` always matches the running server.
        auth_config = WebAuthConfig.load(developer_mode=dev, persist_auth=persistent_auth)
        advertise = _advertise_hosts(host)
        if url_only:
            # one machine-consumable URL: the most reachable host for this bind
            typer.echo(auth_config.login_url(host=_primary_url_host(host), port=port))
            return

        if _port_in_use(host, port):
            typer.echo(
                f"yikes: port {port} is already in use (another yikes web may be running). "
                f"Stop it or use --port.",
                err=True,
            )
            raise typer.Exit(1)

        import threading
        import time
        import webbrowser

        import uvicorn

        from .app_core import YikesAppController
        from .web import create_app

        # Surface developer mode to the app so dev-only UI (e.g. the training
        # label button) is gated on it; off by default for normal users.
        os.environ["YIKES_WEB_DEV"] = "1" if dev else "0"
        app_instance = create_app(YikesAppController(cwd=root), auth=auth_config)
        local_url = auth_config.login_url(host=advertise[0], port=port)
        if open_browser:
            threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(local_url)), daemon=True).start()
        typer.echo(f"yikes! web UI listening on {host}:{port} — open a login URL below:")
        for advertised in advertise:
            typer.echo(f"  {auth_config.login_url(host=advertised, port=port)}")
        if host in {"127.0.0.1", "localhost"}:
            lan = _lan_ipv4_addresses()
            hint = lan[0] if lan else "<lan-ip>"
            typer.echo(
                f"  (loopback only — for another machine: restart with --host 0.0.0.0 "
                f"to serve http://{hint}:{port}/, or use an SSH tunnel)"
            )
        uvicorn.run(app_instance, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        argv = ["menu"]
    # `yikes help [cmd]` is a friendly alias for `--help` / `cmd --help`.
    if argv and argv[0] == "help":
        rest = argv[1:]
        argv = [*rest, "--help"] if rest else ["--help"]
    if typer:
        import click

        try:
            result = app(args=argv, standalone_mode=False)
            return result if isinstance(result, int) else 0
        except typer.Exit as exc:
            return int(exc.exit_code)
        except click.Abort:
            print("Aborted.", file=sys.stderr)
            return 130
        except click.ClickException as exc:
            exc.show()  # clean "Usage: … Error: …" message, no traceback
            return exc.exit_code
        except YikesError as exc:
            print(f"yikes: {exc}", file=sys.stderr)
            return 1

    print("yikes requires the 'typer' dependency. Install the package first: poetry install", file=sys.stderr)
    return 1


def _launch(
    backend: Backend,
    *,
    name: str | None,
    isolated: bool | None,
    new: bool,
    model: str | None,
    port: list[str] | None,
    cwd: Path | None,
    message: str | None = None,
    yes: bool = False,
) -> None:
    from .project_config import load_project_config, normalize_port

    project_dir = (cwd or Path.cwd()).expanduser()
    goal = message
    while True:
        config = load_project_config(project_dir)
        use_isolated = config.isolated if isolated is None else isolated
        resolved_model = model or config.model
        session_name = _sanitize_session_name(name or config.name or project_dir.resolve().name)
        ports = tuple(normalize_port(spec) for spec in port) if port else config.ports
        action, goal = _preflight(
            backend,
            project_dir,
            session_name,
            isolated=use_isolated,
            ports=ports,
            config_source=str(config.source) if config.source else None,
            goal=goal,
            assume_yes=yes,
        )
        if action == "cancel":
            return
        if action == "setup":
            _run_setup(backend, project_dir, assume_yes=False, goal=goal)
            continue  # re-render the panel with the freshly written config
        if action == "prompt":
            continue  # goal updated; re-render the panel
        break
    if use_isolated:
        _launch_docker(backend, project_dir, session_name, new=new, model=resolved_model, ports=ports, message=goal)
    else:
        _launch_host(backend, project_dir, session_name, new=new, model=resolved_model, message=goal)


def _preflight(
    backend: Backend,
    project_dir: Path,
    name: str,
    *,
    isolated: bool,
    ports: tuple[tuple[str, str], ...],
    config_source: str | None,
    goal: str | None,
    assume_yes: bool,
) -> tuple[str, str | None]:
    """Print the panel and return (action, goal). Action: start/setup/prompt/cancel."""
    from . import interactive
    from .preflight import render_panel

    interactive.clear_screen()
    panel = render_panel(
        backend=backend.value,
        name=name,
        location="docker" if isolated else "host",
        cwd=str(project_dir),
        reused=_session_exists(backend, project_dir, name, isolated=isolated),
        config_source=config_source,
        ports=ports,
        isolated=isolated,
        goal=goal,
        color=sys.stdout.isatty(),
    )
    typer.echo(panel)
    typer.echo("")
    if assume_yes or os.environ.get("YIKES_NO_PROMPT") or not sys.stdin.isatty():
        return "start", goal

    action = interactive.select(
        "",
        [
            ("start", "Start the session"),
            ("prompt", "Add an initial prompt"),
            ("setup", "Set up yikes.toml for this project"),
            ("cancel", "Cancel"),
        ],
    )
    if action is None or action == "cancel":
        return "cancel", goal
    if action == "prompt":
        entered = input("initial prompt> ").strip()
        return "prompt", (entered or goal)
    return action, goal


def _run_setup(backend: Backend, project_dir: Path, *, assume_yes: bool, goal: str | None = None) -> bool:
    """Look at the project (via the backend) and write yikes.toml — and an
    AGENTS.md when one is missing. Returns True if anything was written."""
    from .drivers import ask_backend
    from .preflight import (
        parse_scan_result,
        ports_from_scan,
        scan_prompt,
        synthesize_agents_md,
        synthesize_config,
    )
    from .project_config import CONFIG_NAME

    if goal is None and not assume_yes and sys.stdin.isatty():
        entered = input("What is this project about, or what do you want to build here? (press Enter to skip) ").strip()
        goal = entered or None
    try:
        with _progress(f"looking at {project_dir} with {backend.value}"):
            reply = ask_backend(
                backend,
                Driver.DIRECT,
                scan_prompt(goal),
                cwd=project_dir,
                timeout=180.0,
                model=None,
                settings=AgentSettings(),
            )
        data = parse_scan_result(reply)
    except YikesError as exc:
        typer.echo(f"yikes: could not inspect the project: {exc}", err=True)
        return False
    except ValueError as exc:
        typer.echo(f"yikes: could not read the result: {exc}", err=True)
        return False

    ports = ports_from_scan(data)
    scan_backend = data.get("backend") if data.get("backend") in ("claude", "codex") else None
    summary = data.get("summary") if isinstance(data.get("summary"), str) else None
    content = synthesize_config(ports, scan_backend)
    config_path = project_dir / CONFIG_NAME
    agents_path = project_dir / "AGENTS.md"
    make_agents = not agents_path.exists()
    agents_content = synthesize_agents_md(summary, goal, ports) if make_agents else None

    typer.echo("\n" + _fmt("proposed yikes.toml:", "header") + "\n")
    typer.echo(content)
    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        typer.echo(_fmt(f"({notes.strip()})", "muted") + "\n")
    if make_agents:
        typer.echo(_fmt("proposed AGENTS.md (new):", "header") + "\n")
        typer.echo(agents_content)

    targets = "yikes.toml" + " and AGENTS.md" if make_agents else "yikes.toml"
    if not assume_yes and sys.stdin.isatty():
        from . import interactive

        if not interactive.confirm(f"Write {targets}?", default=True):
            typer.echo("skipped")
            return False
    config_path.write_text(content)
    typer.echo(_fmt(f"wrote {config_path}", "success"))
    if make_agents and agents_content is not None:
        agents_path.write_text(agents_content)
        typer.echo(_fmt(f"wrote {agents_path}", "success"))
    return True


def _session_exists(backend: Backend, project_dir: Path, name: str, *, isolated: bool) -> bool:
    from .session_inventory import SessionLifecycle

    ref = _docker_session_id(backend, project_dir) if isolated else name
    return SessionLifecycle().resolve_session_id(ref) is not None


def _lan_ipv4_addresses() -> list[str]:
    """Best-effort list of this host's reachable LAN IPv4 addresses (no deps)."""
    import socket

    found: set[str] = set()
    try:  # the default-route outbound address (no packets are actually sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    try:  # any other addresses bound to this hostname
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith(("127.", "169.254.")))


def _advertise_hosts(host: str) -> list[str]:
    """Hosts the server is actually reachable on, for printing login URLs."""
    if host in {"0.0.0.0", "::", ""}:
        return ["127.0.0.1", *_lan_ipv4_addresses()]
    return [host]


def _primary_url_host(host: str) -> str:
    """The single most useful host for a machine-consumable URL.

    When bound to all interfaces, prefer a LAN address (reachable from other
    machines) over loopback; otherwise the bind host itself.
    """
    if host in {"0.0.0.0", "::", ""}:
        lan = _lan_ipv4_addresses()
        return lan[0] if lan else "127.0.0.1"
    return host


def _port_in_use(host: str, port: int) -> bool:
    import socket

    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((probe_host, port)) == 0


def _config_backend(config: object) -> Backend | None:
    value = getattr(config, "backend", None)
    try:
        return Backend(value) if value else None
    except ValueError:
        return None


def _available_backends() -> list[Backend]:
    import shutil

    return [backend for backend in (Backend.CLAUDE, Backend.CODEX) if shutil.which(backend.value)]


def _resolve_setup_backend(explicit: Backend | None, config_backend: Backend | None) -> Backend:
    """Pick the backend for `yikes setup` without assuming claude.

    An explicit ``-b`` or a project's configured backend wins. Otherwise use the
    only installed backend, and when both claude and codex are present, ask.
    """
    if explicit is not None:
        return explicit
    if config_backend is not None:
        return config_backend
    available = _available_backends()
    if len(available) == 1:
        return available[0]
    if len(available) >= 2 and sys.stdin.isatty():
        from . import interactive

        choice = interactive.select(
            "Both claude and codex are installed — which should set up this project?",
            [("claude", "claude"), ("codex", "codex")],
        )
        return Backend(choice) if choice else Backend.CLAUDE
    return available[0] if available else Backend.CLAUDE


@contextlib.contextmanager
def _progress(label: str):
    """Show a live `label … Ns` spinner on a daemon thread while a call runs."""
    if not sys.stderr.isatty():
        typer.echo(f"{label} ...", err=True)
        yield
        return
    stop = threading.Event()
    start = time.monotonic()
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def spin() -> None:
        i = 0
        while not stop.is_set():
            elapsed = int(time.monotonic() - start)
            sys.stderr.write(f"\r{frames[i % len(frames)]} {label} … {elapsed}s ")
            sys.stderr.flush()
            i += 1
            stop.wait(0.1)

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def _seed_session(ref: str, message: str) -> None:
    """Pre-fill a freshly created session with an initial prompt (best effort)."""
    from .session_inventory import TmuxSessionController

    controller = TmuxSessionController()
    try:
        with _progress("preparing session"):
            controller.wait(ref, timeout=20)
        controller.send(ref, message, submit=False)
        typer.echo(_fmt("initial prompt pre-filled — review it and press Enter to send", "muted"))
    except Exception as exc:  # best effort: never block the attach on a seed hiccup
        typer.echo(f"yikes: could not pre-fill the prompt ({exc}); type it after attaching", err=True)


def _launch_host(
    backend: Backend, project_dir: Path, name: str, *, new: bool, model: str | None, message: str | None = None
) -> None:
    from .session_inventory import SessionLifecycle, TmuxSessionController

    result = TmuxSessionController().start(name, backend=backend, cwd=project_dir, model=model, replace=new)
    action = "replaced" if result.replaced else "started" if result.created else "reattaching"
    typer.echo(f"{_fmt(action, 'header')}: {name} ({backend.value}) @ {project_dir}")
    if message and result.created:
        _seed_session(name, message)
    command = SessionLifecycle().attach_command(name)
    if command is None:
        typer.echo(f"yikes: could not attach to {name}", err=True)
        raise typer.Exit(1)
    os.execvp(command[0], command)


def _launch_docker(
    backend: Backend,
    project_dir: Path,
    name: str,
    *,
    new: bool,
    model: str | None,
    ports: tuple[tuple[str, str], ...],
    message: str | None = None,
) -> None:
    from .drivers import ensure_interactive_session
    from .session_inventory import SessionLifecycle

    session_id = _docker_session_id(backend, project_dir)
    lifecycle = SessionLifecycle()
    existed = lifecycle.resolve_session_id(session_id) is not None
    if new:
        lifecycle.close(session_id)
        existed = False
    options = ChatOptions(
        backend=backend,
        driver=Driver.DOCKER,
        cwd=project_dir,
        cwd_explicit=True,
        model=model,
        settings=AgentSettings(tmux_enabled=True, managed_output_enabled=False, docker_ports=ports),
        session_id=session_id,
    )
    typer.echo(f"{_fmt('starting', 'header')} isolated {backend.value} session ({name}) for {project_dir} ...")
    ensure_interactive_session(options)
    for url in _published_urls(session_id):
        typer.echo(f"  {_fmt(url, 'success')}")
    if message and not existed:
        _seed_session(session_id, message)
    command = lifecycle.attach_command(session_id)
    if command is None:
        typer.echo("yikes: could not attach to the container session", err=True)
        raise typer.Exit(1)
    os.execvp(command[0], command)


def _docker_session_id(backend: Backend, project_dir: Path) -> str:
    digest = hashlib.sha1(f"{backend.value}:{project_dir.resolve()}".encode()).hexdigest()
    return f"ykd{digest[:13]}"


def _published_urls(session_id: str) -> list[str]:
    from .sandbox import SandboxManager
    from .session_inventory import SessionLifecycle

    resolved = SessionLifecycle().resolve_session_id(session_id) or session_id
    sandbox = SandboxManager().get(resolved)
    if sandbox is None:
        return []
    published = sandbox.meta.user_data.get("published_ports", "")
    return [f"http://localhost:{entry.split(':', 1)[0]}" for entry in published.split(",") if entry]


def _sanitize_session_name(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")[:80]
    return cleaned or "session"


def _fmt(text: str, kind: str) -> str:
    """Apply a named style from interactive's palette, but only on a TTY."""
    if not sys.stdout.isatty():
        return text
    from . import interactive

    return getattr(interactive, kind)(text)


def _run_menu() -> None:
    if not sys.stdin.isatty():
        app(args=["tui"], standalone_mode=False)
        return
    from . import interactive

    interactive.clear_screen()
    target = interactive.select(
        "yikes! — what would you like to start?",
        [
            ("claude", "claude — interactive Claude session for this directory"),
            ("codex", "codex — interactive Codex session for this directory"),
            ("tui", "terminal overview (dashboard)"),
        ],
    )
    if target is None:
        return
    app(args=[target], standalone_mode=False)


def _has_settings_cli(
    web_search: bool | None,
    tmux: bool | None,
    capture: bool | None,
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> bool:
    return web_search is not None or tmux is not None or capture is not None or bool(read_dir) or bool(write_dir) or bool(mcp)


def _settings_from_cli(
    web_search: bool | None,
    tmux: bool | None,
    capture: bool | None,
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> AgentSettings:
    managed_output = False if tmux is True and capture is None else (True if capture is None else capture)
    return AgentSettings(
        web_search_enabled=True if web_search is None else web_search,
        tmux_enabled=False if tmux is None else tmux,
        managed_output_enabled=managed_output,
        read_roots=tuple(path.expanduser() for path in (read_dir or ())),
        write_roots=tuple(path.expanduser() for path in (write_dir or ())),
        mcp_servers=tuple(_parse_mcp_spec(spec) for spec in (mcp or ())),
    )


def _parse_mcp_spec(spec: str) -> McpServer:
    name, sep, command = spec.partition("=")
    if not sep or not name.strip() or not command.strip():
        raise YikesError("MCP specs must look like name='command arg...'")
    parts = shlex.split(command)
    if not parts:
        raise YikesError("MCP specs must include a command")
    return McpServer(name.strip(), parts[0], tuple(parts[1:]))


def _parse_tui_driver(value: str | None) -> Driver | None:
    if value is None:
        return None
    try:
        driver = Driver(value)
    except ValueError as exc:
        raise YikesError("interactive driver must be direct, tmux, or docker") from exc
    if driver is Driver.REMOTE_CONTROL:
        raise YikesError("remote-control is not an interactive chat mode; use direct, tmux, or docker")
    return driver


if __name__ == "__main__":
    raise SystemExit(main())
