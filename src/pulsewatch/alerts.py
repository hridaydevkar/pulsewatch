"""
alerts.py
Sends Discord webhook notifications when a service goes down or recovers.
If no webhook URL is configured, alerts are just printed to the console
instead -- so the project still runs and is demoable with zero setup.
"""

import httpx


def send_alert(webhook_url: str, message: str):
    if not webhook_url:
        print(f"[ALERT - no webhook configured] {message}")
        return

    try:
        httpx.post(webhook_url, json={"content": message}, timeout=5)
    except Exception as e:
        print(f"[ALERT] Failed to send Discord alert: {e}")


def down_message(service_name: str, error: str) -> str:
    return f"🔴 **{service_name}** is DOWN. Error: {error}"


def up_message(service_name: str, duration_seconds: float) -> str:
    minutes = round(duration_seconds / 60, 1)
    return f"🟢 **{service_name}** has RECOVERED. Was down for {minutes} minute(s)."
