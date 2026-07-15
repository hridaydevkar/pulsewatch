"""
Monitor-level tests: config -> DB sync of the new dependency fields, the
dependency-aware incident/alert path in check_service, and the alert wording.

Uses an isolated in-memory DB (monitor.SessionLocal monkeypatched) and mocks
run_check / send_alert so nothing hits the network.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pulsewatch import checks, monitor
from pulsewatch.alerts import down_message, up_message
from pulsewatch.checks import CheckResult
from pulsewatch.database import Base
from pulsewatch.models import Service, Incident, RegionStatus

REGION = "local"  # default single-region name used by most tests


def _status(Session, sid, region=REGION):
    """The RegionStatus row for one (service, region), or None."""
    session = Session()
    try:
        return (session.query(RegionStatus)
                .filter_by(service_id=sid, region=region).first())
    finally:
        session.close()


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(monitor, "SessionLocal", factory)
    yield factory
    engine.dispose()


# --- sync_services_from_config -------------------------------------------

def test_sync_populates_dependency_fields(Session):
    config = {"services": [
        {"name": "My API", "url": "https://api.example/health",
         "check_interval_seconds": 15, "failure_threshold": 2},
        {"name": "AWS us-east-1", "check_type": "dependency",
         "dependency_kind": "aws_status", "region": "us-east-1",
         "services": ["EC2"], "check_interval_seconds": 300},
    ]}
    monitor.sync_services_from_config(config)

    session = Session()
    http_svc = session.query(Service).filter_by(name="My API").one()
    dep_svc = session.query(Service).filter_by(name="AWS us-east-1").one()

    assert http_svc.check_type == "http"
    assert http_svc.url == "https://api.example/health"
    assert http_svc.is_dependency is False

    assert dep_svc.check_type == "dependency"
    assert dep_svc.dependency_kind == "aws_status"
    assert dep_svc.url is None
    assert dep_svc.is_dependency is True
    # kind-specific keys are captured into check_config, standard keys are not
    assert dep_svc.check_config == {"region": "us-east-1", "services": ["EC2"]}
    session.close()


def test_sync_is_insert_only(Session):
    monitor.sync_services_from_config({"services": [{"name": "S", "url": "http://a"}]})
    # second sync with a changed url must not overwrite the existing row
    monitor.sync_services_from_config({"services": [{"name": "S", "url": "http://CHANGED"}]})
    session = Session()
    rows = session.query(Service).filter_by(name="S").all()
    assert len(rows) == 1
    assert rows[0].url == "http://a"
    session.close()


# --- check_service dependency incident/alert path -------------------------

def _seed(Session, **kw):
    session = Session()
    svc = Service(name=kw.pop("name", "svc"), failure_threshold=kw.pop("threshold", 1), **kw)
    session.add(svc)
    session.commit()
    sid = svc.id
    session.close()
    return sid


def test_check_service_opens_dependency_incident_with_upstream_wording(Session, monkeypatch):
    sid = _seed(Session, name="AWS us-east-1", check_type="dependency",
                dependency_kind="aws_status", check_config={"region": "us-east-1"})
    monkeypatch.setattr(monitor, "run_check",
                        lambda service: CheckResult(False, error="AWS us-east-1 impacted: EC2"))
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    monitor.check_service(sid, REGION, channels=[])

    # incident opened, this region marked down
    assert _status(Session, sid).is_up is False
    session = Session()
    assert session.query(Incident).filter_by(
        service_id=sid, region=REGION, is_resolved=False).count() == 1
    session.close()

    # alert used upstream-dependency wording, not "your service"
    assert len(sent) == 1
    assert "Upstream dependency" in sent[0]
    assert "AWS us-east-1" in sent[0]


def test_check_service_regular_service_uses_your_service_wording(Session, monkeypatch):
    sid = _seed(Session, name="My API", url="http://api", check_type="http")
    monkeypatch.setattr(monitor, "run_check",
                        lambda service: CheckResult(False, error="HTTP 500"))
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    monitor.check_service(sid, REGION, channels=[])

    assert len(sent) == 1
    assert "Your service" in sent[0]
    assert "Upstream dependency" not in sent[0]


def test_check_service_recovery_sends_dependency_recovery(Session, monkeypatch):
    sid = _seed(Session, name="AWS us-east-1", check_type="dependency",
                dependency_kind="aws_status", check_config={"region": "us-east-1"})
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    # go down, then recover
    monkeypatch.setattr(monitor, "run_check", lambda service: CheckResult(False, error="impacted"))
    monitor.check_service(sid, REGION, channels=[])
    monkeypatch.setattr(monitor, "run_check", lambda service: CheckResult(True))
    monitor.check_service(sid, REGION, channels=[])

    assert _status(Session, sid).is_up is True
    session = Session()
    assert session.query(Incident).filter_by(
        service_id=sid, region=REGION, is_resolved=False).count() == 0
    session.close()

    assert any("Upstream dependency" in m and "recovered" in m for m in sent)


def test_check_service_tracks_regions_independently(Session, monkeypatch):
    """Two regions checking the same service keep separate up/down state and
    separate incidents; the alert for a non-default region is tagged with it."""
    sid = _seed(Session, name="API", url="http://api", check_type="http", threshold=1)
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    # us-east fails, eu-west succeeds
    monkeypatch.setattr(monitor, "run_check", lambda s: CheckResult(False, error="HTTP 500"))
    monitor.check_service(sid, "us-east", channels=[])
    monkeypatch.setattr(monitor, "run_check", lambda s: CheckResult(True))
    monitor.check_service(sid, "eu-west", channels=[])

    assert _status(Session, sid, "us-east").is_up is False
    assert _status(Session, sid, "eu-west").is_up is True

    session = Session()
    assert session.query(Incident).filter_by(service_id=sid, region="us-east").count() == 1
    assert session.query(Incident).filter_by(service_id=sid, region="eu-west").count() == 0
    session.close()

    # the down alert carries the region tag
    assert len(sent) == 1
    assert "[region: us-east]" in sent[0]


# --- alert wording --------------------------------------------------------

def test_down_message_wording():
    assert down_message("My API", "HTTP 500").startswith("🔴 Your service")
    dep = down_message("AWS us-east-1", "EC2 impacted", is_dependency=True)
    assert dep.startswith("⚠️ Upstream dependency")
    assert "AWS us-east-1" in dep


def test_up_message_wording():
    assert "Your service" in up_message("My API", 120)
    dep = up_message("AWS us-east-1", 120, is_dependency=True)
    assert "Upstream dependency" in dep and "recovered" in dep


# --- incident state machine driven through a mocked httpx client ----------
# These exercise the *real* run_check -> http_check -> httpx.get path with the
# network mocked at pulsewatch.checks.httpx, verifying threshold behaviour end
# to end rather than stubbing run_check.

def _http_response(status_code):
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def _state(Session, sid, region=REGION):
    """(is_up, consecutive_failures, open_incidents, total_incidents) for a region."""
    session = Session()
    try:
        st = (session.query(RegionStatus)
              .filter_by(service_id=sid, region=region).first())
        is_up = st.is_up if st else True
        failures = st.consecutive_failures if st else 0
        open_incidents = session.query(Incident).filter_by(
            service_id=sid, region=region, is_resolved=False).count()
        total_incidents = session.query(Incident).filter_by(
            service_id=sid, region=region).count()
        return is_up, failures, open_incidents, total_incidents
    finally:
        session.close()


def test_incident_opens_only_after_threshold_consecutive_failures(Session, monkeypatch):
    sid = _seed(Session, name="API", url="http://api.local", check_type="http", threshold=3)
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    with patch.object(checks.httpx, "get", return_value=_http_response(500)) as get:
        # First two failures: counter climbs but no incident, still "up".
        monitor.check_service(sid, REGION, channels=[])
        assert _state(Session, sid) == (True, 1, 0, 0)
        monitor.check_service(sid, REGION, channels=[])
        assert _state(Session, sid) == (True, 2, 0, 0)

        # Third consecutive failure crosses the threshold: incident opens, down.
        monitor.check_service(sid, REGION, channels=[])
        assert _state(Session, sid) == (False, 3, 1, 1)

        # Further failures don't open duplicate incidents.
        monitor.check_service(sid, REGION, channels=[])
        assert _state(Session, sid) == (False, 4, 1, 1)

    assert get.call_count == 4                       # httpx was really driven
    assert len(sent) == 1 and "is DOWN" in sent[0]   # alerted once, on open


def test_single_failure_then_recovery_opens_no_incident(Session, monkeypatch):
    sid = _seed(Session, name="API", url="http://api.local", check_type="http", threshold=3)
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: None)

    with patch.object(checks.httpx, "get", return_value=_http_response(500)):
        monitor.check_service(sid, REGION, channels=[])   # one blip
    with patch.object(checks.httpx, "get", return_value=_http_response(200)):
        monitor.check_service(sid, REGION, channels=[])   # recovers

    # A single blip below threshold never opens an incident, counter resets.
    assert _state(Session, sid) == (True, 0, 0, 0)


def test_incident_resolves_on_recovery(Session, monkeypatch):
    sid = _seed(Session, name="API", url="http://api.local", check_type="http", threshold=2)
    sent = []
    monkeypatch.setattr(monitor, "send_alert", lambda channels, msg: sent.append(msg))

    with patch.object(checks.httpx, "get", return_value=_http_response(503)):
        monitor.check_service(sid, REGION, channels=[])
        monitor.check_service(sid, REGION, channels=[])   # opens incident
    assert _state(Session, sid) == (False, 2, 1, 1)

    with patch.object(checks.httpx, "get", return_value=_http_response(200)):
        monitor.check_service(sid, REGION, channels=[])   # recovery

    assert _state(Session, sid) == (True, 0, 0, 1)

    session = Session()
    inc = session.query(Incident).filter_by(service_id=sid).one()
    assert inc.is_resolved is True and inc.resolved_at is not None
    session.close()

    assert any("is DOWN" in m for m in sent)
    assert any("RECOVERED" in m for m in sent)
