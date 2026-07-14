"""
database.py
Sets up the SQLite database engine and session for the app.
Using SQLite keeps this project zero-config: no external DB server needed
to clone and run it, which matters a lot for a portfolio project.
"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./uptime.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the initial release. create_all() only creates missing
# tables, never adds columns to an existing one, so upgrading an existing
# uptime.db would otherwise hit "no such column". SQLite has no real migration
# tooling here, so we add any missing columns by hand — idempotent and a no-op
# on a freshly-created DB (where create_all already added them).
_ADDED_SERVICE_COLUMNS = {
    "check_type": "VARCHAR DEFAULT 'http'",
    "dependency_kind": "VARCHAR",
    "check_config": "TEXT",  # SQLAlchemy JSON stores/reads JSON text
}


def ensure_service_columns():
    inspector = inspect(engine)
    if "services" not in inspector.get_table_names():
        return  # create_all will make it fresh, with all columns
    existing = {col["name"] for col in inspector.get_columns("services")}
    missing = {n: d for n, d in _ADDED_SERVICE_COLUMNS.items() if n not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f"ALTER TABLE services ADD COLUMN {name} {ddl}"))
