import json
import shlex
import sys
from pathlib import Path

from .domain import AgentSettings, Backend, Complexity, Driver, McpServer
from .errors import YikesError
from .services import ChatService

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
        cwd: Path = typer.Option(Path.cwd(), "--cwd"),
        timeout: float = typer.Option(180.0, "--timeout"),
        model: str | None = typer.Option(None, "--model"),
        complexity: Complexity = typer.Option(Complexity.MEDIUM, "--complexity"),
        web_search: bool = typer.Option(True, "--web-search/--no-web-search"),
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        try:
            settings = _settings_from_cli(web_search, read_dir, write_dir, mcp)
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
        cwd: Path = typer.Option(Path.cwd(), "--cwd"),
        timeout: float = typer.Option(180.0, "--timeout"),
        model: str | None = typer.Option(None, "--model"),
        complexity: Complexity | None = typer.Option(None, "--complexity"),
        web_search: bool | None = typer.Option(None, "--web-search/--no-web-search"),
        read_dir: list[Path] | None = typer.Option(None, "--read-dir"),
        write_dir: list[Path] | None = typer.Option(None, "--write-dir"),
        mcp: list[str] | None = typer.Option(None, "--mcp"),
    ) -> None:
        from .tui import run_tui

        parsed_driver = _parse_tui_driver(driver)
        settings = _settings_from_cli(web_search, read_dir, write_dir, mcp) if _has_settings_cli(web_search, read_dir, write_dir, mcp) else None
        run_tui(
            backend=backend,
            driver=parsed_driver,
            cwd=cwd,
            timeout=timeout,
            model=model,
            complexity=complexity,
            settings=settings,
        )


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
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> bool:
    return web_search is not None or bool(read_dir) or bool(write_dir) or bool(mcp)


def _settings_from_cli(
    web_search: bool | None,
    read_dir: list[Path] | None,
    write_dir: list[Path] | None,
    mcp: list[str] | None,
) -> AgentSettings:
    return AgentSettings(
        web_search_enabled=True if web_search is None else web_search,
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
        raise YikesError("interactive driver must be direct or tmux") from exc
    if driver is Driver.REMOTE_CONTROL:
        raise YikesError("remote-control is not an interactive chat mode; use direct or tmux")
    return driver


if __name__ == "__main__":
    raise SystemExit(main())
