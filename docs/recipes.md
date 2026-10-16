# Recipes

A recipe is what Jev did, recorded once and replayed with **zero model calls**. The
catalog lives in `usecases/recipes/*.json`; one launchd job runs whatever is due.

```bash
uv run --env-file .env python -m usecases.run list
uv run --env-file .env python -m usecases.run record <name> --goal '…' --url '…' \
    [--param k=v] [--schedule daily@07:00] \
    [--params-from module:function] [--extract module:function] [--store module:function]
uv run --env-file .env python -m usecases.run run <name> [--param k=v] [--no-repair]
uv run --env-file .env python -m usecases.run due
```

Needs Chrome open with `chrome://inspect/#remote-debugging` allowed and the Browser
Harness daemon connected (`uv run browser-harness --doctor`). Recording also needs
`TYPESAFE_API_KEY` and `TEXT_MODEL_*`; replay needs neither.

## How a recipe is made

`record` runs the ordinary agent against a natural-language goal, then keeps what it
chose. It walks the observation stream rather than the run history, because history
keeps an element's label but not its role or value, and its action ids are reassigned on
the next snapshot.

Each step stores a **descriptor** — kind, role, normalised label, plus how many
competing candidates existed when it was recorded — never an element id, which is
meaningless in the next document. `--url` is stored as written, so one recipe serves
every parameter set; `--param` only fills in the page actually visited while recording.

Scroll and wait are not recorded. Finding an element is a replay-time ladder instead,
because an off-screen element is not in the observation at all and a recorded scroll
count would not survive a layout change.

The ladder has two dimensions, because an element can be missing for two reasons. It may
be outside the viewport, which scrolling answers, or it may not have rendered yet, which
waiting answers — and the first step of a recipe observes the instant the tab opens, with
nothing before it to have waited. A match on the first look pays for neither.

## Handing the work back to code

A recipe owns the navigation and nothing else. Three optional handlers, named at record
time, point at functions in `usecases/` — names only, never code, and the resolver
refuses anything outside the package:

| Flag | Signature | Job |
| --- | --- | --- |
| `--params-from` | `() -> [params, …]` | fan one recipe out over the work it applies to |
| `--extract` | `(browser, params) -> (records, problem)` | the terminal verifier |
| `--store` | `(records, params) -> detail` | persist what the verifier passed |

Replay reporting `ok` is not evidence the task succeeded — it only means every step
resolved and acted. `--extract` is the authority, and the runner, not the handler,
refuses to store when it reports a problem.

With `--params-from`, a `--param` on the command line **filters** the sets instead of
replacing them, so `run flights-date-grid --param watch=mia-ccs-nov` reruns one window.

The flights recipe wires all three:

```bash
--params-from usecases.flights_grid:param_sets \
--extract     usecases.flights_grid:extract \
--store       usecases.flights_grid:store
```

`param_sets` yields one set per `direct` window; the `full` homepage run stays in
`usecases.flights_grid` as the daily agent canary that keeps the generality claim honest.

## What replay actually buys

Measured on 2026-10-16, the flights recipe over all 7 windows:

| | model calls / window | time |
| --- | --- | --- |
| agent, `direct` mode | 4.4 Jev requests | 1.7 s of agent loop |
| replay | **0** | 3.3 s per window, 24 s for all 7 |

The two time columns are **not** comparable and neither should be quoted as the other:
the agent figure is `grid_runs.elapsed_ms`, the loop alone, while the replay figure is
everything — opening the tab, navigation, the grid's async render, and the Postgres
write. Page load dominates both.

So the win is cost and determinism, not speed. Replay makes no paid request and takes
the same path every time; a test proves it by monkeypatching `model.post_json` to raise.

## How replay fails

| Failure | Meaning | Repairable |
| --- | --- | --- |
| `no_match` | the descriptor resolved to nothing, after scrolling and waiting | yes |
| `ambiguous:N` | N candidates where the recording saw a different number | yes |
| `unreliable` | the observation was truncated at 250 actions, so a miss proves nothing | yes |
| `stale_pre` | the page changed before any input was dispatched | yes |
| `stale_post` | a native select may already have fired its change event | **no** |
| `expect` | the step acted, but the page did not reach the expected state | **no** |
| `verify` | the steps ran, but the terminal verifier rejected the result | **no** |

Repairable means no input was dispatched, so re-running from a clean start is safe.
The last three already changed the page; re-running would repeat the mutation, which is
why they abort instead and leave the break for a person to read.

## Repair

A repair is a **re-record** from the recipe's original goal, never a surgical splice.
The new steps are adopted as a new version, and the previous body is kept in `history`
for diffing and rollback. Recipes are committed to git, so an auto-applied repair shows
up as a reviewable diff.

`--no-repair` replays without that safety net — the clean signal for whether a recipe
still holds.

A successful repair is worth reading, not ignoring: it means the site changed.

## What `expect` is for

`expect` is a tripwire that localises a break, **not** verification. It polls rather than
reading once (pages render asynchronously), and it supports `count` and `must_not` as
well as `all_of`.

What a recording can put there is limited, and deliberately so: the recorder derives the
poll, `require_page_changed`, and an `all_of` chaining each step to the next step's
target. It cannot invent a `count` or a `must_not`, because choosing those takes
knowledge of the site — and writing them in by hand is exactly the hand-authoring that
makes a recipe stop being a trace.

So the flights recipe, being one step, has a thin `expect`, and the surface check lives
where the authority already is: `grid.verify`, code no recipe can rewrite. It is what
rejects Google's calendar picker, which renders a price per day and was once mistaken
for the grid. `tests/test_fixtures.py` pins that against real captured observations of
both surfaces.

## Watching one run

`python -m jev_ultrafast.demo` serves the inspector on <http://127.0.0.1:8766>. Below the
agent panels there is a **Recipes** section: every recorded recipe with its version,
schedule, steps and last success, and two buttons — **Replay** (no repair) and **Run with
repair**.

Replay makes no decisions, so the operation-choice and target-ranking panels have nothing
to show for it. Its view is the step ladder instead: each stage, the step it belongs to
and what came back, filling in live, then one row per parameter set with its timing. That
ladder is what you want in front of you when a recipe breaks.

A run goes on its own thread with its own lock, so the inspector stays steppable while a
multi-minute replay is going, and a second run while one is in flight is refused rather
than queued. Recording a *new* recipe is still a CLI job — it needs a name, a goal, a URL
template and its handlers.

The scenario dropdown also gained **Any URL…**, which accepts an `http(s)` address for an
ordinary agent run. The server resolves it and refuses private, loopback and link-local
addresses, since this is a web page asking a local service to drive a real browser. That
check is best effort, not a guarantee: the name is resolved here and again by Chrome.

## Scheduling

Each recipe declares `schedule`: `daily@HH:MM`, `hourly`, or `manual` (the default).
One launchd job covers the catalog. Adding a recipe never means touching launchd:

```bash
mkdir -p artifacts/recipes
cp scripts/com.shagala.jev-recipes.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.shagala.jev-recipes.plist
launchctl start com.shagala.jev-recipes     # run now
launchctl unload ~/Library/LaunchAgents/com.shagala.jev-recipes.plist
```

It ticks hourly, which is what lets a `daily@07:00` recipe that failed at 07:00 finish
its work at 08:00 rather than waiting a day. Chrome must be open with remote debugging
allowed — that toggle resets when Chrome restarts.

Runs are appended to `artifacts/runs.jsonl`, one entry per recipe run, which is how
`due` knows what already ran. A recipe counts as done for the day only when **every**
parameter set succeeded, so a partial failure stays due and the next tick retries it.
