import json
import re
import shlex
import sys
import asyncio
import hashlib
import os
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

    @app.command("claude")
    def claude(
        name: str | None = typer.Option(None, "--name", "-n", help="Session name. Default: directory basename."),
        isolated: bool | None = typer.Option(None, "--isolated/--no-isolated", "-i/-I", help="Run isolated in Docker."),
        new: bool = typer.Option(False, "--new", help="Replace any existing session with this name."),
        model: str | None = typer.Option(None, "--model", help="Backend model name."),
        port: list[str] | None = typer.Option(None, "--port", "-p", help="Publish a port when isolated (HOST or HOST:CONTAINER). Repeatable."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the pre-flight panel prompt and start immediately."),
    ) -> None:
        _launch(Backend.CLAUDE, name=name, isolated=isolated, new=new, model=model, port=port, cwd=cwd, yes=yes)

    @app.command("codex")
    def codex(
        name: str | None = typer.Option(None, "--name", "-n", help="Session name. Default: directory basename."),
        isolated: bool | None = typer.Option(None, "--isolated/--no-isolated", "-i/-I", help="Run isolated in Docker."),
        new: bool = typer.Option(False, "--new", help="Replace any existing session with this name."),
        model: str | None = typer.Option(None, "--model", help="Backend model name."),
        port: list[str] | None = typer.Option(None, "--port", "-p", help="Publish a port when isolated (HOST or HOST:CONTAINER). Repeatable."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the pre-flight panel prompt and start immediately."),
    ) -> None:
        _launch(Backend.CODEX, name=name, isolated=isolated, new=new, model=model, port=port, cwd=cwd, yes=yes)

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
        typer.echo(f"wrote {target}")

    @app.command("setup")
    def setup(
        backend: Backend | None = typer.Option(None, "--backend", "-b", help="Backend to run the scan. Default: yikes.toml or claude."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Project directory. Default: current directory."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Write yikes.toml without confirmation."),
    ) -> None:
        from .project_config import load_project_config

        project_dir = (cwd or Path.cwd()).expanduser()
        resolved_backend = backend or _config_backend(load_project_config(project_dir)) or Backend.CLAUDE
        if not _run_setup(resolved_backend, project_dir, assume_yes=yes):
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
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        results = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store).close_all(
            runtime=runtime,
            backend=backend,
        )
        for result in results:
            typer.echo(result.message)
        typer.echo(f"Closed {sum(1 for result in results if result.closed)}/{len(results)} sessions.")

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
        host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
        port: int = typer.Option(8760, "--port", "-p", help="HTTP port."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Default start directory for new sessions."),
        dev: bool = typer.Option(False, "--dev/--no-dev", help="Enable development reload endpoints."),
        persistent_auth: bool = typer.Option(True, "--persistent-auth/--ephemeral-auth", help="Reuse the local login key across restarts."),
        open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the web UI in the default browser."),
    ) -> None:
        import threading
        import time
        import webbrowser

        import uvicorn

        from .app_core import YikesAppController
        from .web import create_app
        from .web_auth import WebAuthConfig

        root = (cwd or Path.cwd()).expanduser()
        auth_config = WebAuthConfig.load(developer_mode=dev, env_path=root / ".env", persist_auth=persistent_auth)
        url = auth_config.login_url(host=host, port=port)
        app_instance = create_app(YikesAppController(cwd=root), auth=auth_config)
        if open_browser:
            threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
        typer.echo(f"yikes! web UI listening on http://{host}:{port}/")
        typer.echo(f"login URL: {url}")
        uvicorn.run(app_instance, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        argv = ["menu"]
    if typer:
        try:
            result = app(args=argv, standalone_mode=False)
            return result if isinstance(result, int) else 0
        except typer.Exit as exc:
            return int(exc.exit_code)
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
    yes: bool = False,
) -> None:
    from .project_config import load_project_config, normalize_port

    project_dir = (cwd or Path.cwd()).expanduser()
    while True:
        config = load_project_config(project_dir)
        use_isolated = config.isolated if isolated is None else isolated
        resolved_model = model or config.model
        session_name = _sanitize_session_name(name or config.name or project_dir.resolve().name)
        ports = tuple(normalize_port(spec) for spec in port) if port else config.ports
        action = _preflight(
            backend,
            project_dir,
            session_name,
            isolated=use_isolated,
            ports=ports,
            config_source=str(config.source) if config.source else None,
            assume_yes=yes,
        )
        if action == "cancel":
            return
        if action == "setup":
            _run_setup(backend, project_dir, assume_yes=False)
            continue  # re-render the panel with the freshly written config
        break
    if use_isolated:
        _launch_docker(backend, project_dir, session_name, new=new, model=resolved_model, ports=ports)
    else:
        _launch_host(backend, project_dir, session_name, new=new, model=resolved_model)


def _preflight(
    backend: Backend,
    project_dir: Path,
    name: str,
    *,
    isolated: bool,
    ports: tuple[tuple[str, str], ...],
    config_source: str | None,
    assume_yes: bool,
) -> str:
    """Print the pre-flight panel and return "start", "setup", or "cancel"."""
    from .preflight import render_panel

    panel = render_panel(
        backend=backend.value,
        name=name,
        location="docker" if isolated else "host",
        cwd=str(project_dir),
        reused=_session_exists(backend, project_dir, name, isolated=isolated),
        config_source=config_source,
        ports=ports,
        isolated=isolated,
    )
    typer.echo(panel)
    if assume_yes or os.environ.get("YIKES_NO_PROMPT") or not sys.stdin.isatty():
        return "start"
    choice = input("> ").strip().lower()
    if choice in ("", "y", "yes", "start"):
        return "start"
    if choice in ("s", "setup", "scan"):
        return "setup"
    return "cancel"


def _run_setup(backend: Backend, project_dir: Path, *, assume_yes: bool) -> bool:
    """Scan the repo via the backend and write yikes.toml. Returns True on write."""
    from .drivers import ask_backend
    from .preflight import SCAN_PROMPT, parse_scan_result, ports_from_scan, synthesize_config
    from .project_config import CONFIG_NAME

    typer.echo(f"scanning {project_dir} with {backend.value} ...")
    try:
        reply = ask_backend(
            backend,
            Driver.DIRECT,
            SCAN_PROMPT,
            cwd=project_dir,
            timeout=180.0,
            model=None,
            settings=AgentSettings(),
        )
        data = parse_scan_result(reply)
    except YikesError as exc:
        typer.echo(f"yikes: scan failed: {exc}", err=True)
        return False
    except ValueError as exc:
        typer.echo(f"yikes: could not read scan result: {exc}", err=True)
        return False
    ports = ports_from_scan(data)
    scan_backend = data.get("backend") if data.get("backend") in ("claude", "codex") else None
    content = synthesize_config(ports, scan_backend)
    typer.echo("\nproposed yikes.toml:\n")
    typer.echo(content)
    notes = data.get("notes")
    if isinstance(notes, str) and notes.strip():
        typer.echo(f"({notes.strip()})\n")
    target = project_dir / CONFIG_NAME
    if not assume_yes and sys.stdin.isatty():
        answer = input(f"write {target}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            typer.echo("skipped")
            return False
    target.write_text(content)
    typer.echo(f"wrote {target}")
    return True


def _session_exists(backend: Backend, project_dir: Path, name: str, *, isolated: bool) -> bool:
    from .session_inventory import SessionLifecycle

    ref = _docker_session_id(backend, project_dir) if isolated else name
    return SessionLifecycle().resolve_session_id(ref) is not None


def _config_backend(config: object) -> Backend | None:
    value = getattr(config, "backend", None)
    try:
        return Backend(value) if value else None
    except ValueError:
        return None


def _launch_host(backend: Backend, project_dir: Path, name: str, *, new: bool, model: str | None) -> None:
    from .session_inventory import SessionLifecycle, TmuxSessionController

    result = TmuxSessionController().start(name, backend=backend, cwd=project_dir, model=model, replace=new)
    action = "replaced" if result.replaced else "started" if result.created else "reattaching"
    typer.echo(f"{action}: {name} ({backend.value}) @ {project_dir}")
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
) -> None:
    from .drivers import ensure_interactive_session
    from .session_inventory import SessionLifecycle

    session_id = _docker_session_id(backend, project_dir)
    lifecycle = SessionLifecycle()
    if new:
        lifecycle.close(session_id)
    options = ChatOptions(
        backend=backend,
        driver=Driver.DOCKER,
        cwd=project_dir,
        cwd_explicit=True,
        model=model,
        settings=AgentSettings(tmux_enabled=True, managed_output_enabled=False, docker_ports=ports),
        session_id=session_id,
    )
    typer.echo(f"starting isolated {backend.value} session ({name}) for {project_dir} ...")
    ensure_interactive_session(options)
    for url in _published_urls(session_id):
        typer.echo(f"  {url}")
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


def _run_menu() -> None:
    if not sys.stdin.isatty():
        app(args=["tui"], standalone_mode=False)
        return
    typer.echo("yikes! — what would you like to start?")
    typer.echo("  [1] claude     interactive Claude session for this directory")
    typer.echo("  [2] codex      interactive Codex session for this directory")
    typer.echo("  [3] overview   full terminal dashboard")
    choice = input("> ").strip().lower()
    routes = {
        "1": "claude", "claude": "claude", "c": "claude",
        "2": "codex", "codex": "codex", "x": "codex",
        "3": "tui", "overview": "tui", "o": "tui", "": "tui",
    }
    target = routes.get(choice)
    if target is None:
        typer.echo(f"yikes: unknown choice {choice!r}", err=True)
        raise typer.Exit(1)
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
