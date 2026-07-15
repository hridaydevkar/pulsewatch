"""
cli.py
Command-line interface for pulsewatch.

  pulsewatch init                      create a default config.yaml here
  pulsewatch serve                     start the FastAPI app + a check worker
  pulsewatch worker --region X         start an extra check worker (region X)
  pulsewatch add --name X --url Y      append a service to config.yaml
"""

import os
from pathlib import Path

import click
import yaml

from pulsewatch import __version__
from pulsewatch.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG,
    DEFAULT_REGION,
    REGION_ENV,
    configured_regions,
    find_config_path,
    load_config,
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
@click.option("--region", default=DEFAULT_REGION, show_default=True,
              help="Region name this server's built-in check worker reports as.")
@click.option("--reload", is_flag=True,
              help="Auto-reload on code changes (development).")
def serve(host, port, region, reload):
    """Start the FastAPI app plus a built-in check worker for one region.

    Run additional regions as separate processes with `pulsewatch worker`.
    """
    import uvicorn

    os.environ[REGION_ENV] = region  # picked up by the app's lifespan
    # Import string (not the app object) so --reload can re-import cleanly.
    uvicorn.run("pulsewatch.main:app", host=host, port=port, reload=reload)


@main.command()
@click.option("--region", required=True,
              help="Region name to tag this worker's check results with.")
def worker(region):
    """Run a standalone check worker for one region (no web server).

    Point several workers (each a separate process, distinct --region) at the
    same config.yaml / database to monitor from multiple 'regions'.
    """
    import threading
    from pulsewatch.database import Base, engine, ensure_columns
    from pulsewatch.monitor import sync_services_from_config, start_scheduler

    config = load_config()
    declared = configured_regions(config)
    if region not in declared:
        click.echo(
            f"note: region {region!r} isn't in config `regions:` "
            f"({', '.join(declared)}) — running it anyway.",
            err=True,
        )

    Base.metadata.create_all(bind=engine)
    ensure_columns()
    sync_services_from_config(config)
    scheduler = start_scheduler(config, region=region)

    click.echo(f"pulsewatch worker running for region '{region}' — Ctrl+C to stop.")
    stop = threading.Event()
    try:
        while not stop.wait(1.0):
            pass
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)


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
