"""Recipe schema: a trace cache of what Jev did, never hand-authored.

A recipe stores descriptors, never element ids. `node` and `id` are per-document and
per-snapshot (snapshot.js:3-7, :101), so a step must re-resolve its target against a live
observation every time. Storing one would be a bug; `validate` enforces that.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

RECIPES = Path(__file__).with_name("recipes")
MODES = {"exact", "normalized", "contains"}
# Identity must never be persisted: it is meaningless in the next document.
FORBIDDEN = {"node", "rect", "fingerprint", "marker", "guards", "page_key"}
# Inside a descriptor, "id" would be an action id ("e1"), which is reassigned every snapshot.
# A step's own "id" is its name ("s1"), so the stricter set applies only to descriptor subtrees.
FORBIDDEN_IN_DESCRIPTOR = FORBIDDEN | {"id"}


@dataclass(frozen=True)
class Match:
    """How a step finds its element. A fixed schema read by code, never a model string."""

    kind: str
    role: str | None = None
    label: str | None = None
    mode: str = "normalized"
    ordinal: int = 0
    expected_candidates: int = 1

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {sorted(MODES)}, got {self.mode!r}")
        if self.ordinal < 0 or self.expected_candidates < 1:
            raise ValueError("ordinal must be >= 0 and expected_candidates >= 1")
        if self.ordinal >= self.expected_candidates:
            raise ValueError(f"ordinal {self.ordinal} outside expected_candidates {self.expected_candidates}")


@dataclass(frozen=True)
class Step:
    id: str
    intent: str
    op: str
    match: Match | None = None
    value_from: dict | None = None
    mutating: bool = False
    when: list[dict] = field(default_factory=list)
    expect: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Recipe:
    name: str
    start: dict
    steps: list[Step]
    provenance: dict
    version: int = 1
    schedule: str | None = None
    params_from: str | None = None
    extract: dict | None = None
    store: dict | None = None
    history: list[dict] = field(default_factory=list)

    def bumped(self, steps, provenance, reason):
        """A repaired recipe is a new version; the old body is kept for diffing and rollback."""
        previous = {
            "version": self.version,
            "replaced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "steps": [asdict(s) for s in self.steps],
            "provenance": self.provenance,
        }
        return replace(
            self,
            version=self.version + 1,
            steps=list(steps),
            provenance=provenance,
            history=[*self.history, previous],
        )


def _leaks(value, forbidden, path="recipe"):
    """Every persisted key, recursively, checked against a forbidden set."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden:
                yield f"{path}.{key}"
            yield from _leaks(item, forbidden, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaks(item, forbidden, f"{path}[{index}]")


def validate(recipe):
    """Reject anything that could not have come from a recording, or that will not replay."""
    if not recipe.steps:
        raise ValueError(f"{recipe.name}: a recipe needs at least one step")
    missing = {"goal", "recorded_at", "model"} - set(recipe.provenance)
    if missing:
        # Regenerability is the invariant: without the goal a recipe cannot be re-derived.
        raise ValueError(f"{recipe.name}: provenance missing {', '.join(sorted(missing))}")
    if "url" not in recipe.start:
        raise ValueError(f"{recipe.name}: start.url is required")
    seen = set()
    for step in recipe.steps:
        if step.id in seen:
            raise ValueError(f"{recipe.name}: duplicate step id {step.id!r}")
        seen.add(step.id)
        if step.op in {"CLICK", "TYPE_TEXT", "SELECT"} and step.match is None:
            raise ValueError(f"{recipe.name}/{step.id}: {step.op} needs a match")
        if step.op in {"SCROLL_DOWN", "SCROLL_UP", "WAIT"}:
            # Finding is a code-owned scroll/poll ladder; a recorded scroll count will not
            # survive a layout change, so recordings must not contain one.
            raise ValueError(f"{recipe.name}/{step.id}: {step.op} steps are not recordable")
        if step.value_from and set(step.value_from) - {"param"}:
            # Replay must make zero model calls, so a value can only come from a named param.
            raise ValueError(f"{recipe.name}/{step.id}: value_from accepts only a param in replay")
    leaked = list(_leaks(to_json(recipe), FORBIDDEN))
    for step in recipe.steps:
        descriptors = {"match": asdict(step.match) if step.match else None,
                       "when": step.when, "expect": step.expect}
        leaked += _leaks(descriptors, FORBIDDEN_IN_DESCRIPTOR, f"recipe.{step.id}")
    if leaked:
        raise ValueError(f"{recipe.name}: must not persist element identity: {', '.join(leaked)}")
    return recipe


def to_json(recipe):
    body = asdict(recipe)
    body["steps"] = [{k: v for k, v in step.items() if v not in (None, [], {})} for step in body["steps"]]
    return {k: v for k, v in body.items() if v not in (None, [], {})}


def from_json(body):
    steps = []
    for step in body["steps"]:
        descriptor = step.get("match")
        steps.append(Step(**{**step, "match": Match(**descriptor) if descriptor else None}))
    return Recipe(**{**body, "steps": steps})


def load(name, root=RECIPES):
    return validate(from_json(json.loads((root / f"{name}.json").read_text())))


def load_all(root=RECIPES):
    return [load(path.stem, root) for path in sorted(root.glob("*.json"))]


def save(recipe, root=RECIPES):
    """Atomic: a torn write would leave a half-recipe driving a browser tomorrow."""
    validate(recipe)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{recipe.name}.json"
    handle, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
    with os.fdopen(handle, "w") as file:
        json.dump(to_json(recipe), file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(temporary, path)
    return path
