import json
import shlex
import sys
import asyncio
import os
from pathlib import Path

from .domain import AgentSettings, Backend, Complexity, Driver, McpServer
from .errors import YikesError
from .events import DEFAULT_EVENT_STORE, EventLog
from .runtime import DEFAULT_RUNTIME_STORE
from .sandbox import DEFAULT_SANDBOX_STORE
from .services import ChatService
from .session_inventory import SessionInventory, SessionLifecycle
from .tokens import DEFAULT_TOKEN_STORE

try:
    import typer
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal dev envs
    typer = None


if typer:
    app = typer.Typer(
        add_completion=False,
        help="Yikes terminal app and chatbot smoke tools.",
    )

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
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        try:
            settings = _settings_from_cli(web_search, tmux, read_dir, write_dir, mcp)
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
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
    ) -> None:
        from .tui import run_tui

        parsed_driver = _parse_tui_driver(driver)
        settings = _settings_from_cli(web_search, tmux, read_dir, write_dir, mcp) if _has_settings_cli(web_search, tmux, read_dir, write_dir, mcp) else None
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
        session_id: str = typer.Argument(..., help="Yikes session ID to close."),
        runtime_store: Path = typer.Option(DEFAULT_RUNTIME_STORE, "--runtime-store", help="Durable session store."),
        sandbox_store: Path = typer.Option(DEFAULT_SANDBOX_STORE, "--sandbox-store", help="Docker sandbox store."),
    ) -> None:
        result = SessionLifecycle(runtime_store=runtime_store, sandbox_store=sandbox_store).close(session_id)
        typer.echo(result.message)
        if not result.closed:
            raise typer.Exit(1)

    @app.command("attach")
    def attach(
        session_id: str = typer.Argument(..., help="Yikes session ID to overtake."),
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
    ) -> None:
        from .remote import RemoteCommandHandler, RemoteServerConfig, YikesRemoteServer
        from .tokens import TokenStore

        tokens = TokenStore(token_store)
        if auth and not tokens.list_tokens():
            typer.echo(
                f"No bearer tokens found in {tokens.path}. Create one with: yikes token --store {tokens.path}",
                err=True,
            )
        config = RemoteServerConfig(host=host, port=port, require_token=auth)
        handler = RemoteCommandHandler(token_store=tokens, event_log=EventLog(event_store), require_token=auth)
        remote_server = YikesRemoteServer(handler, config)
        typer.echo(f"Yikes server listening on {config.websocket_url} (auth: {'on' if auth else 'off'})")
        asyncio.run(remote_server.serve_forever())


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        argv = ["tui"]
    if typer:
        try:
            app(args=argv, standalone_mode=False)
            return 0
        except typer.Exit as exc:
            return int(exc.exit_code)
        except YikesError as exc:
            print(f"yikes: {exc}", file=sys.stderr)
            return 1

    print("yikes requires the 'typer' dependency. Install the package first: poetry install", file=sys.stderr)
    return 1


def _has_settings_cli(
    web_search: bool | None,
    tmux: bool | None,
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> bool:
    return web_search is not None or tmux is not None or bool(read_dir) or bool(write_dir) or bool(mcp)


def _settings_from_cli(
    web_search: bool | None,
    tmux: bool | None,
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> AgentSettings:
    return AgentSettings(
        web_search_enabled=True if web_search is None else web_search,
        tmux_enabled=False if tmux is None else tmux,
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
