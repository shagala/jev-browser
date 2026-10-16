"""Replay a recipe with zero model calls.

Two rules shape this file:

* An element that is off-screen is not in `page["actions"]` at all (snapshot.js:57), so
  finding is a bounded scroll ladder, not a single read.
* `Browser.act` raises `StalePage` only from checks that run *before* any input is
  dispatched, while a `RuntimeError` means a native select may already have fired its
  change event. So StalePage is repairable and RuntimeError is not — re-running after a
  dispatched mutation would be a retry, which AGENTS.md forbids.
"""

import re
import time

from jev_ultrafast.browser import StalePage

from . import match as m

SCROLL_ATTEMPTS = 6
# A first step observes the moment the tab opens, with nothing before it to have waited.
FIND_ATTEMPTS, FIND_INTERVAL = 6, 0.5


def _scroll(page, direction):
    return next((a for a in page["actions"] if a["id"] == f"scroll_{direction}"), None)


def _scan(browser, page, descriptor, attempts):
    """One sweep of the viewport: match, scroll down, match, scroll back up, match."""
    for direction in ("down", "up"):
        current, used = page, 0
        while True:
            try:
                return m.resolve(current, descriptor), current
            except m.NoMatch:
                pass  # Only a miss is worth scrolling for; ambiguity means the page changed shape.
            scroll = _scroll(current, direction)
            if scroll is None or used >= attempts:
                break
            try:
                browser.act(scroll, current)
            except StalePage:
                # The page re-rendered mid-ladder. Scrolling mutates no site state, so
                # re-observe and spend the rung instead of failing the step.
                pass
            current = browser.observe(screenshot=False)
            used += 1
    return m.resolve(page, descriptor), page  # re-raises the original failure class


def find(browser, page, descriptor, *, attempts=SCROLL_ATTEMPTS, waits=None, interval=None):
    """Resolve a descriptor against the live page.

    An element can be missing for two reasons: it is outside the viewport, so snapshot.js
    never reported it, or it has not rendered yet. Scrolling answers the first and waiting
    answers the second, and only when both are spent is it really absent. A match on the
    first look costs neither.

    Returns (action, page) — the caller must hand `Browser.act` the same page the action
    came from, since `fresh()` reads `page["guards"][str(node)]`.
    """
    # Read the budget at call time so a test can collapse the waiting.
    waits = FIND_ATTEMPTS if waits is None else waits
    interval = FIND_INTERVAL if interval is None else interval
    for attempt in range(max(waits, 1)):
        if attempt:
            time.sleep(interval)
            page = browser.observe(screenshot=False)
        try:
            return _scan(browser, page, descriptor, attempts)
        except m.NoMatch as error:
            missing = error  # Ambiguity is a shape change; waiting will not resolve it.
    raise missing


def holds(page, assertion):
    """Does any observed action satisfy this assertion? Identity fields are not allowed here."""
    mode = assertion.get("mode", "normalized")
    for action in page["actions"]:
        if "kind" in assertion and action["kind"] != assertion["kind"]:
            continue
        if "role" in assertion and action.get("role") != assertion["role"]:
            continue
        if not m.labels_match(action.get("label", ""), assertion.get("label"), mode):
            continue
        if any(action.get(state) != assertion[state]
               for state in ("selected", "checked", "expanded", "value") if state in assertion):
            continue
        return True
    return False


def evaluate(page, expect, before=None):
    """Return None when the postcondition holds, else the reason it does not."""
    for assertion in expect.get("all_of", []):
        if not holds(page, assertion):
            return f"expected {assertion.get('label') or assertion.get('role')!r}"
    for assertion in expect.get("must_not", []):
        if holds(page, assertion):
            return f"unexpected {assertion.get('label') or assertion.get('role')!r} is present"
    counter = expect.get("count")
    if counter:
        pattern = re.compile(counter["label_matches"])
        seen = sum(1 for a in page["actions"] if pattern.match(a.get("label", "")))
        if seen < counter.get("min", 1) or seen > counter.get("max", 10**6):
            return f"count {seen} outside [{counter.get('min', 1)}, {counter.get('max', '∞')}]"
    if expect.get("require_page_changed") and before is not None:
        if page.get("fingerprint") == before.get("fingerprint"):
            return "require_page_changed: the page did not change"
    return None


def check_expect(browser, page, expect, before=None):
    """Poll the postcondition. A single observation races async rendering."""
    poll = expect.get("poll", {})
    attempts, interval = poll.get("attempts", 10), poll.get("interval_ms", 300) / 1000
    reason = evaluate(page, expect, before)
    for _ in range(max(attempts - 1, 0)):
        if reason is None:
            break
        time.sleep(interval)
        page = browser.observe(screenshot=False)
        reason = evaluate(page, expect, before)
    return reason is None, reason


def replay(browser, recipe, params, *, on_event=None):
    """Walk the recipe. No model is consulted; every value comes from `params`."""
    emit = on_event or (lambda *_args: None)
    page = browser.observe(screenshot=False)
    for step in recipe.steps:
        emit("step", step.id, step.intent)
        text = None
        if step.value_from:
            name = step.value_from["param"]
            if name not in params:
                return _failed(emit, step, "missing_param", f"no value for {name!r}", repairable=False)
        try:
            action, page = find(browser, page, step.match)
        except m.MatchError as error:
            return _failed(emit, step, error.kind, str(error), repairable=True)
        except StalePage as error:
            # Searching dispatched nothing but a scroll, so the step is safe to re-derive.
            return _failed(emit, step, "stale_pre", str(error), repairable=True)
        emit("matched", step.id, action["label"])
        if step.value_from:
            text = params[step.value_from["param"]]
        before = page
        try:
            browser.act(action, page, text=text)
        except StalePage as error:
            # Raised before any input was dispatched, so nothing was mutated.
            return _failed(emit, step, "stale_pre", str(error), repairable=True)
        except (RuntimeError, ValueError) as error:
            # The mutation may have landed; re-running the recipe would repeat it.
            return _failed(emit, step, "stale_post", str(error), repairable=False)
        emit("acted", step.id, action["label"])
        page = browser.observe(screenshot=False)
        ok, reason = check_expect(browser, page, step.expect, before=before)
        emit("expect", step.id, reason or "satisfied")
        if not ok:
            # The step already acted; only a re-record from a clean start is safe.
            return _failed(emit, step, "expect", reason, repairable=False)
        page = browser.observe(screenshot=False)
    emit("finished", None, "ok")
    return {"status": "ok", "step": None, "failure": None, "reason": None, "repairable": False}


def _failed(emit, step, failure, reason, *, repairable):
    emit("failed", step.id, f"{failure}: {reason}")
    return {"status": "failed", "step": step.id, "failure": failure,
            "reason": reason, "repairable": repairable}
