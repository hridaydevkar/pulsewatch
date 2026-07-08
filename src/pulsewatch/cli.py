"""
cli.py
Command-line interface for pulsewatch.

  pulsewatch init                      create a default config.yaml here
  pulsewatch serve                     start the FastAPI app + monitor
  pulsewatch add --name X --url Y      append a service to config.yaml
"""

from pathlib import Path

import click
import yaml

from pulsewatch import __version__
from pulsewatch.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG,
    find_config_path,
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pulsewatch")
def main():
    """pulsewatch - self-hosted uptime monitoring & status page."""


@main.command()
@click.option("--force", is_flag=True, help="Overwrite an existing config.yaml.")
def init(force):
    """Create a default config.yaml in the current directory."""
    target = Path.cwd() / CONFIG_FILENAME
    if target.exists() and not force:
        raise click.ClickException(
            f"{target} already exists. Use --force to overwrite."
        )
    target.write_text(DEFAULT_CONFIG)
    click.echo(f"Wrote default config to {target}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host interface to bind.")
@click.option("--port", default=8000, show_default=True, type=int,
              help="Port to bind.")
@click.option("--reload", is_flag=True,
              help="Auto-reload on code changes (development).")
def serve(host, port, reload):
    """Start the FastAPI app and background monitor."""
    import uvicorn

    # Import string (not the app object) so --reload can re-import cleanly.
    uvicorn.run("pulsewatch.main:app", host=host, port=port, reload=reload)


@main.command()
@click.option("--name", required=True, help="Human-readable service name.")
@click.option("--url", required=True, help="URL to health-check.")
@click.option("--interval", "check_interval_seconds", default=30,
              show_default=True, type=int, help="Seconds between checks.")
@click.option("--threshold", "failure_threshold", default=3, show_default=True,
              type=int, help="Consecutive failures before an incident opens.")
def add(name, url, check_interval_seconds, failure_threshold):
    """Append a service to config.yaml (creates one here if none exists)."""
    path = find_config_path()
    if path is None:
        path = Path.cwd() / CONFIG_FILENAME
        config = {"services": [], "discord_webhook_url": ""}
    else:
        config = yaml.safe_load(path.read_text()) or {}

    services = config.get("services") or []
    if any(isinstance(s, dict) and s.get("name") == name for s in services):
        raise click.ClickException(
            f"A service named {name!r} already exists in {path}."
        )

    services.append({
        "name": name,
        "url": url,
        "check_interval_seconds": check_interval_seconds,
        "failure_threshold": failure_threshold,
    })
    config["services"] = services
    config.setdefault("discord_webhook_url", "")

    path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False))
    click.echo(f"Added {name!r} ({url}) to {path}")


if __name__ == "__main__":
    main()
