"""Recipe schema guards: regenerable, identity-free, atomic. Offline."""

import json

import pytest

from usecases.recipe import Match, Recipe, Step, from_json, load_all, save, to_json, validate

PROVENANCE = {"goal": "Open the date grid", "recorded_at": "2026-10-15T09:00:00Z", "model": "jev-1.14.0"}


def recipe(**kw):
    base = dict(
        name="demo",
        start={"url": "https://example.test/?hl=en"},
        steps=[Step(id="s1", intent="open the grid", op="CLICK",
                    match=Match(kind="click", role="button", label="Date grid"))],
        provenance=PROVENANCE,
    )
    return Recipe(**{**base, **kw})


def test_round_trips_through_json():
    original = recipe()
    assert from_json(json.loads(json.dumps(to_json(original)))) == original


def test_rejects_persisted_element_identity():
    # node/id/rect are per-document; persisting one would silently break on the next run.
    body = to_json(recipe())
    body["steps"][0]["match"]["node"] = 42
    with pytest.raises(TypeError):
        from_json(body)


def test_validate_rejects_identity_smuggled_through_a_free_dict():
    broken = recipe(steps=[Step(id="s1", intent="x", op="CLICK",
                                match=Match(kind="click", label="Go"),
                                expect={"all_of": [{"role": "tab", "node": 7}]})])
    with pytest.raises(ValueError, match="must not persist element identity"):
        validate(broken)


@pytest.mark.parametrize("missing", ["goal", "recorded_at", "model"])
def test_provenance_is_required_so_a_recipe_stays_regenerable(missing):
    with pytest.raises(ValueError, match=missing):
        validate(recipe(provenance={k: v for k, v in PROVENANCE.items() if k != missing}))


def test_scroll_and_wait_are_not_recordable():
    # Finding is a code-owned ladder; a recorded scroll count will not survive a layout change.
    with pytest.raises(ValueError, match="not recordable"):
        validate(recipe(steps=[Step(id="s1", intent="scroll", op="SCROLL_DOWN")]))


def test_a_typing_step_may_only_take_its_value_from_a_param():
    # Replay must make zero model calls.
    with pytest.raises(ValueError, match="only a param"):
        validate(recipe(steps=[Step(id="s1", intent="type", op="TYPE_TEXT",
                                    match=Match(kind="fill", label="Where from?"),
                                    value_from={"llm": True})]))
    validate(recipe(steps=[Step(id="s1", intent="type", op="TYPE_TEXT",
                                match=Match(kind="fill", label="Where from?"),
                                value_from={"param": "origin"})]))


def test_acting_steps_need_a_match():
    with pytest.raises(ValueError, match="needs a match"):
        validate(recipe(steps=[Step(id="s1", intent="click something", op="CLICK")]))


def test_duplicate_step_ids_are_rejected():
    step = Step(id="s1", intent="x", op="CLICK", match=Match(kind="click", label="Go"))
    with pytest.raises(ValueError, match="duplicate step id"):
        validate(recipe(steps=[step, step]))


def test_match_rejects_an_impossible_ordinal():
    with pytest.raises(ValueError, match="outside expected_candidates"):
        Match(kind="click", label="Go", ordinal=3, expected_candidates=2)


def test_bumped_keeps_the_previous_body_for_rollback():
    original = recipe()
    repaired = original.bumped(
        steps=[Step(id="s1", intent="open the grid", op="CLICK",
                    match=Match(kind="click", role="button", label="Date grid v2"))],
        provenance={**PROVENANCE, "recorded_at": "2026-10-16T09:00:00Z"},
        reason="no_match on s1",
    )
    assert repaired.version == 2
    assert repaired.steps[0].match.label == "Date grid v2"
    assert repaired.history[0]["version"] == 1
    assert repaired.history[0]["steps"][0]["match"]["label"] == "Date grid"
    assert repaired.history[0]["reason"] == "no_match on s1"
    assert original.version == 1 and original.steps[0].match.label == "Date grid"


def test_save_is_atomic_and_reloads(tmp_path):
    path = save(recipe(), root=tmp_path)
    assert path.exists() and not list(tmp_path.glob("*.tmp"))
    assert load_all(root=tmp_path)[0] == recipe()


def test_shipped_recipes_all_validate():
    # A hand-edited recipe is a bug; this is the guard that catches one.
    for shipped in load_all():
        validate(shipped)
