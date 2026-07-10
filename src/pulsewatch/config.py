"""
config.py
Locates and loads pulsewatch's config.yaml.

Resolution order:
  1. ./config.yaml                 (current working directory)
  2. ~/.pulsewatch/config.yaml     (per-user fallback)

This lets you keep a project-local config while still supporting a
user-wide config for a global install.
"""

from pathlib import Path

import yaml

CONFIG_FILENAME = "config.yaml"
USER_CONFIG_DIR = Path.home() / ".pulsewatch"
USER_CONFIG_PATH = USER_CONFIG_DIR / CONFIG_FILENAME

# The config written by `pulsewatch init`. Mirrors the shipped demo config so a
# fresh install has something that immediately shows incidents opening/closing.
DEFAULT_CONFIG = """\
# List the services you want to monitor here.
# check_interval_seconds: how often to ping this service
# failure_threshold: consecutive failures before an incident is opened

services:
  - name: "Example API"
    url: "https://httpbin.org/status/200"
    check_interval_seconds: 30
    failure_threshold: 3

  - name: "Flaky Demo Service"
    url: "https://httpbin.org/status/200,500"
    check_interval_seconds: 30
    failure_threshold: 3

# Alert channels: one block per destination to notify on down/recovery events.
# With none enabled, alerts are just logged to the console.
alerts: []
  # - type: discord
  #   webhook_url: "https://discord.com/api/webhooks/..."
  # - type: slack
  #   webhook_url: "https://hooks.slack.com/services/..."
  # - type: email
  #   to: "oncall@example.com"
  #   from: "pulsewatch@example.com"    # optional; defaults to the SMTP user
  #   # SMTP host/port/credentials are read from environment variables:
  #   #   PULSEWATCH_SMTP_HOST, PULSEWATCH_SMTP_PORT (default 587),
  #   #   PULSEWATCH_SMTP_USER, PULSEWATCH_SMTP_PASSWORD
"""


def candidate_paths():
    """Config locations, in the order they are searched."""
    return [Path.cwd() / CONFIG_FILENAME, USER_CONFIG_PATH]


def find_config_path():
    """Return the first existing config path, or None if none exists."""
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


def load_config(path=None):
    """Load and parse the config.

    If ``path`` is given it is loaded directly; otherwise the config is
    auto-discovered using :func:`candidate_paths`. Raises FileNotFoundError
    with a helpful message when nothing is found.
    """
    if path is None:
        path = find_config_path()
        if path is None:
            searched = " -> ".join(str(p) for p in candidate_paths())
            raise FileNotFoundError(
                f"No {CONFIG_FILENAME} found (searched: {searched}). "
                "Run `pulsewatch init` to create one."
            )
    with open(path, "r") as f:
        return yaml.safe_load(f)
