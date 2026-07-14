"""
Test the lightweight SQLite column migration that upgrades an existing
services table (created before the dependency feature) with the new columns.
"""

from sqlalchemy import create_engine, inspect, text

from pulsewatch import database


def test_ensure_service_columns_adds_missing(monkeypatch, tmp_path):
    db_file = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_file}")
    # An "old" services table without the dependency columns.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE services (id INTEGER PRIMARY KEY, name VARCHAR, url VARCHAR)"
        ))
    monkeypatch.setattr(database, "engine", engine)

    database.ensure_service_columns()

    cols = {c["name"] for c in inspect(engine).get_columns("services")}
    assert {"check_type", "dependency_kind", "check_config"} <= cols

    # Idempotent: running again on the now-migrated table is a no-op.
    database.ensure_service_columns()
    engine.dispose()


def test_ensure_service_columns_noop_without_table(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(database, "engine", engine)
    # No services table yet — must not raise (create_all will make it fresh).
    database.ensure_service_columns()
    engine.dispose()
