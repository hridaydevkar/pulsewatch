"""
Tests for the click CLI (pulsewatch.cli): init / add / serve.

CliRunner.isolated_filesystem gives each test a throwaway cwd, and
config.USER_CONFIG_PATH is redirected to a non-existent temp path so the
per-user config fallback can never interfere with the assertions.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from pulsewatch import config
from pulsewatch.cli import main


@pytest.fixture
def runner(monkeypatch, tmp_path):
    # Ensure the ~/.pulsewatch fallback never matches during tests.
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "nope" / "config.yaml")
    return CliRunner()


def test_bare_invocation_shows_help(runner):
    result = runner.invoke(main, [])
    assert "Usage:" in result.output
    for cmd in ("init", "serve", "add"):
        assert cmd in result.output


def test_version(runner):
    from pulsewatch import __version__
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_init_creates_config(runner):
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0
        cfg = yaml.safe_load(Path("config.yaml").read_text())
        assert [s["name"] for s in cfg["services"]] == ["Example API", "Flaky Demo Service"]


def test_init_refuses_to_overwrite_without_force(runner):
    with runner.isolated_filesystem():
        Path("config.yaml").write_text("services: []\n")
        result = runner.invoke(main, ["init"])
        assert result.exit_code != 0
        assert "already exists" in result.output
        # untouched
        assert yaml.safe_load(Path("config.yaml").read_text()) == {"services": []}


def test_init_force_overwrites(runner):
    with runner.isolated_filesystem():
        Path("config.yaml").write_text("services: []\n")
        result = runner.invoke(main, ["init", "--force"])
        assert result.exit_code == 0
        cfg = yaml.safe_load(Path("config.yaml").read_text())
        assert len(cfg["services"]) == 2


def test_add_creates_config_when_missing(runner):
    with runner.isolated_filesystem():
        result = runner.invoke(main, [
            "add", "--name", "My API", "--url", "https://api.example/health",
            "--interval", "15", "--threshold", "5",
        ])
        assert result.exit_code == 0
        cfg = yaml.safe_load(Path("config.yaml").read_text())
        assert cfg["services"] == [{
            "name": "My API", "url": "https://api.example/health",
            "check_interval_seconds": 15, "failure_threshold": 5,
        }]


def test_add_appends_to_existing_config(runner):
    with runner.isolated_filesystem():
        Path("config.yaml").write_text(yaml.safe_dump({
            "services": [{"name": "Existing", "url": "http://x"}],
            "discord_webhook_url": "",
        }))
        result = runner.invoke(main, ["add", "--name", "New", "--url", "http://y"])
        assert result.exit_code == 0
        cfg = yaml.safe_load(Path("config.yaml").read_text())
        names = [s["name"] for s in cfg["services"]]
        assert names == ["Existing", "New"]


def test_add_rejects_duplicate_name(runner):
    with runner.isolated_filesystem():
        runner.invoke(main, ["add", "--name", "Dup", "--url", "http://a"])
        result = runner.invoke(main, ["add", "--name", "Dup", "--url", "http://b"])
        assert result.exit_code != 0
        assert "already exists" in result.output


def test_serve_invokes_uvicorn_with_import_string(runner):
    with patch("uvicorn.run") as run:
        result = runner.invoke(main, ["serve", "--host", "0.0.0.0", "--port", "9999"])
    assert result.exit_code == 0
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == "pulsewatch.main:app"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9999
    assert kwargs["reload"] is False
