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
  UNIQUE (snapshot_date, origin, destination, anchor_depart, anchor_return, mode)
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
ON CONFLICT (snapshot_date, origin, destination, anchor_depart, anchor_return, mode) DO UPDATE SET
  watch = EXCLUDED.watch, status = 'running', reason = NULL, final_url = NULL,
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


def finish_run(
    conn, run_id, *, status, reason=None, final_url=None, elapsed_ms=None, jev_requests=None, text_calls=None
):
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
