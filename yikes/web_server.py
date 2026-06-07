from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app_core import YikesAppController
from .web import create_app
from .web_auth import WebAuthConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the yikes! web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8760)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--dev", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--persistent-auth", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    # Use the same user-global auth store as `yikes web` / the launcher
    # (~/.yikes/web-auth.env). Reading auth from the project's .env instead made
    # this entry point sign cookies with a different secret/key, so restarting
    # via one path logged you out of the other.
    auth = WebAuthConfig.load(
        developer_mode=bool(args.dev),
        persist_auth=bool(args.persistent_auth),
    )
    app = create_app(YikesAppController(cwd=cwd), auth=auth)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
