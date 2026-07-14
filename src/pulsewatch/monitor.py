"""
monitor.py
The heart of the project: pings every configured service on its own
interval, logs the result, and manages incident open/close + alerting.
"""

import time
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler

from pulsewatch.database import SessionLocal
from pulsewatch.models import Service, PingLog, Incident
from pulsewatch.alerts import build_channels, send_alert, down_message, up_message
from pulsewatch.checks import run_check

# Re-exported so `from pulsewatch.monitor import load_config` keeps working.
from pulsewatch.config import load_config  # noqa: F401

# Keys that map to first-class Service columns; anything else in a service's
# config block is dependency-specific and gets stashed in check_config.
_STANDARD_KEYS = {
    "name", "url", "check_interval_seconds", "failure_threshold",
    "check_type", "dependency_kind",
}


def sync_services_from_config(config):
    """Ensure every service in config.yaml exists in the DB."""
    db = SessionLocal()
    try:
        for svc in config["services"]:
            existing = db.query(Service).filter_by(name=svc["name"]).first()
            if not existing:
                check_config = {k: v for k, v in svc.items() if k not in _STANDARD_KEYS}
                db.add(Service(
                    name=svc["name"],
                    url=svc.get("url"),
                    check_interval_seconds=svc.get("check_interval_seconds", 30),
                    failure_threshold=svc.get("failure_threshold", 3),
                    check_type=svc.get("check_type", "http"),
                    dependency_kind=svc.get("dependency_kind"),
                    check_config=check_config or None,
                ))
        db.commit()
    finally:
        db.close()


def check_service(service_id: int, channels):
    db = SessionLocal()
    try:
        service = db.query(Service).filter_by(id=service_id).first()
        if not service:
            return

        start = time.monotonic()
        result = run_check(service)
        response_time_ms = (time.monotonic() - start) * 1000

        db.add(PingLog(
            service_id=service.id,
            success=result.success,
            status_code=result.status_code,
            response_time_ms=response_time_ms,
            error=result.error,
        ))

        is_dependency = service.check_type == "dependency"
        if result.success:
            # Recovery handling
            if not service.is_up:
                open_incident = (
                    db.query(Incident)
                    .filter_by(service_id=service.id, is_resolved=False)
                    .first()
                )
                if open_incident:
                    open_incident.resolved_at = datetime.now(timezone.utc)
                    open_incident.is_resolved = True
                    send_alert(channels, up_message(
                        service.name, open_incident.duration_seconds,
                        is_dependency=is_dependency))

            service.consecutive_failures = 0
            service.is_up = True
        else:
            service.consecutive_failures += 1
            if service.consecutive_failures >= service.failure_threshold and service.is_up:
                service.is_up = False
                db.add(Incident(service_id=service.id))
                send_alert(channels, down_message(
                    service.name, result.error or "unknown error",
                    is_dependency=is_dependency))

        db.commit()
    finally:
        db.close()


def start_scheduler(config):
    scheduler = BackgroundScheduler()
    db = SessionLocal()
    services = db.query(Service).all()
    db.close()

    channels = build_channels(config)

    for service in services:
        scheduler.add_job(
            check_service,
            "interval",
            seconds=service.check_interval_seconds,
            args=[service.id, channels],
            id=f"check_{service.id}",
            next_run_time=datetime.now(),  # run once immediately on startup
        )

    scheduler.start()
    return scheduler
