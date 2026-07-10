"""
main.py
FastAPI app exposing:
- GET /              -> HTML dashboard (status cards + incident history)
- GET /api/status    -> JSON status for all services (for curl/Postman demo)
- GET /api/services/{id}/history -> ping history for one service
- GET /api/services/{id}/response-times -> recent response times for sparklines

Run with: pulsewatch serve   (or: uvicorn pulsewatch.main:app --reload)
"""

from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pulsewatch.config import load_config
from pulsewatch.database import Base, engine, SessionLocal
from pulsewatch.models import Service, PingLog, Incident, as_utc
from pulsewatch.monitor import sync_services_from_config, start_scheduler

# Resolve static assets relative to this package, not the current working
# directory, so `pulsewatch serve` works from anywhere.
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup/shutdown. Kept out of import time so that importing this
    module (e.g. from tests) doesn't touch the DB, read config, or spin up the
    background scheduler — only running the server does."""
    # startup: create tables, load config, sync services, start the scheduler
    Base.metadata.create_all(bind=engine)
    config = load_config()
    sync_services_from_config(config)
    app.state.scheduler = start_scheduler(config)
    try:
        yield
    finally:
        # shutdown: stop the background scheduler cleanly
        app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="Uptime Monitor", lifespan=lifespan)
templates = Jinja2Templates(directory=str(STATIC_DIR))


def compute_uptime_pct(db, service_id: int, window_hours: int = 24) -> float:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    logs = (
        db.query(PingLog)
        .filter(PingLog.service_id == service_id, PingLog.timestamp >= since)
        .all()
    )
    if not logs:
        return 100.0
    successful = sum(1 for l in logs if l.success)
    return round((successful / len(logs)) * 100, 2)


@app.get("/api/status")
def api_status():
    db = SessionLocal()
    try:
        services = db.query(Service).all()
        result = []
        for s in services:
            result.append({
                "name": s.name,
                "url": s.url,
                "is_up": s.is_up,
                "uptime_24h_pct": compute_uptime_pct(db, s.id),
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
        for s in services:
            service_data.append({
                "id": s.id,
                "name": s.name,
                "url": s.url,
                "is_up": s.is_up,
                "uptime_pct": compute_uptime_pct(db, s.id),
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
                "started_at": inc.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "resolved": inc.is_resolved,
                "duration_min": round(inc.duration_seconds / 60, 1),
            })

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "services": service_data,
            "incidents": incident_data,
        })
    finally:
        db.close()
