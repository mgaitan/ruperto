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
