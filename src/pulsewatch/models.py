"""
models.py
Tables:
- Service: the things we're monitoring (loaded from config.yaml)
- RegionStatus: per-(service, region) live state (consecutive failures, up/down)
- PingLog: every single check result, tagged with the reporting region
- Incident: an "outage" record for one (service, region), opened when that
            region's failure_threshold is hit, closed on recovery

A "region" is just a named worker (see config `regions:`); results from every
region land in this one shared database.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from pulsewatch.config import DEFAULT_REGION
from pulsewatch.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def as_utc(dt):
    """Normalize a datetime to timezone-aware UTC.

    SQLite (via SQLAlchemy's default DateTime column) stores datetimes
    without timezone info, so values written as timezone-aware UTC come
    back *naive* after a DB round-trip. Subtracting such a value from an
    aware datetime (e.g. ``utcnow()``) raises:

        TypeError: can't subtract offset-naive and offset-aware datetimes

    This helper makes comparisons safe regardless of which side is aware
    or naive: naive values are assumed to be UTC (which is how we store
    them), aware values are converted to UTC. Returns None unchanged.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    url = Column(String, nullable=True)  # nullable: dependency checks may not ping a URL
    check_interval_seconds = Column(Integer, default=30)
    failure_threshold = Column(Integer, default=3)

    # "http" (default) pings `url`; "dependency" runs a dependency_kind checker
    # (aws_status / database / custom_api) whose extra settings live in check_config.
    check_type = Column(String, default="http")
    dependency_kind = Column(String, nullable=True)
    check_config = Column(JSON, nullable=True)

    # Live up/down state is per-region now (see RegionStatus), not per-service.
    pings = relationship("PingLog", back_populates="service")
    incidents = relationship("Incident", back_populates="service")
    statuses = relationship("RegionStatus", back_populates="service")

    @property
    def is_dependency(self) -> bool:
        return self.check_type == "dependency"


class RegionStatus(Base):
    """Live check state for one service as seen by one region/worker."""
    __tablename__ = "region_statuses"
    __table_args__ = (UniqueConstraint("service_id", "region", name="uq_service_region"),)

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"), index=True)
    region = Column(String, index=True, default=DEFAULT_REGION)
    consecutive_failures = Column(Integer, default=0)
    is_up = Column(Boolean, default=True)

    service = relationship("Service", back_populates="statuses")


class PingLog(Base):
    __tablename__ = "ping_logs"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"))
    region = Column(String, index=True, default=DEFAULT_REGION)
    timestamp = Column(DateTime, default=utcnow)
    success = Column(Boolean)
    status_code = Column(Integer, nullable=True)
    response_time_ms = Column(Float, nullable=True)
    error = Column(String, nullable=True)

    service = relationship("Service", back_populates="pings")


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"))
    region = Column(String, index=True, default=DEFAULT_REGION)
    started_at = Column(DateTime, default=utcnow)
    resolved_at = Column(DateTime, nullable=True)
    is_resolved = Column(Boolean, default=False)

    service = relationship("Service", back_populates="incidents")

    @property
    def duration_seconds(self):
        # Normalize both sides to aware-UTC: started_at comes back naive
        # from SQLite, while resolved_at/utcnow() are aware. See as_utc().
        end = as_utc(self.resolved_at) or utcnow()
        start = as_utc(self.started_at)
        return (end - start).total_seconds()
