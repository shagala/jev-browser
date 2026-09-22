"""Daily Google Flights date-grid snapshots. Jev navigates; code verifies, extracts, and stores.

uv run --env-file .env python -m usecases.flights_grid [--watch NAME ...] [--date YYYY-MM-DD]
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.demo import load_environment

from . import db, grid

WATCHES = Path(__file__).with_name("watches.json")


def run_window(conn, window, snapshot_date, agent_factory=None):
    agent_factory = agent_factory or Agent  # resolved at call time so tests can substitute a fake
    w = window.watch
    run_id = db.start_run(
        conn,
        snapshot_date=snapshot_date,
        watch=w.name,
        origin=w.origin,
        destination=w.destination,
        anchor_depart=window.anchor_depart,
        anchor_return=window.anchor_return,
        mode=window.mode,
    )
    metrics, result = {}, {"status": "failed", "reason": None, "min_in_scope": None, "run_id": run_id}
    try:
        with agent_factory(grid.start_url(window), grid.goal(window)) as agent:
            for _ in agent.run():
                pass
            state = agent.snapshot()
        page = state.get("page") or {}
        metrics = dict(
            final_url=page.get("url"),
            elapsed_ms=state["elapsed_ms"],
            jev_requests=len(state["decisions"]),
            text_calls=len(state["text_calls"]),
        )
        if state["status"] != "done":
            result["reason"] = f"agent stopped with status {state['status']!r}"
        else:
            cells, reason = grid.verify(page, window)
            if reason:
                result["reason"] = reason
            else:
                rows = grid.price_rows(window, cells, snapshot_date, run_id)
                db.upsert_prices(conn, rows)
                prices = [r["price_usd"] for r in rows if r["in_scope"] and r["price_usd"] is not None]
                result.update(status="ok", min_in_scope=min(prices) if prices else None)
    except Exception as error:  # One window must never end the day's run; the reason is stored.
        result["reason"] = str(error)
    db.finish_run(conn, run_id, status=result["status"], reason=result["reason"], **metrics)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="append", help="Run only these watch names (repeatable).")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="snapshot_date; default today.")
    args = parser.parse_args(argv)
    load_environment()
    watches = grid.load_watches(WATCHES)
    if args.watch:
        unknown = set(args.watch) - {w.name for w in watches}
        if unknown:
            parser.error(f"unknown watch(es): {', '.join(sorted(unknown))}")
        watches = [w for w in watches if w.name in args.watch]
    conn = db.connect()
    db.ensure_schema(conn)
    failures = 0
    print(f"snapshot_date {args.date}  watches {len(watches)}", flush=True)
    for w in watches:
        for window in grid.windows(w):
            result = run_window(conn, window, args.date)
            failures += result["status"] != "ok"
            price = f"min in-scope ${result['min_in_scope']}" if result["min_in_scope"] is not None else ""
            note = result["reason"] or price
            print(
                f"{w.name:16} {window.mode:6} {window.anchor_depart} → {window.anchor_return}  "
                f"{result['status']:6} {note}",
                flush=True,
            )
    print(f"{failures} failed window(s)" if failures else "all windows ok", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
