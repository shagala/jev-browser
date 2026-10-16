"""Turn one agent run into a recipe.

`state["history"]` alone is not enough: it keeps the label but no role, no value and no
node (agent.py:121-141), and its `choice` is an action id that is reassigned on the next
snapshot (snapshot.js:101). So the recorder walks the observation stream instead, pairing
each executed action with the page it was chosen against — the yield before it.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from jev_ultrafast import Agent

from . import match as m
from .recipe import Match, Recipe, Step

SOURCES = ("snapshot.js", "browser.py", "model.py", "agent.py")
SKIP = {"scroll", "wait"}
OPS = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}


def source_hashes():
    root = Path(__file__).resolve().parents[1] / "jev_ultrafast"
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()[:16] for name in SOURCES}


def param_name(label):
    slug = re.sub(r"[^a-z0-9]+", "_", m.normalize(label).lower()).strip("_")
    return slug or "value"


def descriptor_for(action, page):
    """Build the descriptor, counting its live competition so replay can detect a change."""
    kind, role, label = action["kind"], action.get("role"), action.get("label", "")
    peers = [
        other
        for other in page["actions"]
        if other["kind"] == kind
        and other.get("role") == role
        and m.labels_match(other.get("label", ""), label, "normalized")
    ]
    ordinal = next((i for i, other in enumerate(peers) if other is action), 0)
    return Match(
        kind=kind,
        role=role,
        label=m.normalize(label),
        mode="normalized",
        ordinal=ordinal,
        expected_candidates=max(len(peers), 1),
    )


def step_from(entry, page, index):
    """One executed history entry plus the page it was decided against -> one Step."""
    action = next((a for a in page["actions"] if a["id"] == entry["choice"]), None)
    if action is None or action["kind"] in SKIP:
        return None
    step = Step(
        id=f"s{index}",
        intent=entry["action"],
        op=OPS.get(action["kind"], entry["operation"]),
        match=descriptor_for(action, page),
        mutating=action["kind"] in {"fill", "select"} or bool(entry.get("page_changed")),
        value_from={"param": param_name(action.get("label", ""))} if action["kind"] == "fill" else None,
        expect={"poll": {"attempts": 10, "interval_ms": 300},
                **({"require_page_changed": True} if entry.get("page_changed") else {})},
    )
    return step


def chain_expectations(steps):
    """Each step must leave the page where the next step can find its target.

    A tripwire, not verification: its job is to stop early and name the broken step. The
    authority stays the code-owned terminal verifier, which no recipe can rewrite.
    """
    chained = []
    for step, following in zip(steps, [*steps[1:], None]):
        if following is not None and following.match is not None:
            target = following.match
            assertion = {"kind": target.kind, "label": target.label, "mode": target.mode}
            if target.role:
                assertion["role"] = target.role
            step = Step(**{**step.__dict__, "expect": {**step.expect, "all_of": [assertion]}})
        chained.append(step)
    return chained


def record(name, goal, start_url, *, agent_factory=Agent, schedule=None, on_event=None, **extra):
    """Run the agent once and keep what it did. The NL goal stays the source of truth."""
    emit = on_event or (lambda *_args: None)
    steps, seen, model = [], 0, None
    with agent_factory(start_url, goal) as agent:
        previous = agent.snapshot()["page"]
        for state in agent.run():
            history = state["history"]
            if len(history) > seen:
                seen = len(history)
                step = step_from(history[-1], previous, len(steps) + 1)
                if step is not None:
                    steps.append(step)
                    emit("recorded", step.id, step.intent)
            previous = state["page"]
        final = agent.snapshot()
        if final["status"] != "done":
            raise ValueError(f"agent finished {final['status']!r}; nothing recorded")
        model = (final["decisions"] or [{}])[-1].get("model")
    if not steps:
        raise ValueError("the agent reached DONE without executing a recordable action")
    return Recipe(
        name=name,
        start={"url": start_url},
        steps=chain_expectations(steps),
        schedule=schedule,
        provenance={
            "goal": goal,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": model or "unknown",
            "source_hashes": source_hashes(),
        },
        **extra,
    )
