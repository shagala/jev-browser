# Recipes

A recipe is what Jev did, recorded once and replayed with **zero model calls**. The
catalog lives in `usecases/recipes/*.json`; one launchd job runs whatever is due.

```bash
uv run --env-file .env python -m usecases.run list
uv run --env-file .env python -m usecases.run record <name> --goal '…' --url '…' [--param k=v] [--schedule daily@07:00]
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

## How replay fails

| Failure | Meaning | Repairable |
| --- | --- | --- |
| `no_match` | the descriptor resolved to nothing, after scrolling | yes |
| `ambiguous:N` | N candidates where the recording saw a different number | yes |
| `unreliable` | the observation was truncated at 250 actions, so a miss proves nothing | yes |
| `stale_pre` | the page changed before any input was dispatched | yes |
| `stale_post` | a native select may already have fired its change event | **no** |
| `expect` | the step acted, but the page did not reach the expected state | **no** |

Repairable means no input was dispatched, so re-running from a clean start is safe.
The last two already changed the page; re-running would repeat the mutation, which is
why they abort instead.

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
reading once (pages render asynchronously), asserts cardinality rather than existence,
and can carry `must_not` assertions. That combination is what distinguishes Google's real
date grid from its calendar picker, which also renders a price per day.

The authority stays a code-owned verifier that no recipe can rewrite.

## Scheduling

Each recipe declares `schedule`: `daily@HH:MM`, `hourly`, or `manual` (the default).
One launchd job covers the catalog — see the plist in `docs/flights-grid.md`, changing
the arguments to `-m usecases.run due`. Adding a recipe never means touching launchd.

Runs are appended to `artifacts/runs.jsonl`, which is how `due` knows what already ran.
