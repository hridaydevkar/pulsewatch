"""
main.py
FastAPI app exposing:
- GET /              -> HTML dashboard (status cards + incident history)
- GET /api/status    -> JSON status for all services (for curl/Postman demo)
- GET /api/services/{id}/history -> ping history for one service

Run with: uvicorn app.main:app --reload
"""

from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import Base, engine, SessionLocal
from app.models import Service, PingLog, Incident
from app.monitor import load_config, sync_services_from_config, start_scheduler

app = FastAPI(title="Uptime Monitor")
templates = Jinja2Templates(directory="app/static")

# --- startup: create tables, load config, sync services, start scheduler ---
Base.metadata.create_all(bind=engine)
config = load_config()
sync_services_from_config(config)
scheduler = start_scheduler(config)


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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db = SessionLocal()
    try:
        services = db.query(Service).all()
        service_data = []
        for s in services:
            service_data.append({
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
