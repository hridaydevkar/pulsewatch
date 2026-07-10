"""
Tests for the FastAPI app: the /api/services/{id}/response-times endpoint and
the lifespan startup/shutdown wiring.

The lifespan refactor means importing pulsewatch.main has no side effects, so
these tests can drive the app with an isolated in-memory DB and never touch the
real uptime.db or start the background scheduler. The endpoint tests use
TestClient *without* the context manager, which deliberately skips lifespan.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pulsewatch.main as main
from pulsewatch.database import Base
from pulsewatch.models import Service, PingLog


@pytest.fixture
def seeded_client(monkeypatch):
    """A TestClient whose routes hit an isolated in-memory DB pre-seeded with
    one service and four pings: three inside the last hour, one 90 min old."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    # Point the route code's SessionLocal at our throwaway DB.
    monkeypatch.setattr(main, "SessionLocal", TestSession)

    now = datetime.now(timezone.utc)
    session = TestSession()
    service = Service(name="Seeded", url="http://svc.local")
    session.add(service)
    session.commit()
    service_id = service.id

    # (minutes_ago, response_time_ms) — last one is outside the 60-min window.
    samples = [(3, 12.34), (2, 20.0), (1, 8.86), (90, 99.9)]
    for minutes_ago, rt in samples:
        session.add(PingLog(
            service_id=service_id,
            success=True,
            status_code=200,
            response_time_ms=rt,
            timestamp=now - timedelta(minutes=minutes_ago),
        ))
    session.commit()
    session.close()

    # No `with`: lifespan (scheduler/config/real DB) is intentionally not run.
    return TestClient(main.app), service_id


def _get_points(client, service_id, **params):
    resp = client.get(f"/api/services/{service_id}/response-times", params=params)
    assert resp.status_code == 200
    return resp.json()


def test_response_times_shape_and_chronological_order(seeded_client):
    client, sid = seeded_client
    body = _get_points(client, sid)

    assert body["service_id"] == sid
    assert body["window_minutes"] == 60

    points = body["points"]
    # The 90-min-old ping is outside the default 60-min window.
    assert len(points) == 3

    times = [p["timestamp"] for p in points]
    assert times == sorted(times), "points must be oldest-first for the sparkline"

    for p in points:
        assert set(p) == {"timestamp", "response_time_ms", "success"}
        assert p["success"] is True
    # response_time_ms is rounded to one decimal.
    assert [p["response_time_ms"] for p in points] == [12.3, 20.0, 8.9]
    # The out-of-window sample never appears.
    assert 99.9 not in [p["response_time_ms"] for p in points]


def test_timestamps_are_utc_aware(seeded_client):
    client, sid = seeded_client
    points = _get_points(client, sid)["points"]
    assert points, "expected at least one point"
    for p in points:
        assert p["timestamp"].endswith("+00:00")


def test_window_minutes_includes_older_pings(seeded_client):
    client, sid = seeded_client
    body = _get_points(client, sid, window_minutes=120)
    assert body["window_minutes"] == 120
    assert len(body["points"]) == 4  # now includes the 90-min-old ping


def test_limit_keeps_most_recent_in_order(seeded_client):
    client, sid = seeded_client
    # Widen the window so all 4 are candidates, then cap to 2.
    points = _get_points(client, sid, window_minutes=120, limit=2)["points"]
    assert len(points) == 2
    # The two most recent (1 and 2 min ago), still oldest-first.
    assert [p["response_time_ms"] for p in points] == [20.0, 8.9]
    assert points[0]["timestamp"] < points[1]["timestamp"]


def test_unknown_service_returns_empty(seeded_client):
    client, _ = seeded_client
    body = _get_points(client, 9999)
    assert body["points"] == []


# --- lifespan wiring ------------------------------------------------------

def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    """Entering the app context runs startup (tables/config/sync/scheduler);
    leaving it shuts the scheduler down."""
    fake_scheduler = MagicMock()
    cfg = {"services": [], "alerts": []}
    load = MagicMock(return_value=cfg)
    sync = MagicMock()
    start = MagicMock(return_value=fake_scheduler)

    monkeypatch.setattr(main, "load_config", load)
    monkeypatch.setattr(main, "sync_services_from_config", sync)
    monkeypatch.setattr(main, "start_scheduler", start)
    # Keep create_all off disk / off the real uptime.db.
    monkeypatch.setattr(main, "engine", create_engine("sqlite://"))

    with TestClient(main.app):  # __enter__ runs lifespan startup
        load.assert_called_once()
        sync.assert_called_once_with(cfg)
        start.assert_called_once_with(cfg)
        assert main.app.state.scheduler is fake_scheduler
        fake_scheduler.shutdown.assert_not_called()

    # __exit__ runs lifespan shutdown
    fake_scheduler.shutdown.assert_called_once()
