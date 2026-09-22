"""Offline contract for the run loop: a fake Agent and a fake DB stand in for Google, Jev, and Postgres."""

from datetime import date
from unittest.mock import Mock

import pytest

from tests.test_grid import good_page, watch, window
from usecases import flights_grid, grid


class FakeAgent:
    """Mimics jev_ultrafast.Agent: constructed with (url, goal), iterated via run(), inspected via snapshot()."""

    instances = []

    def __init__(self, url, goal, *, page=None, status="done", error=None):
        self.url, self.goal, self.closed = url, goal, False
        self._page, self._status, self._error = page, status, error
        FakeAgent.instances.append(self)

    def run(self):
        if self._error:
            raise self._error
        yield self.snapshot()

    def snapshot(self):
        return {
            "page": self._page, "status": self._status, "decisions": [1, 2, 3], "text_calls": [1], "elapsed_ms": 4321
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    calls = {"start": [], "finish": [], "prices": []}
    monkeypatch.setattr(flights_grid.db, "start_run", lambda conn, **kw: calls["start"].append(kw) or 42)
    monkeypatch.setattr(flights_grid.db, "finish_run", lambda conn, run_id, **kw: calls["finish"].append((run_id, kw)))
    monkeypatch.setattr(flights_grid.db, "upsert_prices", lambda conn, rows: calls["prices"].append(rows))
    FakeAgent.instances.clear()
    return calls


def factory(**kwargs):
    return lambda url, goal: FakeAgent(url, goal, **kwargs)


def test_ok_window_stores_rows_and_metrics(fake_db):
    w = window("full")
    result = flights_grid.run_window(Mock(), w, date(2026, 9, 21), agent_factory=factory(page=good_page(w)))
    assert result["status"] == "ok" and result["run_id"] == 42 and result["min_in_scope"] == 700
    assert fake_db["start"][0]["mode"] == "full" and fake_db["start"][0]["watch"] == "mia-ccs-spring"
    assert len(fake_db["prices"][0]) == len(w.expected_pairs)
    run_id, kw = fake_db["finish"][0]
    assert kw["status"] == "ok" and kw["elapsed_ms"] == 4321 and kw["jev_requests"] == 3 and kw["text_calls"] == 1
    assert kw["final_url"] == "https://www.google.com/travel/flights/search?tfs=x"
    agent = FakeAgent.instances[0]
    assert agent.url == grid.start_url(w) and agent.goal == grid.goal(w) and agent.closed


def test_failed_verification_records_reason_and_stores_nothing(fake_db):
    w = window()
    page = good_page(w)
    page["title"] = "Google Flights"
    result = flights_grid.run_window(Mock(), w, date(2026, 9, 21), agent_factory=factory(page=page))
    assert result["status"] == "failed" and result["reason"].startswith("title mismatch")
    assert fake_db["prices"] == []
    assert fake_db["finish"][0][1]["status"] == "failed"


def test_blocked_agent_is_a_failed_window(fake_db):
    w = window()
    agent = factory(page=good_page(w), status="blocked")
    result = flights_grid.run_window(Mock(), w, date(2026, 9, 21), agent_factory=agent)
    assert result["status"] == "failed" and result["reason"] == "agent stopped with status 'blocked'"


def test_agent_exception_is_a_failed_window_and_loop_continues(fake_db):
    w = window()
    boom = RuntimeError("Model provider returned HTTP 500; no action executed.")
    result = flights_grid.run_window(Mock(), w, date(2026, 9, 21), agent_factory=factory(error=boom))
    assert result["status"] == "failed" and "HTTP 500" in result["reason"]
    assert fake_db["finish"][0][1]["reason"] == str(boom)


def test_main_runs_selected_watches_and_exits_nonzero_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(flights_grid, "load_environment", lambda: None)
    monkeypatch.setattr(flights_grid.db, "connect", lambda: Mock())
    monkeypatch.setattr(flights_grid.db, "ensure_schema", lambda conn: None)
    empty = {"title": "Google Flights", "url": "https://www.google.com/travel/flights", "actions": []}
    monkeypatch.setattr(flights_grid, "Agent", lambda url, goal: FakeAgent(url, goal, page=empty))
    code = flights_grid.main(["--watch", "mia-ccs-nov", "--date", "2026-09-21"])
    out = capsys.readouterr().out
    assert code == 1
    assert "mia-ccs-nov" in out and "failed" in out and "title mismatch" in out
    assert len(FakeAgent.instances) == 2  # the Nov watch tiles to two windows


def test_main_returns_zero_when_every_window_verifies(monkeypatch, capsys):
    monkeypatch.setattr(flights_grid, "load_environment", lambda: None)
    monkeypatch.setattr(flights_grid.db, "connect", lambda: Mock())
    monkeypatch.setattr(flights_grid.db, "ensure_schema", lambda conn: None)
    nov = grid.windows(watch(name="mia-ccs-nov", start=date(2026, 11, 21), end=date(2026, 11, 28)))

    def agent(url, goal):
        w = next(win for win in nov if grid.start_url(win) == url)
        return FakeAgent(url, goal, page=good_page(w))

    monkeypatch.setattr(flights_grid, "Agent", agent)
    code = flights_grid.main(["--watch", "mia-ccs-nov", "--date", "2026-09-21"])
    assert code == 0
    assert "all windows ok" in capsys.readouterr().out


def test_main_rejects_unknown_watch(monkeypatch):
    monkeypatch.setattr(flights_grid, "load_environment", lambda: None)
    with pytest.raises(SystemExit):
        flights_grid.main(["--watch", "nope"])
