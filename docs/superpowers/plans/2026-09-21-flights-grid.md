# Flights Date-Grid Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily CLI that drives Google Flights' round-trip date grid with Jev for a list of watches and upserts every cell into Postgres.

**Architecture:** `usecases/flights_grid.py` computes 7×7 grid windows per watch, runs the existing `jev_ultrafast.Agent` once per window (full homepage run for the first window of a watch, direct `q=` URL for the rest), verifies the final observed page independently of Jev's DONE, parses cell labels with a regex, and hands rows to `usecases/db.py`, a thin psycopg 3 layer with idempotent schema and upserts. No changes to the agent loop.

**Tech Stack:** Python 3.12, `jev_ultrafast.Agent` (existing), `psycopg[binary]` 3, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-09-21-flights-grid-design.md`

## Global Constraints

- Tests never call paid APIs or the network (`AGENTS.md`); the live path is exercised only by the CLI.
- No site-specific selectors; extraction reads only the observed `page["actions"]` labels and `page["title"]`.
- Ruff line length 120, `select = ["E", "F", "I"]`; every task ends with `uv run ruff check .` clean.
- `uv` is at `~/.pyenv/versions/3.11.9/bin/uv`; run commands as `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ...`.
- Dates in code are `datetime.date`; DB columns are `date`/`timestamptz`; prices are integer USD.
- Commit after each task; do not push.

---

## File map

| File | Responsibility |
| --- | --- |
| `usecases/__init__.py` | Empty package marker. |
| `usecases/db.py` | `connect`, `ensure_schema`, `start_run`, `finish_run`, `upsert_prices`. SQL only. |
| `usecases/grid.py` | Pure logic: `Watch`, `Window`, `Cell`, `load_watches`, `windows`, `parse_cell`, `resolve_year`, `goal`, `url`, `verify`, `price_rows`. No I/O except reading `watches.json`. |
| `usecases/flights_grid.py` | `run_window` (Agent + verify + DB), `main` (CLI, summary, exit code). |
| `usecases/watches.json` | The four initial watches. |
| `tests/test_grid.py` | Offline tests for `usecases/grid.py`. |
| `tests/test_flights_db.py` | Offline tests for `usecases/db.py` via a fake connection. |
| `tests/test_flights_grid.py` | Offline tests for `run_window`/`main` with a fake Agent and fake DB. |
| `docs/flights-grid.md` | How to run, launchd plist, trend query. |
| `pyproject.toml` | Add `psycopg[binary]`. |

---

### Task 1: Dependency, package, and DB layer

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Create: `usecases/__init__.py`, `usecases/db.py`
- Test: `tests/test_flights_db.py`

**Interfaces:**
- Produces:
  - `connect() -> psycopg.Connection` (reads `DATABASE_URL`, `prepare_threshold=None` for the Neon pooler)
  - `ensure_schema(conn) -> None`
  - `start_run(conn, *, snapshot_date, watch, origin, destination, anchor_depart, anchor_return, mode) -> int` (run id)
  - `finish_run(conn, run_id, *, status, reason=None, final_url=None, elapsed_ms=None, jev_requests=None, text_calls=None) -> None`
  - `upsert_prices(conn, rows: list[dict]) -> None` where each row has keys `snapshot_date, origin, destination, depart_date, return_date, price_usd, flag, in_scope, watch, raw_label, run_id`

- [ ] **Step 1: Add the dependency and package marker**

In `pyproject.toml` change the dependencies line to:

```toml
dependencies = ["browser-harness==0.1.13", "httpx[http2]>=0.28,<1", "psycopg[binary]>=3.2,<4"]
```

Then:

```bash
PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv sync
touch usecases/__init__.py
```

Expected: `uv.lock` updated, `psycopg` installed, `usecases/__init__.py` exists (empty).

- [ ] **Step 2: Write the failing DB tests**

Create `tests/test_flights_db.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_flights_db.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usecases.db'` (or ImportError).

- [ ] **Step 4: Implement `usecases/db.py`**

