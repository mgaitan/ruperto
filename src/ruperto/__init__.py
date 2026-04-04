"""Core package entrypoints for the Ruperto service."""

from __future__ import annotations

import argparse
import asyncio
import getpass
from importlib import metadata

from ruperto.auth import normalize_email
from ruperto.config import Settings
from ruperto.db import create_database_runtime, init_database
from ruperto.dev_web import run_web_chat
from ruperto.models import StaffRole
from ruperto.repository import BusinessRepository


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
    subparsers.add_parser("create-admin", help="Create or update one dashboard admin interactively.")
    subparsers.add_parser("show-settings", help="Print the effective public settings.")
    web_chat = subparsers.add_parser("web-chat", help="Run the development web chat UI.")
    web_chat.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    web_chat.add_argument("--port", default=7932, type=int, help="TCP port for the development web UI.")
    return parser


def prompt_required_text(prompt: str, *, default: str | None = None, normalize: bool = False) -> str:
    """Prompt until the user provides one non-empty value."""
    while True:
        raw_value = input(prompt).strip()
        if raw_value:
            return normalize_email(raw_value) if normalize else raw_value
        if default is not None:
            return default
        print("This field is required.")


def prompt_password() -> str:
    """Prompt for a password twice until both entries match."""
    while True:
        password = getpass.getpass("Password: ")
        if not password:
            print("Password cannot be empty.")
            continue
        confirmation = getpass.getpass("Password (again): ")
        if password != confirmation:
            print("Passwords do not match. Please try again.")
            continue
        return password


async def create_admin_user(*, settings: Settings, email: str, full_name: str, password: str) -> None:
    """Create or update one owner user for the configured default store."""
    runtime = await init_database(settings=settings)
    try:
        async with runtime.session_factory() as session:
            repository = BusinessRepository(session)
            await repository.ensure_staff_user(
                email=email,
                full_name=full_name,
                password=password,
                store_id=settings.default_store_id,
                role=StaffRole.OWNER,
            )
            await session.commit()
    finally:
        await runtime.engine.dispose()


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

    if namespace.command == "create-admin":
        email = prompt_required_text("Email: ", normalize=True)
        full_name = prompt_required_text("Full name [Store Admin]: ", default="Store Admin")
        password = prompt_password()
        asyncio.run(create_admin_user(settings=settings, email=email, full_name=full_name, password=password))
        print(f"Admin user ready for {email} in store {settings.default_store_id}")
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
