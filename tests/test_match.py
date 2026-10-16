"""Descriptor matching: the only way a recorded step finds a live element. Offline."""

import pytest

from usecases import match as m
from usecases.recipe import Match


def action(**kw):
    base = {"id": "e1", "kind": "click", "role": "button", "label": "Go", "node": 1, "value": ""}
    return {**base, **kw}


def page(*actions, omitted=0):
    return {"actions": list(actions), "omitted_actions": omitted, "scroll": {"y": 0, "height": 800}}


def test_resolves_a_unique_candidate():
    p = page(action(label="Date grid"), action(label="Price graph", node=2))
    found = m.resolve(p, Match(kind="click", role="button", label="Date grid"))
    assert found["label"] == "Date grid" and found["node"] == 1


def test_kind_separates_the_fill_and_its_companion_open_click():
    # snapshot.js emits two actions for one editable node: the fill, and a click "Open <label>".
    p = page(
        action(kind="fill", role="combobox", label="Where from?", node=7),
        action(kind="click", role="combobox", label="Open Where from?", node=7),
    )
    assert m.resolve(p, Match(kind="fill", role="combobox", label="Where from?"))["kind"] == "fill"
    assert m.resolve(p, Match(kind="click", role="combobox", label="Open Where from?"))["kind"] == "click"


def test_duplicate_labels_are_ambiguous_not_a_guess():
    p = page(action(label="Select flight"), action(label="Select flight", node=2))
    with pytest.raises(m.Ambiguous) as error:
        m.resolve(p, Match(kind="click", role="button", label="Select flight"))
    assert "ambiguous:2" in str(error.value)


def test_recorded_ordinal_picks_among_a_recorded_count():
    p = page(*(action(label="Select flight", node=i) for i in range(1, 4)))
    found = m.resolve(p, Match(kind="click", role="button", label="Select flight",
                               ordinal=1, expected_candidates=3))
    assert found["node"] == 2


def test_a_changed_candidate_count_fails_instead_of_guessing():
    p = page(*(action(label="Select flight", node=i) for i in range(1, 3)))
    with pytest.raises(m.Ambiguous):
        m.resolve(p, Match(kind="click", role="button", label="Select flight",
                           ordinal=1, expected_candidates=3))


def test_no_candidate_raises_no_match():
    with pytest.raises(m.NoMatch):
        m.resolve(page(action(label="Price graph")), Match(kind="click", role="button", label="Date grid"))


@pytest.mark.parametrize(
    "live",
    ["Date grid", "Date  grid", " Date grid ", "Date​grid" if False else "Date grid​"],
)
def test_normalized_mode_absorbs_whitespace_and_invisible_characters(live):
    p = page(action(label=live))
    assert m.resolve(p, Match(kind="click", role="button", label="Date grid", mode="normalized"))


def test_normalized_mode_strips_a_trailing_state_suffix():
    # Google appends state to labels: "$745, Oct 3 to Oct 10, selected".
    p = page(action(label="$745, Oct 3 to Oct 10, selected"))
    assert m.resolve(p, Match(kind="click", role="button", label="$745, Oct 3 to Oct 10", mode="normalized"))


def test_exact_mode_does_not_absorb_those_differences():
    p = page(action(label="Date grid"))
    with pytest.raises(m.NoMatch):
        m.resolve(p, Match(kind="click", role="button", label="Date grid", mode="exact"))


def test_contains_mode():
    p = page(action(label="Nonstop flight with Iberia. Select flight"))
    assert m.resolve(p, Match(kind="click", role="button", label="Select flight", mode="contains"))


def test_select_options_are_addressed_by_their_composed_label():
    p = page(action(kind="select", role="combobox", label="Category → Design", value="design", node=4))
    found = m.resolve(p, Match(kind="select", role="combobox", label="Category → Design"))
    assert found["value"] == "design"  # Browser.act needs the option value, which only the live page has


def test_truncated_action_lists_are_unreliable_rather_than_missing():
    # snapshot.js caps at 250 actions; a miss inside a truncated list proves nothing.
    p = page(action(label="Price graph"), omitted=12)
    with pytest.raises(m.Unreliable):
        m.resolve(p, Match(kind="click", role="button", label="Date grid"))


def test_truncation_does_not_mask_a_successful_match():
    p = page(action(label="Date grid"), omitted=12)
    assert m.resolve(p, Match(kind="click", role="button", label="Date grid"))["label"] == "Date grid"


def test_role_is_optional_but_filters_when_present():
    p = page(action(role="tab", label="Dates"), action(role="button", label="Dates", node=2))
    assert m.resolve(p, Match(kind="click", role="tab", label="Dates"))["role"] == "tab"
