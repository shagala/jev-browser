"""Replay: the scroll ladder, expect polling, and the pre/post mutation boundary. Offline."""

import pytest

from jev_ultrafast.browser import StalePage
from usecases import match as m
from usecases import player
from usecases.recipe import Match, Recipe, Step

PROVENANCE = {"goal": "Open the date grid", "recorded_at": "2026-10-15T09:00:00Z", "model": "jev-1.14.0"}


def action(**kw):
    base = {"id": "e1", "kind": "click", "role": "button", "label": "Go", "node": 1, "value": ""}
    return {**base, **kw}


def page(*actions, y=0, height=800, omitted=0, fingerprint="f0"):
    scrolls = []
    if y + 780 < height - 2:
        scrolls.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560})
    if y > 0:
        scrolls.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "delta": -560})
    return {
        "actions": [*actions, *scrolls],
        "omitted_actions": omitted,
        "scroll": {"y": y, "height": height},
        "fingerprint": fingerprint,
        "url": "https://example.test/",
        "title": "Example",
    }


class FakeBrowser:
    """Scripted observations; records every act. fresh() is True unless scripted otherwise."""

    def __init__(self, pages, act_effects=None):
        self.pages, self.acted, self.index = list(pages), [], 0
        self.act_effects = list(act_effects or [])

    def observe(self, screenshot=False):
        current = self.pages[min(self.index, len(self.pages) - 1)]
        self.index += 1
        return current

    def act(self, action, page, text=None):
        self.acted.append({"label": action["label"], "kind": action["kind"], "text": text})
        if self.act_effects:
            effect = self.act_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect

    def fresh(self, page, action=None):
        return True


def recipe(*steps, **kw):
    return Recipe(name="demo", start={"url": "https://example.test/"}, steps=list(steps),
                  provenance=PROVENANCE, **kw)


def step(**kw):
    base = dict(id="s1", intent="open the grid", op="CLICK",
                match=Match(kind="click", role="button", label="Date grid"),
                expect={"all_of": [{"role": "tab", "label": "Dates", "selected": "true"}],
                        "poll": {"attempts": 3, "interval_ms": 0}})
    return Step(**{**base, **kw})


# --- the scroll ladder ------------------------------------------------------------------


def test_finds_an_element_that_is_below_the_fold():
    # snapshot.js only reports elements inside the viewport, so the first observation misses it.
    above = page(action(label="Price graph"), y=0, height=2000)
    below = page(action(label="Date grid"), y=560, height=2000)
    browser = FakeBrowser([below])  # observe() only runs after a scroll
    found, found_page = player.find(browser, above, Match(kind="click", role="button", label="Date grid"))
    assert found["label"] == "Date grid"
    assert found_page is below  # act() must receive the page the action was found in
    assert [a["kind"] for a in browser.acted] == ["scroll"]


def test_gives_up_after_the_scroll_budget_and_reports_no_match():
    nothing = page(action(label="Price graph"), y=0, height=9000)
    browser = FakeBrowser([nothing] * 20)
    with pytest.raises(m.NoMatch):
        player.find(browser, nothing, Match(kind="click", role="button", label="Date grid"), attempts=3)
    assert len(browser.acted) == 3


def test_does_not_scroll_when_the_page_cannot_scroll():
    short = page(action(label="Price graph"), y=0, height=700)
    browser = FakeBrowser([short])
    with pytest.raises(m.NoMatch):
        player.find(browser, short, Match(kind="click", role="button", label="Date grid"))
    assert browser.acted == []


def test_ambiguity_is_not_retried_by_scrolling():
    # Two candidates means the page changed shape; scrolling cannot fix that.
    both = page(action(label="Date grid"), action(label="Date grid", node=2), y=0, height=9000)
    browser = FakeBrowser([both] * 5)
    with pytest.raises(m.Ambiguous):
        player.find(browser, both, Match(kind="click", role="button", label="Date grid"))
    assert browser.acted == []


# --- expect ----------------------------------------------------------------------------


def test_expect_polls_instead_of_reading_once():
    # The grid renders asynchronously; a single observation races it.
    empty = page(action(label="Date grid"))
    ready = page(action(label="Date grid"), action(kind="click", role="tab", label="Dates", selected="true"))
    browser = FakeBrowser([empty, ready])  # the first evaluation uses the page passed in
    ok, _ = player.check_expect(browser, empty, step().expect, before=page(fingerprint="old"))
    assert ok


def test_expect_fails_when_the_condition_never_arrives():
    empty = page(action(label="Date grid"))
    browser = FakeBrowser([empty] * 5)
    ok, reason = player.check_expect(browser, empty, step().expect, before=page(fingerprint="old"))
    assert not ok and "Dates" in reason


