"""
alerts.py
Pluggable alert-channel system.

Every channel implements AlertChannel.send(message). Channels are built from
config via build_channels(), and send_alert() fans a message out to all of
them. If a channel isn't configured (blank webhook, missing SMTP host) it
degrades gracefully to a console log, so the project still runs with zero
setup.

Built-in channels:
- DiscordChannel  webhook, posts {"content": message}
- SlackChannel    webhook, posts {"text": message}
- EmailChannel    smtplib; SMTP host/port/credentials come from env vars:
                    PULSEWATCH_SMTP_HOST      (required to actually send)
                    PULSEWATCH_SMTP_PORT      (default 587)
                    PULSEWATCH_SMTP_USER      (optional; enables login)
                    PULSEWATCH_SMTP_PASSWORD  (optional; enables login)
                    PULSEWATCH_SMTP_FROM      (fallback From address)
                    PULSEWATCH_SMTP_STARTTLS  (default "true")
"""

import os
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

import httpx


class AlertChannel(ABC):
    """A destination that a plain-text alert message can be sent to."""

    @abstractmethod
    def send(self, message: str) -> None:
        """Deliver ``message``. Must never raise: log and move on instead, so
        one broken channel can't stop the others or crash a scheduled check."""
        raise NotImplementedError


class WebhookChannel(AlertChannel):
    """Shared behaviour for webhook-based channels (Discord, Slack).

    Subclasses set ``channel_name`` and ``payload_key`` (the JSON field the
    service expects the message text in).
    """

    channel_name = "webhook"
    payload_key = "content"

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url or ""

    def send(self, message: str) -> None:
        if not self.webhook_url:
            print(f"[ALERT - {self.channel_name} webhook not configured] {message}")
            return
        try:
            httpx.post(self.webhook_url, json={self.payload_key: message}, timeout=5)
        except Exception as e:
            print(f"[ALERT] Failed to send {self.channel_name} alert: {e}")


class DiscordChannel(WebhookChannel):
    channel_name = "Discord"
    payload_key = "content"


class SlackChannel(WebhookChannel):
    channel_name = "Slack"
    payload_key = "text"


class EmailChannel(AlertChannel):
    """Sends alerts over SMTP. Routing (to/from) is configured in config.yaml;
    the SMTP server and credentials come from environment variables so secrets
    stay out of the config file."""

    def __init__(self, to_addr: str, from_addr: str = None,
                 subject_prefix: str = "[pulsewatch]"):
        self.to_addr = to_addr
        self.from_addr = from_addr
        self.subject_prefix = subject_prefix

    def send(self, message: str) -> None:
        host = os.environ.get("PULSEWATCH_SMTP_HOST")
        if not host or not self.to_addr:
            missing = "PULSEWATCH_SMTP_HOST" if not host else "a 'to' address"
            print(f"[ALERT - email not configured: set {missing}] {message}")
            return

        port = int(os.environ.get("PULSEWATCH_SMTP_PORT", "587"))
        user = os.environ.get("PULSEWATCH_SMTP_USER")
        password = os.environ.get("PULSEWATCH_SMTP_PASSWORD")
        use_tls = os.environ.get("PULSEWATCH_SMTP_STARTTLS", "true").lower() != "false"
        from_addr = (
            self.from_addr
            or os.environ.get("PULSEWATCH_SMTP_FROM")
            or user
            or "pulsewatch@localhost"
        )

        try:
            email = EmailMessage()
            # First line of the message, minus Discord/Slack markdown, as subject.
            headline = message.splitlines()[0].replace("**", "").strip()
            email["Subject"] = f"{self.subject_prefix} {headline}".strip()
            email["From"] = from_addr
            email["To"] = self.to_addr
            email.set_content(message)

            with smtplib.SMTP(host, port, timeout=10) as smtp:
                if use_tls:
                    smtp.starttls()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(email)
        except Exception as e:
            print(f"[ALERT] Failed to send email alert: {e}")


# Maps the `type:` field in config to a builder taking the channel's config dict.
_CHANNEL_BUILDERS = {
    "discord": lambda spec: DiscordChannel(spec.get("webhook_url", "")),
    "slack": lambda spec: SlackChannel(spec.get("webhook_url", "")),
    "email": lambda spec: EmailChannel(
        to_addr=spec.get("to"),
        from_addr=spec.get("from"),
        subject_prefix=spec.get("subject_prefix", "[pulsewatch]"),
    ),
}


def build_channels(config: dict) -> list:
    """Construct AlertChannel instances from a parsed config dict.

    Reads the ``alerts:`` list (each entry needs a ``type``). For backward
    compatibility, a top-level ``discord_webhook_url`` is still honored.
    """
    channels = []
    for spec in config.get("alerts") or []:
        ctype = str(spec.get("type", "")).lower()
        builder = _CHANNEL_BUILDERS.get(ctype)
        if builder is None:
            raise ValueError(
                f"Unknown alert channel type: {spec.get('type')!r}. "
                f"Supported: {', '.join(sorted(_CHANNEL_BUILDERS))}."
            )
        channels.append(builder(spec))

    # Legacy single-Discord config.
    legacy = config.get("discord_webhook_url")
    if legacy:
        channels.append(DiscordChannel(legacy))

    return channels


def send_alert(channels, message: str) -> None:
    """Send ``message`` to every channel. With no channels, log to console."""
    if not channels:
        print(f"[ALERT - no channels configured] {message}")
        return
    for channel in channels:
        channel.send(message)


def _region_tag(region) -> str:
    """A ` [region: X]` suffix, omitted for the default single-worker region."""
    from pulsewatch.config import DEFAULT_REGION
    return f" [region: {region}]" if region and region != DEFAULT_REGION else ""


def down_message(service_name: str, error: str, is_dependency: bool = False,
                 region: str = None) -> str:
    tag = _region_tag(region)
    if is_dependency:
        return f"⚠️ Upstream dependency **{service_name}** is degraded{tag}. {error}"
    return f"🔴 Your service **{service_name}** is DOWN{tag}. Error: {error}"


def up_message(service_name: str, duration_seconds: float,
               is_dependency: bool = False, region: str = None) -> str:
    minutes = round(duration_seconds / 60, 1)
    tag = _region_tag(region)
    if is_dependency:
        return (f"✅ Upstream dependency **{service_name}** has recovered{tag}. "
                f"Was degraded for {minutes} minute(s).")
    return (f"🟢 Your service **{service_name}** has RECOVERED{tag}. "
            f"Was down for {minutes} minute(s).")
