"""
Monitor-level tests: config -> DB sync of the new dependency fields, the
dependency-aware incident/alert path in check_service, and the alert wording.

Uses an isolated in-memory DB (monitor.SessionLocal monkeypatched) and mocks
run_check / send_alert so nothing hits the network.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pulsewatch import monitor
from pulsewatch.alerts import down_message, up_message
from pulsewatch.checks import CheckResult
from pulsewatch.database import Base
from pulsewatch.models import Service, Incident


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
    return factory


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

    monitor.check_service(sid, channels=[])

    # incident opened, service marked down
    session = Session()
    svc = session.get(Service, sid)
    assert svc.is_up is False
    assert session.query(Incident).filter_by(service_id=sid, is_resolved=False).count() == 1
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

    monitor.check_service(sid, channels=[])

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
    monitor.check_service(sid, channels=[])
    monkeypatch.setattr(monitor, "run_check", lambda service: CheckResult(True))
    monitor.check_service(sid, channels=[])

    session = Session()
    svc = session.get(Service, sid)
    assert svc.is_up is True
    assert session.query(Incident).filter_by(service_id=sid, is_resolved=False).count() == 0
    session.close()

    assert any("Upstream dependency" in m and "recovered" in m for m in sent)


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