```python
"""Postgres persistence for the flights grid. Idempotent schema, upserts keyed on the window and the cell."""

import os

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_runs (
  id            bigserial PRIMARY KEY,
  snapshot_date date        NOT NULL,
  watch         text        NOT NULL,
  origin        text        NOT NULL,
  destination   text        NOT NULL,
  anchor_depart date        NOT NULL,
  anchor_return date        NOT NULL,
  mode          text        NOT NULL,
  status        text        NOT NULL,
  reason        text,
  final_url     text,
  elapsed_ms    integer,
  jev_requests  integer,
  text_calls    integer,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  UNIQUE (snapshot_date, origin, destination, anchor_depart, anchor_return)
);
CREATE TABLE IF NOT EXISTS flight_prices (
  snapshot_date date    NOT NULL,
  origin        text    NOT NULL,
  destination   text    NOT NULL,
  depart_date   date    NOT NULL,
  return_date   date    NOT NULL,
  price_usd     integer,
  flag          text,
  in_scope      boolean NOT NULL,
  watch         text    NOT NULL,
  raw_label     text,
  run_id        bigint  NOT NULL REFERENCES grid_runs(id),
  captured_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, origin, destination, depart_date, return_date)
);
"""

START_RUN = """
INSERT INTO grid_runs (snapshot_date, watch, origin, destination, anchor_depart, anchor_return, mode, status)
VALUES (%(snapshot_date)s, %(watch)s, %(origin)s, %(destination)s, %(anchor_depart)s, %(anchor_return)s,
        %(mode)s, 'running')
ON CONFLICT (snapshot_date, origin, destination, anchor_depart, anchor_return) DO UPDATE SET
  watch = EXCLUDED.watch, mode = EXCLUDED.mode, status = 'running', reason = NULL, final_url = NULL,
  elapsed_ms = NULL, jev_requests = NULL, text_calls = NULL, started_at = now(), finished_at = NULL
RETURNING id
"""

FINISH_RUN = """
UPDATE grid_runs SET status = %(status)s, reason = %(reason)s, final_url = %(final_url)s,
  elapsed_ms = %(elapsed_ms)s, jev_requests = %(jev_requests)s, text_calls = %(text_calls)s,
  finished_at = now()
WHERE id = %(run_id)s
"""

UPSERT_PRICE = """
INSERT INTO flight_prices (snapshot_date, origin, destination, depart_date, return_date, price_usd, flag,
                           in_scope, watch, raw_label, run_id)
VALUES (%(snapshot_date)s, %(origin)s, %(destination)s, %(depart_date)s, %(return_date)s, %(price_usd)s,
        %(flag)s, %(in_scope)s, %(watch)s, %(raw_label)s, %(run_id)s)
ON CONFLICT (snapshot_date, origin, destination, depart_date, return_date) DO UPDATE SET
  price_usd = EXCLUDED.price_usd, flag = EXCLUDED.flag, in_scope = EXCLUDED.in_scope,
  watch = EXCLUDED.watch, raw_label = EXCLUDED.raw_label, run_id = EXCLUDED.run_id, captured_at = now()
"""


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is required; add it to .env")
    # Neon's pooler is PgBouncer in transaction mode: never use server-side prepared statements.
    return psycopg.connect(url, prepare_threshold=None)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def start_run(conn, *, snapshot_date, watch, origin, destination, anchor_depart, anchor_return, mode):
    params = dict(
        snapshot_date=snapshot_date,
        watch=watch,
        origin=origin,
        destination=destination,
        anchor_depart=anchor_depart,
        anchor_return=anchor_return,
        mode=mode,
    )
    with conn.cursor() as cur:
        cur.execute(START_RUN, params)
        (run_id,) = cur.fetchone()
    conn.commit()
    return run_id


def finish_run(conn, run_id, *, status, reason=None, final_url=None, elapsed_ms=None, jev_requests=None, text_calls=None):
    params = dict(
        run_id=run_id,
        status=status,
        reason=reason,
        final_url=final_url,
        elapsed_ms=elapsed_ms,
        jev_requests=jev_requests,
        text_calls=text_calls,
    )
    with conn.cursor() as cur:
        cur.execute(FINISH_RUN, params)
    conn.commit()


def upsert_prices(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(UPSERT_PRICE, rows)
    conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass, then lint**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_flights_db.py -q && PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check .`
Expected: `6 passed`, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock usecases/__init__.py usecases/db.py tests/test_flights_db.py .gitignore jev_ultrafast/model.py
git commit -m "feat(usecases): Postgres layer for flight price snapshots

Also: OpenAI-native reasoning/token params in the text helper; ignore .env.local."
```

---

### Task 2: Watches and window tiling

**Files:**
- Create: `usecases/grid.py`, `usecases/watches.json`
- Test: `tests/test_grid.py`

**Interfaces:**
- Produces:
  - `CITIES: dict[str, str]` — IATA → city name (`MIA: Miami, CCS: Caracas, MAD: Madrid, MEX: Mexico City`)
  - `@dataclass(frozen=True) Watch(name: str, origin: str, destination: str, start: date, end: date)` with properties `origin_city`, `destination_city`
  - `@dataclass(frozen=True) Window(watch: Watch, anchor_depart: date, anchor_return: date, mode: str)` with properties `columns: list[date]` (D−3…D+3), `rows: list[date]` (R−3…R+3), `expected_pairs: list[tuple[date, date]]` (all column×row with `r > d`)
  - `load_watches(path: Path) -> list[Watch]` (raises `ValueError` on unknown IATA code or `end <= start`)
  - `windows(watch: Watch) -> list[Window]` (first window `mode="full"`, rest `"direct"`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_grid.py`:

