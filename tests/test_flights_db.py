"""Offline contracts for the Postgres layer. A fake connection records SQL; nothing connects."""

from datetime import date

import pytest

from usecases import db


class FakeCursor:
    def __init__(self, log, fetch=None):
        self.log, self.fetch = log, fetch

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.log.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, seq):
        self.log.append(("executemany", " ".join(sql.split()), list(seq)))

    def fetchone(self):
        return self.fetch


class FakeConnection:
    def __init__(self, fetch=None):
        self.log, self.fetch, self.commits = [], fetch, 0

    def cursor(self):
        return FakeCursor(self.log, self.fetch)

    def commit(self):
        self.commits += 1


def test_ensure_schema_creates_both_tables_idempotently():
    conn = FakeConnection()
    db.ensure_schema(conn)
    sql = " ".join(entry[1] for entry in conn.log)
    assert "CREATE TABLE IF NOT EXISTS grid_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS flight_prices" in sql
    assert "PRIMARY KEY (snapshot_date, origin, destination, depart_date, return_date)" in sql
    assert conn.commits == 1


def test_start_run_upserts_on_the_window_key_and_returns_id():
    conn = FakeConnection(fetch=(42,))
    run_id = db.start_run(
        conn,
        snapshot_date=date(2026, 9, 21),
        watch="mia-ccs-spring",
        origin="MIA",
        destination="CCS",
        anchor_depart=date(2027, 3, 26),
        anchor_return=date(2027, 3, 27),
        mode="full",
    )
    assert run_id == 42
    kind, sql, params = conn.log[-1]
    assert kind == "execute"
    assert "INSERT INTO grid_runs" in sql
    assert "ON CONFLICT (snapshot_date, origin, destination, anchor_depart, anchor_return) DO UPDATE" in sql
    assert "status = 'running'" in sql and "RETURNING id" in sql
    assert params["mode"] == "full" and params["watch"] == "mia-ccs-spring"
    assert conn.commits == 1


def test_finish_run_writes_status_and_metrics():
    conn = FakeConnection()
    db.finish_run(conn, 42, status="failed", reason="title mismatch", elapsed_ms=1200, jev_requests=3, text_calls=0)
    kind, sql, params = conn.log[-1]
    assert "UPDATE grid_runs SET" in sql and "finished_at = now()" in sql and "WHERE id = %(run_id)s" in sql
    assert params == {
        "run_id": 42,
        "status": "failed",
        "reason": "title mismatch",
        "final_url": None,
        "elapsed_ms": 1200,
        "jev_requests": 3,
        "text_calls": 0,
    }
    assert conn.commits == 1


def test_upsert_prices_uses_executemany_with_conflict_update():
    conn = FakeConnection()
    row = {
        "snapshot_date": date(2026, 9, 21),
        "origin": "MIA",
        "destination": "CCS",
        "depart_date": date(2027, 3, 24),
        "return_date": date(2027, 3, 30),
        "price_usd": 745,
        "flag": None,
        "in_scope": True,
        "watch": "mia-ccs-spring",
        "raw_label": "$745, Mar 24 to Mar 30",
        "run_id": 42,
    }
    db.upsert_prices(conn, [row])
    kind, sql, rows = conn.log[-1]
    assert kind == "executemany"
    assert "INSERT INTO flight_prices" in sql
    assert "ON CONFLICT (snapshot_date, origin, destination, depart_date, return_date) DO UPDATE" in sql
    assert "captured_at = now()" in sql
    assert rows == [row]
    assert conn.commits == 1


def test_upsert_prices_with_no_rows_does_nothing():
    conn = FakeConnection()
    db.upsert_prices(conn, [])
    assert conn.log == [] and conn.commits == 0


def test_connect_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        db.connect()