def test_must_not_catches_the_right_shape_on_the_wrong_surface():
    # The calendar picker also renders per-day prices; positives alone would accept it.
    picker = page(
        action(kind="click", role="tab", label="Dates", selected="true"),
        action(label="$745, Oct 3 to Oct 10"),
        action(label="Reset"),
    )
    expect = {**step().expect, "must_not": [{"role": "button", "label": "Reset"}]}
    ok, reason = player.check_expect(FakeBrowser([picker]), picker, expect, before=page(fingerprint="old"))
    assert not ok and "Reset" in reason


def test_count_asserts_cardinality_not_existence():
    # "a price cell exists" passed on the picker too; "at least ten" does not.
    thin = page(action(kind="click", role="tab", label="Dates", selected="true"),
                action(label="$745, Oct 3 to Oct 10"))
    expect = {**step().expect, "count": {"label_matches": r"^\$[\d,]+, .* to .*$", "min": 10}}
    ok, reason = player.check_expect(FakeBrowser([thin]), thin, expect, before=page(fingerprint="old"))
    assert not ok and "count" in reason


def test_require_page_changed_rejects_a_no_op():
    same = page(action(kind="click", role="tab", label="Dates", selected="true"), fingerprint="same")
    expect = {**step().expect, "require_page_changed": True}
    ok, reason = player.check_expect(FakeBrowser([same]), same, expect, before=same)
    assert not ok and "page_changed" in reason


# --- the mutation boundary --------------------------------------------------------------


def test_a_stale_page_before_dispatch_is_repairable():
    # Browser.act raises StalePage only from its freshness check, before any input is sent.
    ready = page(action(label="Date grid"), action(kind="click", role="tab", label="Dates", selected="true"))
    browser = FakeBrowser([ready] * 5, act_effects=[StalePage("Page changed since this decision")])
    result = player.replay(browser, recipe(step()), params={})
    assert result["status"] == "failed" and result["repairable"] is True
    assert result["failure"] == "stale_pre"


def test_an_unconfirmed_mutation_is_never_repairable():
    # RuntimeError from a select means the change event may already have fired.
    ready = page(action(label="Date grid"), action(kind="click", role="tab", label="Dates", selected="true"))
    browser = FakeBrowser([ready] * 5,
                          act_effects=[RuntimeError("Dropdown execution was not confirmed")])
    result = player.replay(browser, recipe(step()), params={})
    assert result["status"] == "failed" and result["repairable"] is False


def test_a_failed_expect_after_acting_is_not_repairable():
    # The click already landed; re-running the recipe would re-click it.
    empty = page(action(label="Date grid"))
    browser = FakeBrowser([empty] * 8)
    result = player.replay(browser, recipe(step()), params={})
    assert result["status"] == "failed" and result["repairable"] is False
    assert result["failure"] == "expect"


def test_a_missing_element_before_acting_is_repairable():
    missing = page(action(label="Price graph"), y=0, height=700)
    result = player.replay(FakeBrowser([missing] * 4), recipe(step()), params={})
    assert result["status"] == "failed" and result["repairable"] is True
    assert result["failure"] == "no_match" and result["step"] == "s1"


# --- happy path and typing ----------------------------------------------------------------


def test_a_clean_replay_acts_once_per_step_and_emits_a_trace():
    ready = page(action(label="Date grid"), action(kind="click", role="tab", label="Dates", selected="true"))
    browser = FakeBrowser([ready] * 6)
    events = []
    result = player.replay(browser, recipe(step()), params={}, on_event=lambda *a: events.append(a))
    assert result["status"] == "ok"
    assert [a["label"] for a in browser.acted] == ["Date grid"]
    assert [stage for stage, _, _ in events] == ["step", "matched", "acted", "expect", "finished"]


def test_typing_takes_its_value_from_a_param_never_a_model():
    field = page(action(kind="fill", role="combobox", label="Where from?"),
                 action(kind="click", role="tab", label="Dates", selected="true"))
    browser = FakeBrowser([field] * 6)
    typing = step(op="TYPE_TEXT", match=Match(kind="fill", role="combobox", label="Where from?"),
                  value_from={"param": "origin"})
    result = player.replay(browser, recipe(typing), params={"origin": "Miami"})
    assert result["status"] == "ok" and browser.acted[0]["text"] == "Miami"


def test_a_missing_param_fails_before_touching_the_browser():
    field = page(action(kind="fill", role="combobox", label="Where from?"))
    browser = FakeBrowser([field] * 4)
    typing = step(op="TYPE_TEXT", match=Match(kind="fill", role="combobox", label="Where from?"),
                  value_from={"param": "origin"})
    result = player.replay(browser, recipe(typing), params={})
    assert result["status"] == "failed" and browser.acted == []