```python
"""Offline contracts for grid tiling, parsing, verification, and row shaping."""

from datetime import date
from pathlib import Path

import pytest

from usecases import grid

ROOT = Path(__file__).resolve().parents[1]


def watch(name="mia-ccs-spring", origin="MIA", destination="CCS", start=date(2027, 3, 23), end=date(2027, 4, 5)):
    return grid.Watch(name, origin, destination, start, end)


def test_load_watches_reads_the_repo_config():
    watches = grid.load_watches(ROOT / "usecases" / "watches.json")
    assert [w.name for w in watches] == ["mia-ccs-spring", "mia-mad-spring", "mia-ccs-nov", "mia-mex-xmas"]
    assert watches[3].destination_city == "Mexico City"
    assert watches[3].start == date(2026, 12, 25) and watches[3].end == date(2027, 1, 3)


def test_load_watches_rejects_unknown_airport(tmp_path):
    path = tmp_path / "w.json"
    path.write_text('[{"name": "x", "origin": "MIA", "destination": "ZZZ", "start": "2027-01-01", "end": "2027-01-09"}]')
    with pytest.raises(ValueError, match="ZZZ"):
        grid.load_watches(path)


def test_load_watches_rejects_empty_trip_box(tmp_path):
    path = tmp_path / "w.json"
    path.write_text('[{"name": "x", "origin": "MIA", "destination": "CCS", "start": "2027-01-09", "end": "2027-01-09"}]')
    with pytest.raises(ValueError, match="end"):
        grid.load_watches(path)


@pytest.mark.parametrize(
    "start, end, expected",
    [
        # Spring box: two departure columns, two return rows, clamped/deduped to three windows.
        (date(2027, 3, 23), date(2027, 4, 5), [
            (date(2027, 3, 26), date(2027, 3, 27)),
            (date(2027, 3, 26), date(2027, 4, 2)),
            (date(2027, 4, 2), date(2027, 4, 3)),
        ]),
        # One-week box: one departure column, two return rows.
        (date(2026, 11, 21), date(2026, 11, 28), [
            (date(2026, 11, 24), date(2026, 11, 25)),
            (date(2026, 11, 24), date(2026, 12, 1)),
        ]),
        # Year wrap.
        (date(2026, 12, 25), date(2027, 1, 3), [
            (date(2026, 12, 28), date(2026, 12, 29)),
            (date(2026, 12, 28), date(2027, 1, 4)),
            (date(2027, 1, 4), date(2027, 1, 5)),
        ]),
    ],
)
def test_windows_tile_the_trip_box(start, end, expected):
    result = grid.windows(watch(start=start, end=end))
    assert [(w.anchor_depart, w.anchor_return) for w in result] == expected
    assert [w.mode for w in result] == ["full"] + ["direct"] * (len(expected) - 1)


def test_window_geometry():
    w = grid.Window(watch(), date(2027, 3, 26), date(2027, 3, 27), "full")
    assert w.columns == [date(2027, 3, 23 + i) for i in range(7)]
    assert w.rows == [date(2027, 3, 24 + i) for i in range(7)]
    assert all(r > d for d, r in w.expected_pairs)
    assert (date(2027, 3, 23), date(2027, 3, 24)) in w.expected_pairs
    assert (date(2027, 3, 29), date(2027, 3, 24)) not in w.expected_pairs
    assert len(w.expected_pairs) == 42


def test_every_in_scope_pair_is_covered_by_some_window():
    w = watch()
    covered = {pair for win in grid.windows(w) for pair in win.expected_pairs}
    days = [date(2027, 3, 23 + i) for i in range(14)]
    wanted = {(d, r) for d in days for r in days if d < r}
    assert wanted <= covered
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usecases.grid'`.

- [ ] **Step 3: Create `usecases/watches.json`**

```json
[
  {"name": "mia-ccs-spring", "origin": "MIA", "destination": "CCS", "start": "2027-03-23", "end": "2027-04-05"},
  {"name": "mia-mad-spring", "origin": "MIA", "destination": "MAD", "start": "2027-03-23", "end": "2027-04-05"},
  {"name": "mia-ccs-nov",    "origin": "MIA", "destination": "CCS", "start": "2026-11-21", "end": "2026-11-28"},
  {"name": "mia-mex-xmas",   "origin": "MIA", "destination": "MEX", "start": "2026-12-25", "end": "2027-01-03"}
]
```

- [ ] **Step 4: Create `usecases/grid.py` with watches and tiling**

