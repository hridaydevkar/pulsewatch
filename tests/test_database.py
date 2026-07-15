"""
Test the lightweight SQLite column migration that upgrades pre-existing tables
(created before later features) with columns added since — including the
per-region `region` columns.
"""

from sqlalchemy import create_engine, inspect, text

from pulsewatch import database


def test_ensure_columns_adds_missing(monkeypatch, tmp_path):
    db_file = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_file}")
    # "Old" tables predating the dependency + region features.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE services (id INTEGER PRIMARY KEY, name VARCHAR, url VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE ping_logs (id INTEGER PRIMARY KEY, service_id INTEGER)"
        ))
        conn.execute(text(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY, service_id INTEGER)"
        ))
    monkeypatch.setattr(database, "engine", engine)

    database.ensure_columns()

    scols = {c["name"] for c in inspect(engine).get_columns("services")}
    assert {"check_type", "dependency_kind", "check_config"} <= scols
    assert "region" in {c["name"] for c in inspect(engine).get_columns("ping_logs")}
    assert "region" in {c["name"] for c in inspect(engine).get_columns("incidents")}

    # Idempotent: running again on the now-migrated tables is a no-op.
    database.ensure_columns()
    engine.dispose()


def test_ensure_columns_noop_without_tables(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(database, "engine", engine)
    # No tables yet — must not raise (create_all will make them fresh).
    database.ensure_columns()
    engine.dispose()
