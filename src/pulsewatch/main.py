"""
main.py
FastAPI app exposing:
- GET /              -> HTML dashboard (status cards + incident history)
- GET /api/status    -> JSON status for all services (for curl/Postman demo)
- GET /api/services/{id}/history -> ping history for one service
- GET /api/services/{id}/response-times -> recent response times for sparklines

Run with: pulsewatch serve   (or: uvicorn pulsewatch.main:app --reload)
"""

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pulsewatch.config import load_config, DEFAULT_REGION, REGION_ENV
from pulsewatch.database import Base, engine, SessionLocal, ensure_columns
from pulsewatch.models import Service, PingLog, Incident, RegionStatus, as_utc
from pulsewatch.monitor import sync_services_from_config, start_scheduler

# Resolve static assets relative to this package, not the current working
# directory, so `pulsewatch serve` works from anywhere.
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown. Kept out of import time so that importing this
    module (e.g. from tests) doesn't touch the DB, read config, or spin up the
    background scheduler — only running the server does."""
    # startup: create tables, load config, sync services, start the worker.
    # The built-in worker's region comes from `pulsewatch serve --region`.
    region = os.environ.get(REGION_ENV, DEFAULT_REGION)
    Base.metadata.create_all(bind=engine)
    ensure_columns()  # add post-release columns to a pre-existing DB
    config = load_config()
    sync_services_from_config(config)
    app.state.scheduler = start_scheduler(config, region=region)
    try:
        yield
    finally:
        # shutdown: stop the background scheduler cleanly
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="Uptime Monitor", lifespan=lifespan)
templates = Jinja2Templates(directory=str(STATIC_DIR))


def _redact_conn(conn: str) -> str:
    """Strip user:pass@ from a DB connection string so credentials never reach
    the dashboard or API."""
    return re.sub(r"://[^@/]*@", "://", conn) if conn else ""


def display_target(service) -> str:
    """A short, credential-safe description of what a service checks."""
    if service.check_type != "dependency":
        return service.url or ""
    cfg = service.check_config or {}
    kind = service.dependency_kind
    if kind == "aws_status":
        region = cfg.get("region", "?")
        services = cfg.get("services")
        base = f"AWS · {region}"
        return f"{base} · {', '.join(services)}" if services else base
    if kind == "database":
        return _redact_conn(cfg.get("connection_string", "")) or "database"
    if kind == "custom_api":
        return service.url or cfg.get("url", "")
    return kind or "dependency"


def compute_uptime_pct(db, service_id: int, window_hours: int = 24,
                       region: str = None) -> float:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    query = db.query(PingLog).filter(
        PingLog.service_id == service_id, PingLog.timestamp >= since)
    if region is not None:
        query = query.filter(PingLog.region == region)
    logs = query.all()
    if not logs:
        return 100.0
    successful = sum(1 for l in logs if l.success)
    return round((successful / len(logs)) * 100, 2)


def region_reports(db, service):
    """Per-region status for a service, one entry per region that has reported.

    Returns (regions, overall_up): `regions` is sorted by name; `overall_up` is
    True only if every reporting region is up (down anywhere -> down overall).
    """
    statuses = sorted(service.statuses, key=lambda st: st.region)
    regions = [
        {
            "region": st.region,
            "is_up": st.is_up,
            "uptime_pct": compute_uptime_pct(db, service.id, region=st.region),
        }
        for st in statuses
    ]
    overall_up = all(r["is_up"] for r in regions) if regions else True
    return regions, overall_up


@app.get("/api/status")
def api_status():
    db = SessionLocal()
    try:
        services = db.query(Service).all()
        result = []
        for s in services:
            regions, overall_up = region_reports(db, s)
            result.append({
                "name": s.name,
                "url": s.url,
                "is_up": overall_up,
                "uptime_24h_pct": compute_uptime_pct(db, s.id),
                "check_type": s.check_type,
                "dependency_kind": s.dependency_kind,
                "target": display_target(s),
                "regions": regions,
            })
        return {"services": result}
    finally:
        db.close()


@app.get("/api/services/{service_id}/history")
def service_history(service_id: int, limit: int = 50):
    db = SessionLocal()
    try:
        logs = (
            db.query(PingLog)
            .filter_by(service_id=service_id)
            .order_by(PingLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "timestamp": l.timestamp.isoformat(),
                "success": l.success,
                "status_code": l.status_code,
                "response_time_ms": round(l.response_time_ms, 1) if l.response_time_ms else None,
                "error": l.error,
            }
            for l in logs
        ]
    finally:
        db.close()


@app.get("/api/services/{service_id}/response-times")
def service_response_times(service_id: int, window_minutes: int = 60, limit: int = 200):
    """Last `limit` response times within the last `window_minutes`, oldest
    first, so a sparkline reads left-to-right in time."""
    db = SessionLocal()
    try:
        since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        logs = (
            db.query(PingLog)
            .filter(PingLog.service_id == service_id, PingLog.timestamp >= since)
            .order_by(PingLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        logs.reverse()  # chronological order for the chart
        return {
            "service_id": service_id,
            "window_minutes": window_minutes,
            "points": [
                {
                    "timestamp": as_utc(l.timestamp).isoformat(),
                    "response_time_ms": (
                        round(l.response_time_ms, 1)
                        if l.response_time_ms is not None else None
                    ),
                    "success": l.success,
                }
                for l in logs
            ],
        }
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = SessionLocal()
    try:
        services = db.query(Service).all()
        service_data = []
        multi_region_anywhere = False
        for s in services:
            regions, overall_up = region_reports(db, s)
            if len(regions) > 1:
                multi_region_anywhere = True
            service_data.append({
                "id": s.id,
                "name": s.name,
                "target": display_target(s),
                "is_up": overall_up,
                "uptime_pct": compute_uptime_pct(db, s.id),
                "is_dependency": s.check_type == "dependency",
                "kind": s.dependency_kind if s.check_type == "dependency" else "service",
                # per-region breakdown; template shows it only when >1 region.
                "regions": regions,
                "multi_region": len(regions) > 1,
            })

        incidents = (
            db.query(Incident)
            .order_by(Incident.started_at.desc())
            .limit(20)
            .all()
        )
        incident_data = []
        for inc in incidents:
            incident_data.append({
                "service_name": inc.service.name,
                "region": inc.region,
                "started_at": inc.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "resolved": inc.is_resolved,
                "duration_min": round(inc.duration_seconds / 60, 1),
            })

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "services": service_data,
                "incidents": incident_data,
                # show the region column in the incident table only if relevant
                "show_regions": multi_region_anywhere,
            },
        )
    finally:
        db.close()