```python
"""Pure logic for the Google Flights date grid: watches, 7x7 window tiling, cell parsing, verification."""

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

CITIES = {"MIA": "Miami", "CCS": "Caracas", "MAD": "Madrid", "MEX": "Mexico City"}
HALF = timedelta(days=3)  # Google centres the grid on the selected pair: 3 days each side.
STEP = timedelta(days=7)


@dataclass(frozen=True)
class Watch:
    name: str
    origin: str
    destination: str
    start: date
    end: date

    @property
    def origin_city(self):
        return CITIES[self.origin]

    @property
    def destination_city(self):
        return CITIES[self.destination]


@dataclass(frozen=True)
class Window:
    watch: Watch
    anchor_depart: date
    anchor_return: date
    mode: str

    @property
    def columns(self):
        return [self.anchor_depart - HALF + timedelta(days=i) for i in range(7)]

    @property
    def rows(self):
        return [self.anchor_return - HALF + timedelta(days=i) for i in range(7)]

    @property
    def expected_pairs(self):
        return [(d, r) for d in self.columns for r in self.rows if r > d]


def load_watches(path):
    watches = []
    for item in json.loads(path.read_text()):
        for code in (item["origin"], item["destination"]):
            if code not in CITIES:
                raise ValueError(f"Unknown airport code {code}; add it to CITIES in usecases/grid.py")
        w = Watch(item["name"], item["origin"], item["destination"],
                  date.fromisoformat(item["start"]), date.fromisoformat(item["end"]))
        if w.end <= w.start:
            raise ValueError(f"Watch {w.name}: end must be after start")
        watches.append(w)
    return watches


def _anchors(start, last):
    """Anchors start+3, start+10, ... while the window's first day is still on or before `last`."""
    anchor, out = start + HALF, []
    while anchor - HALF <= last:
        out.append(anchor)
        anchor += STEP
    return out


def windows(watch):
    seen, out = set(), []
    for d in _anchors(watch.start, watch.end - timedelta(days=1)):
        for r in _anchors(watch.start, watch.end):
            if r <= d:
                r = d + timedelta(days=1)  # Google rejects a return on or before departure.
            first_depart, last_return = max(d - HALF, watch.start), min(r + HALF, watch.end)
            if first_depart >= last_return or (d, r) in seen:
                continue
            seen.add((d, r))
            out.append(Window(watch, d, r, "full" if not out else "direct"))
    return out
```

- [ ] **Step 5: Run the tests to verify they pass, then lint**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q && PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check .`
Expected: `8 passed`, `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add usecases/grid.py usecases/watches.json tests/test_grid.py
git commit -m "feat(usecases): watches and 7x7 date-grid window tiling"
```

---

### Task 3: Cell parsing and year resolution

**Files:**
- Modify: `usecases/grid.py` (append)
- Test: `tests/test_grid.py` (append)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Cell(depart: date, ret: date, price: int, flag: str | None, selected: bool, raw: str)`
  - `resolve_year(month: int, day: int, anchor: date) -> date` — nearest of anchor.year−1/0/+1
  - `parse_cell(label: str, anchor: date) -> Cell | None` — `None` when the label is not a grid cell; raises `ValueError("currency")` when a price-like label has a non-`$` currency symbol

- [ ] **Step 1: Append the failing tests to `tests/test_grid.py`**

```python
def test_parse_cell_reads_price_dates_and_flags():
    anchor = date(2026, 10, 3)
    plain = grid.parse_cell("$745, Oct 3 to Oct 10", anchor)
    assert plain == grid.Cell(date(2026, 10, 3), date(2026, 10, 10), 745, None, False, "$745, Oct 3 to Oct 10")
    low = grid.parse_cell("$689, low price, Oct 6 to Oct 7", anchor)
    assert (low.price, low.flag, low.selected) == (689, "low price", False)
    cheapest = grid.parse_cell("$1,004, cheapest price, Sep 30 to Oct 13, selected", anchor)
    assert (cheapest.price, cheapest.flag, cheapest.selected) == (1004, "cheapest price", True)
    assert cheapest.depart == date(2026, 9, 30)


@pytest.mark.parametrize("label", ["Scroll right", "Dates", "OK", "Track prices", "$745", "Oct 3 to Oct 10"])
def test_parse_cell_ignores_non_cells(label):
    assert grid.parse_cell(label, date(2026, 10, 3)) is None


def test_parse_cell_rejects_other_currencies():
    with pytest.raises(ValueError, match="currency"):
        grid.parse_cell("€745, Oct 3 to Oct 10", date(2026, 10, 3))


@pytest.mark.parametrize(
    "month, day, anchor, expected",
    [
        (12, 30, date(2027, 1, 4), date(2026, 12, 30)),  # column before a January anchor
        (1, 2, date(2026, 12, 28), date(2027, 1, 2)),    # row after a December anchor
        (3, 26, date(2027, 3, 26), date(2027, 3, 26)),
    ],
)
def test_resolve_year_picks_the_nearest_year(month, day, anchor, expected):
    assert grid.resolve_year(month, day, anchor) == expected


def test_parse_cell_year_wrap_uses_the_anchor():
    cell = grid.parse_cell("$512, Dec 30 to Jan 2", date(2026, 12, 28))
    assert (cell.depart, cell.ret) == (date(2026, 12, 30), date(2027, 1, 2))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q`
Expected: FAIL with `AttributeError: module 'usecases.grid' has no attribute 'parse_cell'`.

- [ ] **Step 3: Append parsing to `usecases/grid.py`**

