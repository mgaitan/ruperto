"""Core package entrypoints for the Ruperto service."""

from __future__ import annotations

import argparse
import asyncio
from importlib import metadata

from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.dev_web import run_web_chat


def get_version() -> str:
    """Return the installed package version when available."""
    try:
        return metadata.version("ruperto")
    except metadata.PackageNotFoundError:
        return "unknown"


def get_parser() -> argparse.ArgumentParser:
    """Build the CLI parser used by `ruperto`."""
    parser = argparse.ArgumentParser(prog="ruperto", description="Bootstrap and inspect the Ruperto service.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {get_version()}")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init-db", help="Initialize the local database and bootstrap the store profile.")
    subparsers.add_parser("show-settings", help="Print the effective public settings.")
    web_chat = subparsers.add_parser("web-chat", help="Run the development web chat UI.")
    web_chat.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    web_chat.add_argument("--port", default=7932, type=int, help="TCP port for the development web UI.")
    return parser


def main(args: list[str] | None = None) -> int:
    """Run the main CLI entrypoint."""
    parser = get_parser()
    namespace = parser.parse_args(args=args)

    if namespace.command is None:
        parser.print_help()
        return 0

    settings = Settings()

    if namespace.command == "init-db":
        runtime = asyncio.run(init_database(settings=settings))
        asyncio.run(runtime.engine.dispose())
        print(f"Database initialized at {settings.database_url}")
        return 0

    if namespace.command == "web-chat":
        runtime = create_database_runtime(settings)
        if settings.auto_init_db:
            asyncio.run(init_database(settings=settings, runtime=runtime))
        try:
            return run_web_chat(
                settings=settings,
                session_factory=runtime.session_factory,
                host=namespace.host,
                port=namespace.port,
            )
        finally:
            asyncio.run(runtime.engine.dispose())

    print(settings.public_settings_json())
    return 0


__all__: list[str] = ["get_parser", "get_version", "main"]
