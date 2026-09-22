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
`TEXT_MODEL_*`, `DATABASE_URL`. Exit code is 1 if any window failed; each attempt's reason is
in `grid_runs.reason`.

Each watch tiles its trip box into 7×7 windows centred on an anchor pair. The first window
of each watch starts from the Flights homepage (`mode = full`: a real end-to-end Jev run in
which the text model types the cities and Jev works the calendar). The remaining windows
start from a `?q=Flights from MIA to CCS on ... through ...` URL and only open the grid
(`mode = direct`, ~2 s and 5 Jev requests each). If a full run fails, the window is retried
in direct mode so the day's data is never lost; both attempts are recorded.

The full run is the flaky one: in testing it failed on a calendar double-click race inside
the agent loop, on text-helper mistakes (`gpt-4.1-nano` typed the wrong city once and
returned invalid JSON once; `gpt-4.1-mini` did better), and it tends to thrash on the grid
dialog before choosing DONE. Treat it as the daily agent exercise, not the data source.

## Watches

Edit `usecases/watches.json`. `start`/`end` bound the whole trip (`start <= depart < return <= end`).
Airport codes must exist in `CITIES` in `usecases/grid.py`. Google also renders same-day
returns; those cells are stored with `in_scope = false`.

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

-- what ran today and why anything failed
SELECT watch, mode, anchor_depart, anchor_return, status, reason, elapsed_ms, jev_requests
  FROM grid_runs WHERE snapshot_date = current_date ORDER BY id;
```

## Schedule with launchd (daily 07:00)

Save as `~/Library/LaunchAgents/com.shagala.jev-flights.plist`, adjusting the paths:

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