```python
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
CELL = re.compile(
    r"^(?P<currency>\D{1,3}?)(?P<price>\d[\d,]*),(?: (?P<flag>low price|cheapest price),)?"
    r" (?P<dm>[A-Za-z]{3,4}) (?P<dd>\d{1,2}) to (?P<rm>[A-Za-z]{3,4}) (?P<rd>\d{1,2})(?P<selected>, selected)?$"
)


@dataclass(frozen=True)
class Cell:
    depart: date
    ret: date
    price: int
    flag: str | None
    selected: bool
    raw: str


def resolve_year(month, day, anchor):
    """Grid labels omit the year; pick the candidate nearest the anchor so Dec/Jan windows resolve correctly."""
    candidates = [date(anchor.year + delta, month, day) for delta in (-1, 0, 1)]
    return min(candidates, key=lambda d: abs((d - anchor).days))


def parse_cell(label, anchor):
    match = CELL.match(label.strip())
    if not match:
        return None
    if match["currency"] != "$":
        raise ValueError(f"Unexpected currency in {label!r}; only USD ($) is stored")
    dm, rm = MONTHS.get(match["dm"][:3].lower()), MONTHS.get(match["rm"][:3].lower())
    if not dm or not rm:
        return None
    return Cell(
        depart=resolve_year(dm, int(match["dd"]), anchor),
        ret=resolve_year(rm, int(match["rd"]), anchor),
        price=int(match["price"].replace(",", "")),
        flag=match["flag"],
        selected=bool(match["selected"]),
        raw=label,
    )
```

- [ ] **Step 4: Run the tests to verify they pass, then lint**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q && PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check .`
Expected: `19 passed`, `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add usecases/grid.py tests/test_grid.py
git commit -m "feat(usecases): parse date-grid cell labels with year resolution"
```

---

### Task 4: Goals, URLs, verification, and row shaping

**Files:**
- Modify: `usecases/grid.py` (append)
- Test: `tests/test_grid.py` (append)

**Interfaces:**
- Produces:
  - `start_url(window: Window) -> str` — homepage for `full`, `q=` URL for `direct`
  - `goal(window: Window) -> str`
  - `verify(page: dict, window: Window) -> tuple[list[Cell], str | None]` — `(cells, None)` on success, `([], reason)` on failure; `page` is the Agent's observed page (`title`, `actions`)
  - `price_rows(window: Window, cells: list[Cell], snapshot_date: date, run_id: int) -> list[dict]` — one row per `expected_pair`, `price_usd=None` when no cell

- [ ] **Step 1: Append the failing tests to `tests/test_grid.py`**

```python
def window(mode="direct"):
    return grid.Window(watch(), date(2027, 3, 26), date(2027, 4, 2), mode)


def cell_actions(anchor_depart, anchor_return, columns, rows, price=700, skip=()):
    actions = [{"id": "e1", "kind": "click", "role": "tab", "label": "Dates", "selected": "true"}]
    for d in columns:
        for r in rows:
            if r <= d or (d, r) in skip:
                continue
            selected = ", selected" if (d, r) == (anchor_depart, anchor_return) else ""
            actions.append({"id": f"e{len(actions) + 1}", "kind": "click", "role": "button",
                            "label": f"${price}, {d:%b} {d.day} to {r:%b} {r.day}{selected}"})
    return actions


def good_page(w=None, **kwargs):
    w = w or window()
    return {
        "title": f"{w.watch.origin_city} to {w.watch.destination_city} | Google Flights",
        "url": "https://www.google.com/travel/flights/search?tfs=x",
        "actions": cell_actions(w.anchor_depart, w.anchor_return, w.columns, w.rows, **kwargs),
    }


def test_start_url_and_goal_by_mode():
    full, direct = window("full"), window("direct")
    assert grid.start_url(full) == "https://www.google.com/travel/flights?hl=en&curr=USD"
    assert grid.start_url(direct) == (
        "https://www.google.com/travel/flights?hl=en&curr=USD&q="
        "Flights%20from%20MIA%20to%20CCS%20on%202027-03-26%20through%202027-04-02"
    )
    assert "Miami to Caracas" in grid.goal(full) and "March 26, 2027" in grid.goal(full)
    assert "April 2, 2027" in grid.goal(full) and "Do not select a flight" in grid.goal(full)
    assert grid.goal(direct) == "Open the date grid and stop when prices for multiple dates are visible."


def test_verify_accepts_a_centred_grid():
    cells, reason = grid.verify(good_page(), window())
    assert reason is None
    assert len(cells) == len(window().expected_pairs)
    assert sum(c.selected for c in cells) == 1


def test_verify_rejects_wrong_route_title():
    page = good_page()
    page["title"] = "Miami to Madrid | Google Flights"
    assert grid.verify(page, window()) == ([], "title mismatch: 'Miami to Madrid | Google Flights'")


def test_verify_rejects_when_dates_tab_not_selected():
    page = good_page()
    page["actions"][0]["selected"] = "false"
    assert grid.verify(page, window())[1] == "Dates tab not selected"


def test_verify_rejects_no_cells():
    page = good_page()
    page["actions"] = page["actions"][:1]
    assert grid.verify(page, window())[1] == "no grid cells parsed"


