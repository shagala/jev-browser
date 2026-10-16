"""The recipe catalog: list, record, replay, repair, and run whatever is due.

    uv run --env-file .env python -m usecases.run list
    uv run --env-file .env python -m usecases.run record <name> --goal '...' --url https://...
    uv run --env-file .env python -m usecases.run run <name> [--no-repair] [--param k=v]
    uv run --env-file .env python -m usecases.run due

Repair is a re-record, never a splice: `Agent` builds its own tab, so there is no way to
hand it a live mid-recipe page, and replaying a prefix to reach the broken step would
re-execute mutations. Re-deriving the whole recipe from the original goal costs one agent
run — which a break was going to cost anyway.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jev_ultrafast import Agent, Browser
from jev_ultrafast.demo import load_environment

from . import player, recorder
from . import recipe as recipes

LEDGER = Path("artifacts/runs.jsonl")


def ledger_entries(path=LEDGER):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def record_run(entry, path=LEDGER):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(entry) + "\n")


def last_success(name, entries):
    runs = [e for e in entries if e["recipe"] == name and e["status"] == "ok"]
    return datetime.fromisoformat(runs[-1]["at"]) if runs else None


def is_due(recipe, entries, now=None):
    """`daily@HH:MM` fires once a day on or after that local time; `hourly` every hour."""
    now = now or datetime.now(timezone.utc).astimezone()
    schedule = (recipe.schedule or "").strip()
    if not schedule or schedule == "manual":
        return False
    previous = last_success(recipe.name, entries)
    if schedule == "hourly":
        return previous is None or now - previous >= timedelta(hours=1)
    if schedule.startswith("daily@"):
        hour, _, minute = schedule.removeprefix("daily@").partition(":")
        at = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        if now < at:
            return False
        return previous is None or previous.astimezone(now.tzinfo).date() < now.date()
    raise ValueError(f"{recipe.name}: unknown schedule {schedule!r}")


def params_for(recipe, overrides):
    params = dict(overrides)
    if recipe.params_from:
        source = Path(__file__).with_name(recipe.params_from)
        params = {**json.loads(source.read_text()), **params}
    return params


def run_recipe(recipe, params, *, repair=True, on_event=None, browser_factory=Browser,
               agent_factory=Agent, root=recipes.RECIPES):
    """Replay; on a pre-mutation failure, re-record and adopt the new version."""
    emit = on_event or (lambda *_args: None)
    url = recipe.start["url"].format(**params)
    browser = browser_factory(url)
    try:
        result = player.replay(browser, recipe, params, on_event=emit)
    finally:
        browser.close()
    if result["status"] == "ok" or not result["repairable"]:
        if result["status"] != "ok":
            emit("abandoned", result["step"], f"{result['failure']} is not safe to re-run")
        return {**result, "recipe": recipe}
    if not repair:
        return {**result, "recipe": recipe}

    emit("repairing", result["step"], f"{result['failure']}: re-recording from the original goal")
    try:
        fresh = recorder.record(recipe.name, recipe.provenance["goal"], url,
                                agent_factory=agent_factory, on_event=emit)
    except Exception as error:
        emit("repair_failed", result["step"], str(error))
        return {**result, "status": "failed", "failure": "repair_failed",
                "reason": str(error), "recipe": recipe}
    adopted = recipe.bumped(fresh.steps, fresh.provenance,
                            reason=f"{result['failure']} on {result['step']}: {result['reason']}")
    recipes.save(adopted, root=root)
    emit("repaired", None, f"{recipe.name} v{adopted.version}")
    return {"status": "repaired", "step": result["step"], "failure": result["failure"],
            "reason": result["reason"], "repairable": True, "recipe": adopted}


def report(stage, step, detail):
    print(f"  {stage:14} {step or '':4} {detail}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    due = sub.add_parser("due")
    due.add_argument("--no-repair", action="store_true")
    new = sub.add_parser("record")
    new.add_argument("name")
    new.add_argument("--goal", required=True)
    new.add_argument("--url", required=True)
    new.add_argument("--schedule")
    new.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    play = sub.add_parser("run")
    play.add_argument("name")
    play.add_argument("--no-repair", action="store_true")
    play.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    load_environment()

    if args.command == "list":
        for item in recipes.load_all():
            previous = last_success(item.name, ledger_entries())
            print(f"{item.name:24} v{item.version:<3} {item.schedule or 'manual':12} "
                  f"{len(item.steps)} steps  last ok: {previous or 'never'}")
        return 0

    if args.command == "record":
        made = recorder.record(args.name, args.goal, args.url, schedule=args.schedule,
                               params=dict(p.split("=", 1) for p in args.param), on_event=report)
        path = recipes.save(made)
        print(f"recorded {len(made.steps)} step(s) → {path}", flush=True)
        return 0

    selected = (
        [r for r in recipes.load_all() if is_due(r, ledger_entries())]
        if args.command == "due"
        else [recipes.load(args.name)]
    )
    overrides = dict(pair.split("=", 1) for pair in getattr(args, "param", []))
    failures = 0
    print(f"{len(selected)} recipe(s) to run", flush=True)
    for item in selected:
        print(f"{item.name} v{item.version}", flush=True)
        result = run_recipe(item, params_for(item, overrides), repair=not args.no_repair, on_event=report)
        failures += result["status"] == "failed"
        record_run({"recipe": item.name, "at": datetime.now(timezone.utc).astimezone().isoformat(),
                    "status": result["status"], "failure": result["failure"],
                    "version": result["recipe"].version})
        print(f"  → {result['status']}" + (f" ({result['reason']})" if result["reason"] else ""), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
