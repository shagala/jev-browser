"""Recording from the observation stream, not from the lossy history. Offline."""

import pytest

from usecases import recorder
from usecases.recipe import validate


def action(**kw):
    base = {"id": "e1", "kind": "click", "role": "button", "label": "Go", "node": 1, "value": ""}
    return {**base, **kw}


def page(*actions):
    return {"actions": list(actions), "omitted_actions": 0, "scroll": {"y": 0, "height": 800},
            "fingerprint": "f", "url": "https://example.test/", "title": "Example"}


def entry(choice, label, *, kind="click", operation="CLICK", changed=True, text=None):
    return {"choice": choice, "action": label, "kind": kind, "operation": operation,
            "page_changed": changed, "text": text}


class FakeAgent:
    """Yields snapshots like Agent.run(): state["page"] is the page AFTER the action."""

    def __init__(self, url, goal, *, pages, entries, status="done"):
        self.url, self.goal = url, goal
        self.pages, self.entries, self.status = pages, entries, status
        self.history = []

    def run(self):
        for post, item in zip(self.pages[1:], self.entries):
            if item is not None:
                self.history.append(item)
            self._page = post
            yield self.snapshot()

    def snapshot(self):
        return {"page": getattr(self, "_page", self.pages[0]), "history": list(self.history),
                "status": self.status, "decisions": [{"model": "jev-1.14.0"}]}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def factory(pages, entries, status="done"):
    return lambda url, goal: FakeAgent(url, goal, pages=pages, entries=entries, status=status)


def test_records_the_descriptor_from_the_page_the_action_was_chosen_against():
    # The id "e2" means nothing on the next page; it must be resolved against the pre-action one.
    first = page(action(id="e1", label="Price graph"), action(id="e2", label="Date grid"))
    after = page(action(id="e1", kind="click", role="tab", label="Dates", selected="true"))
    recipe = recorder.record("demo", "Open the date grid", "https://example.test/",
                             agent_factory=factory([first, after], [entry("e2", "Date grid")]))
    assert len(recipe.steps) == 1
    assert recipe.steps[0].match.label == "Date grid"
    assert recipe.steps[0].match.role == "button" and recipe.steps[0].match.kind == "click"
    validate(recipe)


def test_scroll_and_wait_are_not_recorded():
    # They are a code-owned ladder at replay time, not steps.
    first = page(action(id="e1", label="Date grid"),
                 {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
                 {"id": "wait", "kind": "wait", "label": "Wait"})
    recipe = recorder.record(
        "demo", "goal", "https://example.test/",
        agent_factory=factory([first, first, first, first],
                              [entry("scroll_down", "Scroll down", kind="scroll"),
                               entry("wait", "Wait", kind="wait"),
                               entry("e1", "Date grid")]))
    assert [s.intent for s in recipe.steps] == ["Date grid"]
    assert recipe.steps[0].id == "s1"
    validate(recipe)


def test_counts_competing_candidates_so_replay_can_detect_a_change():
    first = page(action(id="e1", label="Select flight"), action(id="e2", label="Select flight", node=2))
    recipe = recorder.record("demo", "goal", "https://example.test/",
                             agent_factory=factory([first, first], [entry("e2", "Select flight")]))
    assert recipe.steps[0].match.expected_candidates == 2
    assert recipe.steps[0].match.ordinal == 1


def test_a_typing_step_records_where_its_value_comes_from_never_the_value():
    first = page(action(id="e1", kind="fill", role="combobox", label="Where from?"))
    recipe = recorder.record(
        "demo", "goal", "https://example.test/",
        agent_factory=factory([first, first],
                              [entry("e1", "Where from?", kind="fill", operation="TYPE_TEXT", text="Miami")]))
    assert recipe.steps[0].value_from == {"param": "where_from"}
    assert "Miami" not in str(recipe)  # the typed value is never persisted
    validate(recipe)


def test_each_step_expects_the_next_step_to_be_findable():
    first = page(action(id="e1", label="Date grid"))
    middle = page(action(id="e1", kind="click", role="tab", label="Dates"))
    recipe = recorder.record(
        "demo", "goal", "https://example.test/",
        agent_factory=factory([first, middle, middle],
                              [entry("e1", "Date grid"), entry("e1", "Dates")]))
    assert recipe.steps[0].expect["all_of"] == [{"kind": "click", "label": "Dates",
                                                 "mode": "normalized", "role": "tab"}]
    assert "all_of" not in recipe.steps[1].expect  # nothing follows the last step


def test_page_changed_becomes_a_transition_requirement():
    first = page(action(id="e1", label="Date grid"))
    recipe = recorder.record("demo", "goal", "https://example.test/",
                             agent_factory=factory([first, first], [entry("e1", "Date grid", changed=True)]))
    assert recipe.steps[0].expect["require_page_changed"] is True


def test_a_blocked_run_records_nothing():
    first = page(action(id="e1", label="Date grid"))
    with pytest.raises(ValueError, match="blocked"):
        recorder.record("demo", "goal", "https://example.test/",
                        agent_factory=factory([first, first], [entry("e1", "Date grid")], status="blocked"))


def test_a_run_with_no_recordable_action_is_an_error():
    first = page({"id": "wait", "kind": "wait", "label": "Wait"})
    with pytest.raises(ValueError, match="without executing"):
        recorder.record("demo", "goal", "https://example.test/",
                        agent_factory=factory([first, first], [entry("wait", "Wait", kind="wait")]))


def test_provenance_carries_the_goal_and_source_hashes():
    first = page(action(id="e1", label="Date grid"))
    goal = "Open the round-trip date grid"
    recipe = recorder.record("demo", goal, "https://example.test/",
                             agent_factory=factory([first, first], [entry("e1", "Date grid")]))
    assert recipe.provenance["goal"] == goal
    assert recipe.provenance["model"] == "jev-1.14.0"
    assert "snapshot.js" in recipe.provenance["source_hashes"]


def test_the_url_template_is_stored_while_a_filled_url_is_visited():
    # One recipe must serve every route; the recording visits one of them.
    first = page(action(id="e1", label="Date grid"))
    visited = []

    def factory(url, goal):
        visited.append(url)
        return FakeAgent(url, goal, pages=[first, first], entries=[entry("e1", "Date grid")])

    template = "https://example.test/?from={origin}"
    recipe = recorder.record("demo", "goal", template, params={"origin": "MIA"}, agent_factory=factory)
    assert visited == ["https://example.test/?from=MIA"]
    assert recipe.start["url"] == template
