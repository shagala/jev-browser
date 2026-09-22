# Flights date-grid price tracker — design

Status: approved 2026-09-21. First use case in the `shagala/jev-browser` fork; proof of concept.

## Goal

Once a day, capture Google Flights' round-trip **date grid** prices for a configurable list of
watches (route + trip box) and store every cell in Postgres, so price movement per
depart/return pair can be queried over time. Jev drives the browser; code extracts and
verifies; no site-specific selectors and no model in the extraction path.

## Inputs

`usecases/watches.json` — a list of watches. Each is `(name, origin, destination, start, end)`
where `start`/`end` bound the whole trip: `start <= depart < return <= end`.

Initial watches:

| name | route | start | end |
| --- | --- | --- | --- |
| mia-ccs-spring | MIA→CCS | 2027-03-23 | 2027-04-05 |
| mia-mad-spring | MIA→MAD | 2027-03-23 | 2027-04-05 |
| mia-ccs-nov | MIA→CCS | 2026-11-21 | 2026-11-28 |
| mia-mex-xmas | MIA→MEX | 2026-12-25 | 2027-01-03 |

CLI: `uv run --env-file .env python -m usecases.flights_grid [--watch NAME ...] [--date YYYY-MM-DD]`.
`--date` overrides `snapshot_date` (default: today, local time) for reruns/backfills of the
audit key only; prices are always whatever Google shows at run time.

Environment: `TYPESAFE_API_KEY`, `TEXT_MODEL_*` (existing), `DATABASE_URL` (new; Neon
`flightprice`, pooler endpoint, `sslmode=require`).

## Window tiling

Google centres the grid on the selected pair: departures `D-3..D+3` (columns) and returns
`R-3..R+3` (rows), 7×7 cells. For a watch:

- departure anchors: `start+3, start+10, ...` while `anchor-3 <= end-1`
- return anchors: `start+3, start+10, ...` while `anchor-3 <= end`
- one window per (departure anchor, return anchor) whose column range and row range can
  contain at least one in-scope pair (`depart < return`).

Anchors are clamped so `anchor_return > anchor_depart` (Google rejects otherwise): if a
tile would put `R <= D`, use `R = D+1`. Windows are independent: each is its own agent run.

Cells observed outside the trip box are stored with `in_scope = false`.

## Navigation (hybrid, approach "c")

- **First window of each watch — `mode = 'full'`.** Start at
  `https://www.google.com/travel/flights?hl=en&curr=USD`. Goal:
  "Show the date grid for round-trip flights from Miami to Caracas departing on
  {anchor_depart:%B %-d, %Y} and returning on {anchor_return:%B %-d, %Y}, one adult in economy.
  Stop when the date grid with prices is visible. Do not select a flight." Exercises the full
  loop daily (city typing via the text model, calendar clicks, opening the grid).
- **Remaining windows — `mode = 'direct'`.** Start at
  `https://www.google.com/travel/flights?hl=en&curr=USD&q=Flights from {ORIGIN} to {DEST} on
  {anchor_depart:%Y-%m-%d} through {anchor_return:%Y-%m-%d}`. Goal: "Open the date grid and
  stop when prices for multiple dates are visible." ~2 Jev requests, no text model.

Jev never pages the grid. The agent runs until `DONE`, `BLOCKED`, or the existing budgets.

## Verification (independent of DONE)

A window's final observed page is accepted only if all hold:

1. `title` starts with `"{Origin city} to {Destination city} | Google Flights"`.
2. A `tab` action labelled `Dates` has `selected == "true"`.
3. At least one grid cell parses (below).
4. The cell flagged `selected` parses to exactly `(anchor_depart, anchor_return)`.
5. Every parsed price carries `$` (USD); any other currency symbol fails the window.

Failure records `status='failed'` with the failing check as `reason` and continues.

## Extraction

From the final page's `actions`, every `kind == 'click'` action whose label matches

