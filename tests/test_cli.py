"""Tests for the CLI."""

from __future__ import annotations

import json
import runpy
import sqlite3
import sys
from importlib import metadata
from pathlib import Path

import pytest

from ruperto import get_version, main

WEB_CHAT_PORT = 9000


def test_main():
    """Basic CLI test."""
    assert main([]) == 0


def test_show_help(capsys: pytest.CaptureFixture):
    """Show help.

    Parameters:
        capsys: Pytest fixture to capture output.
    """
    with pytest.raises(SystemExit):
        main(["-h"])
    captured = capsys.readouterr()
    assert "ruperto" in captured.out


def test_show_version(mocker, capsys: pytest.CaptureFixture):
    """Show version.

    Parameters:
        mocker: pytest-mock fixture to patch get_version.
        capsys: Pytest fixture to capture output.
    """
    mocker.patch("ruperto.get_version", return_value="0.1.0")
    with pytest.raises(SystemExit):
        main(["-V"])
    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_main_module(mocker):
    """Test running the CLI via __main__ (python -m ...)."""
    module_name = "ruperto.__main__"
    # Simulate: python -m ruperto --version
    mocker.patch.object(sys, "argv", ["ruperto", "-V"])
    with pytest.raises(SystemExit):
        runpy.run_module(module_name, run_name="__main__", alter_sys=False)


def test_get_version_package_not_found(mocker):
    """Test get_version returns 'unknown' if package is not found."""
    mocker.patch(
        "importlib.metadata.version",
        side_effect=metadata.PackageNotFoundError("not found"),
    )
    assert get_version() == "unknown"


def test_show_settings(monkeypatch, capsys: pytest.CaptureFixture):
    """The public settings snapshot is printed as JSON."""
    monkeypatch.setenv("RUPERTO_STORE_NAME", "Pizza Planet")
    assert main(["show-settings"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["store_name"] == "Pizza Planet"


def test_init_db(monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    """The database bootstrap command creates the first table."""
    database_path = tmp_path / "ruperto.db"
    monkeypatch.setenv("RUPERTO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    assert main(["init-db"]) == 0
    captured = capsys.readouterr()
    assert str(database_path) in captured.out

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {row[0] for row in rows}
    assert "store_profile" in tables


def test_create_admin_interactively(monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture):
    """The create-admin command can prompt for one owner user interactively."""
    database_path = tmp_path / "admin.db"
    monkeypatch.setenv("RUPERTO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    prompts = iter(["", "gaitan@gmail.com", ""])
    passwords = iter(["clave-1", "clave-2", "", "secreta", "secreta"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(prompts))
    monkeypatch.setattr("ruperto.getpass.getpass", lambda _prompt="": next(passwords))

    assert main(["create-admin"]) == 0
    captured = capsys.readouterr()
    assert "Admin user ready for gaitan@gmail.com" in captured.out
    assert "This field is required." in captured.out
    assert "Passwords do not match." in captured.out
    assert "Password cannot be empty." in captured.out

    with sqlite3.connect(database_path) as connection:
        user_row = connection.execute("SELECT email, full_name FROM staff_user").fetchone()
        membership_row = connection.execute("SELECT store_id, role FROM store_membership").fetchone()

    assert user_row == ("gaitan@gmail.com", "Store Admin")
    assert membership_row == (1, "OWNER")


def test_web_chat_command_starts_dev_server(monkeypatch, mocker, tmp_path: Path):
    """The web-chat subcommand starts the development web UI."""
    database_path = tmp_path / "web-chat.db"
    monkeypatch.setenv("RUPERTO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    run_web_chat = mocker.patch("ruperto.run_web_chat", return_value=0)

    assert main(["web-chat", "--host", "0.0.0.0", "--port", str(WEB_CHAT_PORT)]) == 0
    run_web_chat.assert_called_once()
    assert run_web_chat.call_args.kwargs["host"] == "0.0.0.0"
    assert run_web_chat.call_args.kwargs["port"] == WEB_CHAT_PORT