def test_verify_rejects_shifted_grid():
    w = window()
    page = good_page(w)
    shifted = cell_actions(w.anchor_depart, w.anchor_return + timedelta(days=1),
                           w.columns, [r + timedelta(days=1) for r in w.rows])
    page["actions"] = shifted
    assert grid.verify(page, w)[1].startswith("selected cell 2027-03-26/2027-04-03 != anchor")


def test_verify_rejects_currency():
    page = good_page()
    page["actions"][1]["label"] = page["actions"][1]["label"].replace("$", "€")
    assert "currency" in grid.verify(page, window())[1]


def test_price_rows_cover_expected_pairs_with_null_for_missing():
    w = window()
    missing = (date(2027, 3, 24), date(2027, 3, 31))
    cells, reason = grid.verify(good_page(w, skip={missing}), w)
    assert reason is None
    rows = grid.price_rows(w, cells, date(2026, 9, 21), run_id=7)
    assert len(rows) == len(w.expected_pairs)
    by_pair = {(r["depart_date"], r["return_date"]): r for r in rows}
    assert by_pair[missing]["price_usd"] is None and by_pair[missing]["raw_label"] is None
    present = by_pair[(date(2027, 3, 23), date(2027, 3, 30))]
    assert present["price_usd"] == 700 and present["in_scope"] is True and present["run_id"] == 7
    assert present["watch"] == "mia-ccs-spring" and present["origin"] == "MIA" and present["destination"] == "CCS"
    assert present["snapshot_date"] == date(2026, 9, 21)


def test_price_rows_flag_out_of_scope_pairs():
    w = grid.Window(watch(), date(2027, 4, 2), date(2027, 4, 3), "direct")  # rows reach Apr 6, past the box
    cells, _ = grid.verify(good_page(w), w)
    rows = grid.price_rows(w, cells, date(2026, 9, 21), run_id=1)
    late = next(r for r in rows if r["return_date"] == date(2027, 4, 6))
    assert late["in_scope"] is False
```

Add `from datetime import date, timedelta` at the top of the test file (replace the existing `from datetime import date`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q`
Expected: FAIL with `AttributeError: module 'usecases.grid' has no attribute 'start_url'`.

- [ ] **Step 3: Append to `usecases/grid.py`**

Add `from urllib.parse import quote` to the imports, then:

```python
HOME = "https://www.google.com/travel/flights?hl=en&curr=USD"


def start_url(window):
    if window.mode == "full":
        return HOME
    w = window.watch
    query = f"Flights from {w.origin} to {w.destination} on {window.anchor_depart:%Y-%m-%d} through {window.anchor_return:%Y-%m-%d}"
    return f"{HOME}&q={quote(query)}"


def _long(d):
    return f"{d:%B} {d.day}, {d.year}"


def goal(window):
    if window.mode == "direct":
        return "Open the date grid and stop when prices for multiple dates are visible."
    w = window.watch
    return (
        f"Show the date grid for round-trip flights from {w.origin_city} to {w.destination_city} "
        f"departing on {_long(window.anchor_depart)} and returning on {_long(window.anchor_return)}, "
        "one adult in economy. Stop when the date grid with prices is visible. Do not select a flight."
    )


def verify(page, window):
    """Independent checks on the final observed page. Jev's DONE is not evidence."""
    w = window.watch
    expected_title = f"{w.origin_city} to {w.destination_city} | Google Flights"
    if not page.get("title", "").startswith(expected_title):
        return [], f"title mismatch: {page.get('title')!r}"
    tabs = [a for a in page["actions"] if a.get("role") == "tab" and a["label"].strip() == "Dates"]
    if not any(a.get("selected") == "true" for a in tabs):
        return [], "Dates tab not selected"
    cells = []
    for action in page["actions"]:
        if action["kind"] != "click":
            continue
        try:
            cell = parse_cell(action["label"], window.anchor_depart)
        except ValueError as error:
            return [], str(error)
        if cell:
            cells.append(cell)
    if not cells:
        return [], "no grid cells parsed"
    selected = [c for c in cells if c.selected]
    anchor = (window.anchor_depart, window.anchor_return)
    if len(selected) != 1 or (selected[0].depart, selected[0].ret) != anchor:
        got = f"{selected[0].depart}/{selected[0].ret}" if len(selected) == 1 else f"{len(selected)} selected"
        return [], f"selected cell {got} != anchor {anchor[0]}/{anchor[1]}"
    expected = set(window.expected_pairs)
    stray = [c for c in cells if (c.depart, c.ret) not in expected]
    if stray:
        return [], f"grid shifted: {stray[0].raw!r} outside the expected window"
    return cells, None


def price_rows(window, cells, snapshot_date, run_id):
    w = window.watch
    by_pair = {(c.depart, c.ret): c for c in cells}
    rows = []
    for d, r in window.expected_pairs:
        cell = by_pair.get((d, r))
        rows.append(
            dict(
                snapshot_date=snapshot_date,
                origin=w.origin,
                destination=w.destination,
                depart_date=d,
                return_date=r,
                price_usd=cell.price if cell else None,
                flag=cell.flag if cell else None,
                in_scope=w.start <= d < r <= w.end,
                watch=w.name,
                raw_label=cell.raw if cell else None,
                run_id=run_id,
            )
        )
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass, then lint**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_grid.py -q && PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check .`
Expected: `28 passed`, `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add usecases/grid.py tests/test_grid.py
git commit -m "feat(usecases): goals, start URLs, independent grid verification, price rows"
```

