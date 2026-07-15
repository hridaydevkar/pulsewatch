"""
database.py
Sets up the SQLite database engine and session for the app.
Using SQLite keeps this project zero-config: no external DB server needed
to clone and run it, which matters a lot for a portfolio project.
"""

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./uptime.db"

engine = create_engine(
    DATABASE_URL,
    # timeout = how long a writer waits for the lock before erroring; needed
    # because multiple region workers write to this one SQLite file.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    """WAL + a busy timeout let several worker processes read/write the same
    SQLite database concurrently without tripping over 'database is locked'."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


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
_ADDED_COLUMNS = {
    "services": {
        "check_type": "VARCHAR DEFAULT 'http'",
        "dependency_kind": "VARCHAR",
        "check_config": "TEXT",  # SQLAlchemy JSON stores/reads JSON text
    },
    "ping_logs": {"region": "VARCHAR DEFAULT 'local'"},
    "incidents": {"region": "VARCHAR DEFAULT 'local'"},
}


def ensure_columns():
    """Add any missing post-release columns to existing tables (idempotent)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in tables:
                continue  # create_all will make it fresh, with all columns
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
