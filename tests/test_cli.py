"""
Tests for the click CLI (pulsewatch.cli): init / add / serve / worker.

CliRunner.isolated_filesystem gives each test a throwaway cwd, and
config.USER_CONFIG_PATH is redirected to a non-existent temp path so the
per-user config fallback can never interfere with the assertions.
"""

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from pulsewatch import cli, config
from pulsewatch.config import configured_regions
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


def test_serve_passes_region_via_env(runner, monkeypatch):
    monkeypatch.delenv("PULSEWATCH_REGION", raising=False)
    seen = {}
    # capture the env var at the moment serve hands off to uvicorn
    monkeypatch.setattr("uvicorn.run",
                        lambda *a, **k: seen.update(region=os.environ.get("PULSEWATCH_REGION")))
    try:
        result = runner.invoke(main, ["serve", "--region", "us-east"])
        assert result.exit_code == 0
        assert seen["region"] == "us-east"
    finally:
        os.environ.pop("PULSEWATCH_REGION", None)


def test_worker_starts_scheduler_for_region_and_stops(monkeypatch):
    import pulsewatch.database as database
    import pulsewatch.monitor as monitor

    fake_scheduler = MagicMock()
    monkeypatch.setattr(cli, "load_config",
                        lambda: {"services": [], "regions": [{"name": "us-east"}]})
    # keep it off any real DB / scheduler
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda **k: None)
    monkeypatch.setattr(database, "ensure_columns", lambda: None)
    monkeypatch.setattr(monitor, "sync_services_from_config", lambda cfg: None)
    monkeypatch.setattr(monitor, "start_scheduler",
                        lambda cfg, region: fake_scheduler)
    # make the block-forever loop return immediately
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: True)

    result = CliRunner().invoke(main, ["worker", "--region", "us-east"])
    assert result.exit_code == 0
    assert "region 'us-east'" in result.output
    fake_scheduler.shutdown.assert_called_once()


def test_configured_regions():
    assert configured_regions({"services": []}) == ["local"]        # default
    assert configured_regions({"regions": []}) == ["local"]         # empty -> default
    assert configured_regions(
        {"regions": [{"name": "us-east"}, {"name": "eu-west"}]}) == ["us-east", "eu-west"]
    assert configured_regions({"regions": ["plain-string"]}) == ["plain-string"]