---

### Task 5: Run loop and CLI

**Files:**
- Create: `usecases/flights_grid.py`
- Test: `tests/test_flights_grid.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4; `jev_ultrafast.Agent(url, goal)` with `.run()` generator, `.snapshot()` → dict with `page`, `status`, `decisions`, `text_calls`, `elapsed_ms`; `jev_ultrafast.demo.load_environment()`.
- Produces:
  - `run_window(conn, window, snapshot_date, agent_factory=Agent) -> dict(status, reason, min_in_scope, run_id)`
  - `main(argv=None) -> int` — exit code 0/1

- [ ] **Step 1: Write the failing tests**

Create `tests/test_flights_grid.py`:

```python
"""Offline contract for the run loop: a fake Agent and a fake DB stand in for Google, Jev, and Postgres."""

from datetime import date
from unittest.mock import Mock

import pytest

from usecases import flights_grid, grid
from tests.test_grid import good_page, watch, window


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
        return {"page": self._page, "status": self._status, "decisions": [1, 2, 3], "text_calls": [1], "elapsed_ms": 4321}

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
    result = flights_grid.run_window(Mock(), w, date(2026, 9, 21), agent_factory=factory(page=good_page(w), status="blocked"))
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
    monkeypatch.setattr(flights_grid, "Agent", lambda url, goal: FakeAgent(url, goal, page={"title": "x", "actions": []}))
    code = flights_grid.main(["--watch", "mia-ccs-nov", "--date", "2026-09-21"])
    out = capsys.readouterr().out
    assert code == 1
    assert "mia-ccs-nov" in out and "failed" in out
    assert len(FakeAgent.instances) == 2  # the Nov watch tiles to two windows


def test_main_returns_zero_when_every_window_verifies(monkeypatch, capsys):
    monkeypatch.setattr(flights_grid, "load_environment", lambda: None)
    monkeypatch.setattr(flights_grid.db, "connect", lambda: Mock())
    monkeypatch.setattr(flights_grid.db, "ensure_schema", lambda conn: None)

    def agent(url, goal):
        w = next(win for win in grid.windows(watch(start=date(2026, 11, 21), end=date(2026, 11, 28)))
                 if grid.start_url(win) == url or (win.mode == "full" and url == grid.HOME))
        return FakeAgent(url, goal, page=good_page(w))

    monkeypatch.setattr(flights_grid, "Agent", agent)
    code = flights_grid.main(["--watch", "mia-ccs-nov", "--date", "2026-09-21"])
    assert code == 0
    assert "ok" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest tests/test_flights_grid.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usecases.flights_grid'`.

- [ ] **Step 3: Create `usecases/flights_grid.py`**

```python
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


def run_window(conn, window, snapshot_date, agent_factory=Agent):
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
        metrics = dict(
            final_url=state["page"]["url"] if state.get("page") else None,
            elapsed_ms=state["elapsed_ms"],
            jev_requests=len(state["decisions"]),
            text_calls=len(state["text_calls"]),
        )
        if state["status"] != "done":
            result["reason"] = f"agent stopped with status {state['status']!r}"
        else:
            cells, reason = grid.verify(state["page"], window)
            if reason:
                result["reason"] = reason
            else:
                rows = grid.price_rows(window, cells, snapshot_date, run_id)
                db.upsert_prices(conn, rows)
                prices = [r["price_usd"] for r in rows if r["in_scope"] and r["price_usd"] is not None]
                result.update(status="ok", min_in_scope=min(prices) if prices else None)
    except Exception as error:  # noqa: BLE001 - one window must never end the day's run.
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
```

- [ ] **Step 4: Run the tests to verify they pass, then lint the whole repo**

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest -q && PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check .`
Expected: `71 passed` (31 existing + 6 + 28 + 6), `All checks passed!`. If ruff flags the `# noqa: BLE001` as unused (BLE is not in the selected rules), remove that comment.

- [ ] **Step 5: Commit**

```bash
git add usecases/flights_grid.py tests/test_flights_grid.py
git commit -m "feat(usecases): flights grid run loop and CLI"
```

---

### Task 6: Live smoke, docs, and launchd

**Files:**
- Create: `docs/flights-grid.md`
- Modify: `README.md` (add a "Use cases" section after "Use the library")

- [ ] **Step 1: Run one watch live**

Requires Chrome open with remote debugging, the harness daemon connected (`uv run browser-harness --doctor`), and `.env` with all keys.

