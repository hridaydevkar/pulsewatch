"""
Tests for the pluggable alert-channel system.

Network (httpx) and SMTP (smtplib) are mocked so nothing leaves the machine.
"""

from unittest.mock import MagicMock, patch

import pytest

from pulsewatch.alerts import (
    AlertChannel,
    DiscordChannel,
    SlackChannel,
    EmailChannel,
    build_channels,
    send_alert,
)


# --- build_channels -------------------------------------------------------

def test_build_channels_from_alerts_list():
    config = {
        "alerts": [
            {"type": "discord", "webhook_url": "https://discord/x"},
            {"type": "slack", "webhook_url": "https://slack/y"},
            {"type": "email", "to": "a@b.com", "from": "c@d.com"},
        ]
    }
    channels = build_channels(config)
    assert [type(c) for c in channels] == [DiscordChannel, SlackChannel, EmailChannel]
    assert all(isinstance(c, AlertChannel) for c in channels)
    assert channels[0].webhook_url == "https://discord/x"
    assert channels[1].payload_key == "text"
    assert channels[2].to_addr == "a@b.com"
    assert channels[2].from_addr == "c@d.com"


def test_build_channels_type_is_case_insensitive():
    channels = build_channels({"alerts": [{"type": "Discord", "webhook_url": "u"}]})
    assert isinstance(channels[0], DiscordChannel)


def test_build_channels_empty_or_missing():
    assert build_channels({}) == []
    assert build_channels({"alerts": []}) == []
    assert build_channels({"alerts": None}) == []


def test_build_channels_legacy_discord_webhook():
    channels = build_channels({"discord_webhook_url": "https://legacy/hook"})
    assert len(channels) == 1
    assert isinstance(channels[0], DiscordChannel)
    assert channels[0].webhook_url == "https://legacy/hook"


def test_build_channels_unknown_type_raises():
    with pytest.raises(ValueError, match="Unknown alert channel type"):
        build_channels({"alerts": [{"type": "carrier-pigeon"}]})


# --- webhook channels -----------------------------------------------------

def test_discord_channel_posts_content():
    with patch("pulsewatch.alerts.httpx.post") as post:
        DiscordChannel("https://discord/hook").send("hello")
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://discord/hook"
    assert kwargs["json"] == {"content": "hello"}


def test_slack_channel_posts_text():
    with patch("pulsewatch.alerts.httpx.post") as post:
        SlackChannel("https://slack/hook").send("hello")
    assert post.call_args.kwargs["json"] == {"text": "hello"}


def test_webhook_unconfigured_logs_and_skips_post(capsys):
    with patch("pulsewatch.alerts.httpx.post") as post:
        DiscordChannel("").send("boom")
    post.assert_not_called()
    assert "not configured" in capsys.readouterr().out


def test_webhook_send_swallows_errors(capsys):
    with patch("pulsewatch.alerts.httpx.post", side_effect=RuntimeError("network")):
        SlackChannel("https://slack/hook").send("boom")  # must not raise
    assert "Failed to send Slack alert" in capsys.readouterr().out


# --- email channel --------------------------------------------------------

def test_email_channel_sends_via_smtp(monkeypatch):
    monkeypatch.setenv("PULSEWATCH_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PULSEWATCH_SMTP_PORT", "2525")
    monkeypatch.setenv("PULSEWATCH_SMTP_USER", "user")
    monkeypatch.setenv("PULSEWATCH_SMTP_PASSWORD", "pass")

    with patch("pulsewatch.alerts.smtplib.SMTP") as smtp_cls:
        conn = smtp_cls.return_value.__enter__.return_value
        EmailChannel(to_addr="oncall@example.com", from_addr="alerts@example.com").send(
            "🔴 **My API** is DOWN. Error: HTTP 500"
        )

    smtp_cls.assert_called_once_with("smtp.example.com", 2525, timeout=10)
    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with("user", "pass")
    conn.send_message.assert_called_once()
    sent = conn.send_message.call_args.args[0]
    assert sent["To"] == "oncall@example.com"
    assert sent["From"] == "alerts@example.com"
    # subject is the first line, markdown stripped, prefixed
    assert sent["Subject"] == "[pulsewatch] 🔴 My API is DOWN. Error: HTTP 500"


def test_email_channel_no_host_logs_and_skips(monkeypatch, capsys):
    monkeypatch.delenv("PULSEWATCH_SMTP_HOST", raising=False)
    with patch("pulsewatch.alerts.smtplib.SMTP") as smtp_cls:
        EmailChannel(to_addr="oncall@example.com").send("down")
    smtp_cls.assert_not_called()
    assert "email not configured" in capsys.readouterr().out


def test_email_channel_no_login_without_credentials(monkeypatch):
    monkeypatch.setenv("PULSEWATCH_SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("PULSEWATCH_SMTP_USER", raising=False)
    monkeypatch.delenv("PULSEWATCH_SMTP_PASSWORD", raising=False)
    with patch("pulsewatch.alerts.smtplib.SMTP") as smtp_cls:
        conn = smtp_cls.return_value.__enter__.return_value
        EmailChannel(to_addr="oncall@example.com").send("down")
    conn.login.assert_not_called()
    conn.send_message.assert_called_once()


# --- send_alert dispatch --------------------------------------------------

def test_send_alert_fans_out_to_all_channels():
    c1, c2 = MagicMock(spec=AlertChannel), MagicMock(spec=AlertChannel)
    send_alert([c1, c2], "ping")
    c1.send.assert_called_once_with("ping")
    c2.send.assert_called_once_with("ping")


def test_send_alert_no_channels_logs(capsys):
    send_alert([], "ping")
    assert "no channels configured" in capsys.readouterr().out