```
^\$(?P<price>[\d,]+),(?: (?P<flag>low price|cheapest price),)? (?P<dm>\w{3}) (?P<dd>\d{1,2}) to (?P<rm>\w{3}) (?P<rd>\d{1,2})(?P<selected>, selected)?$
```

Year assignment: labels omit the year. For each date, choose the year in
`{anchor.year-1, anchor.year, anchor.year+1}` that minimises `|date - anchor|`. Handles
Dec→Jan wraps (e.g. the mia-mex-xmas watch).

Expected pairs: all 49 `(d, r)` in the window's column × row ranges with `r > d`. Pairs
with no matching button are stored with `price_usd = NULL` (Google shows no fare). Cells
that parse but are not in the expected set (grid shifted) fail verification check 4.

`raw_label` keeps Google's original string for debugging.

## Schema

Created idempotently on every run (`CREATE TABLE IF NOT EXISTS`); no migration tool.

```sql
CREATE TABLE IF NOT EXISTS grid_runs (
  id            bigserial PRIMARY KEY,
  snapshot_date date        NOT NULL,
  watch         text        NOT NULL,
  origin        text        NOT NULL,
  destination   text        NOT NULL,
  anchor_depart date        NOT NULL,
  anchor_return date        NOT NULL,
  mode          text        NOT NULL,   -- 'full' | 'direct'
  status        text        NOT NULL,   -- 'running' | 'ok' | 'failed'
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
  price_usd     integer,               -- NULL = no fare shown
  flag          text,                  -- 'low price' | 'cheapest price' | NULL
  in_scope      boolean NOT NULL,
  watch         text    NOT NULL,
  raw_label     text,
  run_id        bigint  NOT NULL REFERENCES grid_runs(id),
  captured_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, origin, destination, depart_date, return_date)
);
```

Reruns on the same `snapshot_date` upsert both tables (`ON CONFLICT ... DO UPDATE`), so
retrying a failed window never duplicates.

## Run flow

1. Load `.env`; `db.ensure_schema()`; load watches; filter by `--watch`.
2. For each watch, for each window: insert/upsert the `grid_runs` row as `running`; run the
   `Agent` (context manager, so the tab always closes); verify; extract; upsert prices;
   update the run row to `ok`/`failed` with metrics (`elapsed_ms`, `len(decisions)`,
   `len(text_calls)`, final URL).
3. Any exception inside a window (provider HTTP error, `StalePage` loop, budget) is caught,
   written as `failed` with `reason = str(error)`, and the loop continues.
4. Print a summary (per watch: windows ok/failed, minimum in-scope price) and exit `1` if any
   window failed.

## Files

- `usecases/__init__.py`
- `usecases/db.py` — `connect()`, `ensure_schema()`, `start_run()`, `finish_run()`,
  `upsert_prices()`; psycopg 3, autocommit off, one transaction per window.
- `usecases/flights_grid.py` — watches loading, tiling, goals/URLs, parsing, verification,
  main loop, summary.
- `usecases/watches.json`
- `tests/test_flights_grid.py` — offline: tiling for the four watches (exact anchors), label
  parsing incl. flags/selected/year-wrap, in-scope classification, verification rejecting
  wrong title/anchor/currency, missing cells → NULL rows, upsert SQL/params via a fake
  connection.
- `docs/flights-grid.md` — how to run, the launchd plist example, example trend query.
- `pyproject.toml` — add `psycopg[binary]>=3.2,<4`.

No changes to `jev_ultrafast/` beyond the `model.py` OpenAI-compat fix already made.

## Scheduling

`docs/flights-grid.md` includes `~/Library/LaunchAgents/com.shagala.jev-flights.plist`
running daily at 07:00 local with stdout/stderr to `artifacts/flights-grid/`. Requirements:
Chrome open with remote debugging allowed, Browser Harness daemon reachable, Mac awake (launchd
runs a missed job on wake).

## Out of scope

Indeed resume downloads (use case 2), one-way scans, paging within a grid, currencies other
than USD, alerting, and any dashboard. Google consent or rate-limit pages are reported as
failed windows, not worked around.