Run: `PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run --env-file .env python -m usecases.flights_grid --watch mia-ccs-nov`
Expected: two lines (one `full`, one `direct`) ending in `ok min in-scope $NNN`, then `all windows ok`, exit 0. If a window fails, read its reason: a `title mismatch` on the `full` run usually means Jev stopped before the grid; rerun once before changing code.

- [ ] **Step 2: Check the rows in Postgres**

Run:

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -c "
SELECT watch, mode, status, reason, elapsed_ms, jev_requests, text_calls FROM grid_runs ORDER BY id;
SELECT depart_date, return_date, price_usd, flag, in_scope FROM flight_prices
 WHERE watch='mia-ccs-nov' AND in_scope ORDER BY price_usd NULLS LAST LIMIT 10;"
```

Expected: two `ok` runs; ten priced rows inside Nov 21–28.

- [ ] **Step 3: Write `docs/flights-grid.md`**

```markdown
# Flights date-grid tracker

Daily snapshots of Google Flights' round-trip **date grid** for the watches in
`usecases/watches.json`, stored in Postgres. Jev drives the browser; code verifies the
final page and parses the 7×7 cells. Design: `docs/superpowers/specs/2026-09-21-flights-grid-design.md`.

## Run

```bash
uv run --env-file .env python -m usecases.flights_grid             # every watch
uv run --env-file .env python -m usecases.flights_grid --watch mia-mad-spring
```

Needs Chrome open with `chrome://inspect/#remote-debugging` allowed, the Browser Harness
daemon connected (`uv run browser-harness --doctor`), and in `.env`: `TYPESAFE_API_KEY`,
`TEXT_MODEL_*`, `DATABASE_URL`. Exit code is 1 if any window failed; each window's reason is
in `grid_runs.reason`.

Each watch tiles its trip box into 7×7 windows. The first window of each watch starts from
the Flights homepage (full Jev run, text model types the cities); the rest start from a
`?q=Flights from MIA to CCS on ... through ...` URL and only open the grid.

## Watches

Edit `usecases/watches.json`. `start`/`end` bound the whole trip (`start <= depart < return <= end`).
Airport codes must exist in `CITIES` in `usecases/grid.py`.

## Query

```sql
-- lowest in-scope fare per day for one watch
SELECT snapshot_date, min(price_usd) AS min_usd
  FROM flight_prices WHERE watch = 'mia-mad-spring' AND in_scope
 GROUP BY 1 ORDER BY 1;

-- today's cheapest pairs
SELECT depart_date, return_date, price_usd, flag
  FROM flight_prices WHERE watch = 'mia-ccs-spring' AND snapshot_date = current_date AND in_scope
 ORDER BY price_usd NULLS LAST LIMIT 10;
```

## Schedule with launchd (daily 07:00)

Save as `~/Library/LaunchAgents/com.shagala.jev-flights.plist`, replacing the two paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shagala.jev-flights</string>
  <key>ProgramArguments</key><array>
    <string>/Users/shagala/.pyenv/versions/3.11.9/bin/uv</string>
    <string>run</string><string>--env-file</string><string>.env</string>
    <string>python</string><string>-m</string><string>usecases.flights_grid</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/shagala/Documents/Projects/jev-ultrafast</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/shagala/Documents/Projects/jev-ultrafast/artifacts/flights-grid/out.log</string>
  <key>StandardErrorPath</key><string>/Users/shagala/Documents/Projects/jev-ultrafast/artifacts/flights-grid/err.log</string>
</dict></plist>
```

```bash
mkdir -p artifacts/flights-grid
launchctl load ~/Library/LaunchAgents/com.shagala.jev-flights.plist
launchctl start com.shagala.jev-flights      # run now to test
launchctl unload ~/Library/LaunchAgents/com.shagala.jev-flights.plist   # stop
```

launchd runs a missed job when the Mac wakes. Chrome must be open.

## Limits

USD only; other currencies fail the window. Google consent or rate-limit pages fail
verification and are recorded, not worked around. One window's failure never stops the run;
rerunning the same day upserts.
```

- [ ] **Step 4: Add a short section to `README.md`**

Insert after the "Use the library" section (before "## Why it moves"):

```markdown
## Use cases (this fork)

- **Flights date-grid tracker** — daily round-trip price snapshots for the watches in `usecases/watches.json`, stored in Postgres. `uv run --env-file .env python -m usecases.flights_grid`. See [docs/flights-grid.md](docs/flights-grid.md).
```

- [ ] **Step 5: Full checks**

Run:

```bash
PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run ruff check . && \
PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv run pytest -q && \
node --check jev_ultrafast/static/app.js && node --check jev_ultrafast/snapshot.js && \
PATH="$HOME/.pyenv/versions/3.11.9/bin:$PATH" uv build
```

Expected: all clean, `71 passed`, wheel built.

- [ ] **Step 6: Commit**

```bash
git add docs/flights-grid.md README.md
git commit -m "docs: flights grid tracker usage, queries, and launchd schedule"
```
