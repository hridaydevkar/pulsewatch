"""
models.py
Three tables:
- Service: the things we're monitoring (loaded from config.yaml)
- PingLog: every single check result, used to calculate uptime %
- Incident: an "outage" record, opened when failure_threshold is hit,
            closed automatically on recovery
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    url = Column(String)
    check_interval_seconds = Column(Integer, default=30)
    failure_threshold = Column(Integer, default=3)

    consecutive_failures = Column(Integer, default=0)
    is_up = Column(Boolean, default=True)

    pings = relationship("PingLog", back_populates="service")
    incidents = relationship("Incident", back_populates="service")


class PingLog(Base):
    __tablename__ = "ping_logs"

    id = Column(Integer, primary_key=True, index=True)
    service_id = Column(Integer, ForeignKey("services.id"))
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
    started_at = Column(DateTime, default=utcnow)
    resolved_at = Column(DateTime, nullable=True)
    is_resolved = Column(Boolean, default=False)

    service = relationship("Service", back_populates="incidents")

    @property
    def duration_seconds(self):
        end = self.resolved_at or utcnow()
        return (end - self.started_at).total_seconds()
