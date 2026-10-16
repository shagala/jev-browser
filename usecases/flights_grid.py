"""Daily Google Flights date-grid snapshots. Jev navigates; code verifies, extracts, and stores.

uv run --env-file .env python -m usecases.flights_grid [--watch NAME ...] [--date YYYY-MM-DD]
"""

import argparse
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.demo import load_environment

from . import db, grid

WATCHES = Path(__file__).with_name("watches.json")
SETTLE_ATTEMPTS, SETTLE_INTERVAL = 10, 0.3


def settle(observe, page, window):
    """The grid renders after DONE: the Dates tab, then the cells. Re-observe until it holds.

    Polling on any problem rather than one message keeps this from matching on the
    verifier's wording; a permanent failure only costs the budget before it is reported.
    """
    cells, reason = grid.verify(page, window)
    for _ in range(SETTLE_ATTEMPTS):
        if not reason:
            break
        time.sleep(SETTLE_INTERVAL)
        page = observe()
        cells, reason = grid.verify(page, window)
    return page, cells, reason


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
            if state["status"] == "done":
                page, cells, reason = settle(lambda: agent.browser.observe(screenshot=False), page, window)
        metrics = dict(
            final_url=page.get("url"),
            elapsed_ms=state["elapsed_ms"],
            jev_requests=len(state["decisions"]),
            text_calls=len(state["text_calls"]),
        )
        if state["status"] != "done":
            result["reason"] = f"agent stopped with status {state['status']!r}"
        elif reason:
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


# --- recipe handlers -------------------------------------------------------------------
# A recorded recipe owns the navigation; the tiling, the verifier and the store stay here
# as code the recipe cannot rewrite. `usecases.run` reaches them by name.


def param_sets():
    """One unit of work per `direct` window. `full` stays main()'s daily agent canary."""
    return [
        {"watch": w.name, "origin": w.origin, "destination": w.destination,
         "depart": window.anchor_depart.isoformat(), "ret": window.anchor_return.isoformat()}
        for w in grid.load_watches(WATCHES)
        for window in grid.windows(w)
        if window.mode == "direct"
    ]


def window_for(params):
    """Params are the only interface a recipe has to this module; rebuild the window from them."""
    watch = next(w for w in grid.load_watches(WATCHES) if w.name == params["watch"])
    return grid.Window(watch, date.fromisoformat(params["depart"]),
                       date.fromisoformat(params["ret"]), "replay")


def extract(browser, params):
    """The terminal verifier. Replay reporting `ok` is not evidence that the grid is open."""
    def observe():
        return browser.observe(screenshot=False)

    _page, cells, reason = settle(observe, observe(), window_for(params))
    return cells, reason


def store(cells, params, *, snapshot_date=None, conn=None):
    """Persist only what the verifier passed, with one run row per replay."""
    window = window_for(params)
    w = window.watch
    snapshot_date = snapshot_date or date.today()
    owned = conn is None
    conn = conn or db.connect()
    try:
        if owned:
            db.ensure_schema(conn)
        run_id = db.start_run(conn, snapshot_date=snapshot_date, watch=w.name, origin=w.origin,
                              destination=w.destination, anchor_depart=window.anchor_depart,
                              anchor_return=window.anchor_return, mode=window.mode)
        rows = grid.price_rows(window, cells, snapshot_date, run_id)
        db.upsert_prices(conn, rows)
        db.finish_run(conn, run_id, status="ok", reason=None)
        prices = [r["price_usd"] for r in rows if r["in_scope"] and r["price_usd"] is not None]
        return {"rows": len(rows), "min_in_scope": min(prices) if prices else None}
    finally:
        if owned:
            conn.close()


def report(watch, window, result):
    price = f"min in-scope ${result['min_in_scope']}" if result["min_in_scope"] is not None else ""
    note = result["reason"] or price
    print(
        f"{watch.name:16} {window.mode:6} {window.anchor_depart} → {window.anchor_return}  {result['status']:6} {note}",
        flush=True,
    )


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
            report(w, window, result)
            if result["status"] != "ok":
                # One retry in direct mode: a flaky Jev run (or the daily homepage exercise) must not lose data.
                window = replace(window, mode="direct")
                result = run_window(conn, window, args.date)
                report(w, window, result)
            failures += result["status"] != "ok"
    print(f"{failures} failed window(s)" if failures else "all windows ok", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
