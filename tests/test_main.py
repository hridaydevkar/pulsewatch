"""
Tests for the FastAPI app: the /api/services/{id}/response-times endpoint and
the lifespan startup/shutdown wiring.

The lifespan refactor means importing pulsewatch.main has no side effects, so
these tests can drive the app with an isolated in-memory DB and never touch the
real uptime.db or start the background scheduler. The endpoint tests use
TestClient *without* the context manager, which deliberately skips lifespan.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pulsewatch.main as main
from pulsewatch.database import Base
from pulsewatch.models import Service, PingLog, RegionStatus


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
    yield TestClient(main.app), service_id
    engine.dispose()


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

    monkeypatch.delenv("PULSEWATCH_REGION", raising=False)  # -> default "local"
    monkeypatch.setattr(main, "load_config", load)
    monkeypatch.setattr(main, "sync_services_from_config", sync)
    monkeypatch.setattr(main, "start_scheduler", start)
    # Keep create_all + the column migration off disk / off the real uptime.db.
    # StaticPool = one shared connection, so dispose() reliably closes it even
    # though the lifespan runs in TestClient's portal thread.
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(main, "ensure_columns", MagicMock())

    with TestClient(main.app):  # __enter__ runs lifespan startup
        load.assert_called_once()
        sync.assert_called_once_with(cfg)
        start.assert_called_once_with(cfg, region="local")
        assert main.app.state.scheduler is fake_scheduler
        fake_scheduler.shutdown.assert_not_called()

    # __exit__ runs lifespan shutdown
    fake_scheduler.shutdown.assert_called_once()
    test_engine.dispose()


# --- /api/status, /api/services/{id}/history, and the dashboard -----------

@pytest.fixture
def multi_client(monkeypatch):
    """A TestClient over an isolated DB with a healthy http service and a
    down database dependency (whose connection string carries a secret)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", TestSession)

    session = TestSession()
    api = Service(name="My API", url="http://api.local/health", check_type="http")
    db_dep = Service(
        name="Primary DB", check_type="dependency", dependency_kind="database",
        check_config={"connection_string": "postgresql://u:secret@db.internal:5432/prod"},
    )
    session.add_all([api, db_dep])
    session.commit()
    # per-region live status (single "local" region): API up, DB down
    session.add(RegionStatus(service_id=api.id, region="local", is_up=True))
    session.add(RegionStatus(service_id=db_dep.id, region="local", is_up=False))
    session.commit()
    session.close()
    yield TestClient(main.app)
    engine.dispose()


def test_api_status_lists_services_with_dependency_fields(multi_client):
    resp = multi_client.get("/api/status")
    assert resp.status_code == 200
    services = {s["name"]: s for s in resp.json()["services"]}

    api = services["My API"]
    assert api["check_type"] == "http"
    assert api["is_up"] is True
    assert api["target"] == "http://api.local/health"
    assert api["uptime_24h_pct"] == 100.0  # no pings -> defaults to 100

    db = services["Primary DB"]
    assert db["check_type"] == "dependency"
    assert db["dependency_kind"] == "database"
    assert db["is_up"] is False


def test_api_status_never_leaks_db_credentials(multi_client):
    db = {s["name"]: s for s in multi_client.get("/api/status").json()["services"]}["Primary DB"]
    assert "secret" not in db["target"]
    assert "secret" not in (db["url"] or "")
    assert db["target"] == "postgresql://db.internal:5432/prod"


@pytest.fixture
def multiregion_client(monkeypatch):
    """One service reported on by two regions: us-east up, eu-west down."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", TestSession)
    session = TestSession()
    svc = Service(name="Multi API", url="http://api.local", check_type="http")
    session.add(svc)
    session.commit()
    session.add(RegionStatus(service_id=svc.id, region="us-east", is_up=True))
    session.add(RegionStatus(service_id=svc.id, region="eu-west", is_up=False))
    session.commit()
    session.close()
    yield TestClient(main.app)
    engine.dispose()


def test_api_status_reports_per_region(multiregion_client):
    svc = multiregion_client.get("/api/status").json()["services"][0]
    # down in one region -> overall down
    assert svc["is_up"] is False
    regions = {r["region"]: r["is_up"] for r in svc["regions"]}
    assert regions == {"us-east": True, "eu-west": False}


def test_dashboard_shows_region_breakdown_when_multiple(multiregion_client):
    html = multiregion_client.get("/").text
    assert "by region" in html          # the per-region section renders
    assert "us-east" in html and "eu-west" in html


def test_dashboard_no_region_breakdown_for_single_region(seeded_client):
    # seeded_client seeds pings but no RegionStatus -> single/'no region' view
    client, _ = seeded_client
    assert "by region" not in client.get("/").text


def test_api_history_returns_pings_newest_first(seeded_client):
    client, sid = seeded_client
    logs = client.get(f"/api/services/{sid}/history").json()
    assert len(logs) == 4  # the fixture seeds four pings
    times = [l["timestamp"] for l in logs]
    assert times == sorted(times, reverse=True)  # newest first
    for entry in logs:
        assert set(entry) >= {"timestamp", "success", "status_code",
                              "response_time_ms", "error"}


def test_api_history_respects_limit(seeded_client):
    client, sid = seeded_client
    logs = client.get(f"/api/services/{sid}/history", params={"limit": 2}).json()
    assert len(logs) == 2


def test_display_target_is_safe_for_every_kind():
    from pulsewatch.main import display_target

    def s(**kw):
        kw.setdefault("url", None)
        kw.setdefault("dependency_kind", None)
        kw.setdefault("check_config", None)
        return SimpleNamespace(**kw)

    assert display_target(s(check_type="http", url="http://x")) == "http://x"
    assert display_target(s(check_type="dependency", dependency_kind="aws_status",
                            check_config={"region": "us-east-1", "services": ["EC2", "S3"]})) \
        == "AWS · us-east-1 · EC2, S3"
    assert display_target(s(check_type="dependency", dependency_kind="aws_status",
                            check_config={"region": "eu-west-1"})) == "AWS · eu-west-1"
    assert display_target(s(check_type="dependency", dependency_kind="custom_api",
                            url="https://status.example/api")) == "https://status.example/api"
    # credentials stripped from the database target
    assert display_target(s(check_type="dependency", dependency_kind="database",
                            check_config={"connection_string": "mysql://u:pw@h:3306/db"})) \
        == "mysql://h:3306/db"


def test_dashboard_renders_html_with_service_badge(seeded_client):
    client, _ = seeded_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Seeded" in resp.text                       # the service name
    assert '<span class="tag svc">service</span>' in resp.text
    assert 'canvas class="spark"' in resp.text         # sparkline wired in
