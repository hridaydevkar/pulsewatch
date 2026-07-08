"""
Regression tests for Incident.duration_seconds.

Reproduces the exact bug: an Incident's started_at is stored as timezone-aware
UTC but comes back timezone-naive after a SQLite round-trip, so subtracting it
from an aware datetime (utcnow() or a freshly-set resolved_at) raised:

    TypeError: can't subtract offset-naive and offset-aware datetimes

Uses a throwaway in-memory SQLite DB (not the app's uptime.db) so the test is
isolated and leaves nothing behind.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pulsewatch.database import Base
from pulsewatch.models import Service, Incident


@pytest.fixture
def session():
    # StaticPool keeps a single shared connection so the in-memory DB persists
    # across commits within the test.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_incident(session):
    service = Service(name="Example API", url="https://example.com")
    session.add(service)
    session.commit()
    incident = Incident(service_id=service.id)  # started_at default = aware UTC
    session.add(incident)
    session.commit()
    incident_id = incident.id
    # Force a genuine reload from the DB so started_at is read back naive,
    # exactly as it is in the running app.
    session.expire_all()
    return session.get(Incident, incident_id)


def test_started_at_comes_back_naive(session):
    """Documents the root cause: SQLite drops tzinfo on the round-trip."""
    incident = _make_incident(session)
    assert incident.started_at.tzinfo is None


def test_duration_seconds_ongoing_incident(session):
    """The exact failure scenario: resolved_at is None, so end=utcnow() (aware)
    is subtracted from started_at (naive from DB)."""
    incident = _make_incident(session)
    assert incident.resolved_at is None

    duration = incident.duration_seconds  # used to raise TypeError

    assert isinstance(duration, float)
    assert duration >= 0


def test_duration_seconds_resolved_incident(session):
    """The recovery path: resolved_at is set aware (as monitor.py does) while
    started_at is naive from the DB."""
    incident = _make_incident(session)
    incident.resolved_at = datetime.now(timezone.utc)  # aware, like monitor.py

    duration = incident.duration_seconds  # used to raise TypeError

    assert isinstance(duration, float)
    assert duration >= 0


def test_duration_seconds_resolved_naive(session):
    """Belt-and-suspenders: even if resolved_at is stored/read back naive, the
    subtraction still works."""
    incident = _make_incident(session)
    incident.resolved_at = datetime.now(timezone.utc)
    session.add(incident)
    session.commit()
    session.expire_all()
    reloaded = session.get(Incident, incident.id)
    assert reloaded.resolved_at.tzinfo is None  # naive after round-trip

    assert isinstance(reloaded.duration_seconds, float)
