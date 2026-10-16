"""Replay against a real DOM: the scroll ladder, collisions, staleness and async render.

The pytest tiers script `Browser.observe`, so they cannot catch a wrong assumption about
what snapshot.js actually reports. These run the real browser against `data:` pages.
No model calls and no external websites.
"""

from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage
from usecases import match as m
from usecases import player
from usecases.recipe import Match, Recipe, Step

PROVENANCE = {"goal": "offline check", "recorded_at": "2026-10-16T00:00:00Z", "model": "none"}

BELOW_THE_FOLD = """<!doctype html><title>Below the fold</title>
<style>body{margin:0;font:16px system-ui}
#filler{height:1800px;background:linear-gradient(#eee,#fff)}button{height:44px;padding:0 18px}</style>
<button id="top">Price graph</button>
<div id="filler">Results</div>
<button id="grid">Date grid</button>
<div style="height:900px"></div>"""

COLLISION = """<!doctype html><title>Collision</title>
<button>Select flight</button><button>Select flight</button>"""

ASYNC_RENDER = """<!doctype html><title>Async render</title>
<button id="open" onclick="setTimeout(()=>{
  document.querySelector('#panel').innerHTML='<div role=tab aria-selected=true tabindex=0>Dates</div>'
},400)">Date grid</button><div id="panel"></div>"""


def step(**kw):
    base = dict(id="s1", intent="open the grid", op="CLICK",
                match=Match(kind="click", role="button", label="Date grid"),
                expect={"poll": {"attempts": 10, "interval_ms": 150}})
    return Step(**{**base, **kw})


def recipe(*steps):
    return Recipe(name="check", start={"url": "about:blank"}, steps=list(steps), provenance=PROVENANCE)


def page_for(html):
    return Browser("data:text/html," + quote(html))


def check_below_the_fold(passed):
    """snapshot.js drops anything whose centre is outside the viewport, so the first read misses it."""
    browser = page_for(BELOW_THE_FOLD)
    try:
        first = browser.observe(screenshot=False)
        assert not any(a.get("label") == "Date grid" for a in first["actions"]), \
            "fixture is wrong: the button must start below the fold"
        action, found_on = player.find(browser, first, Match(kind="click", role="button", label="Date grid"))
        assert action["label"] == "Date grid"
        browser.act(action, found_on)  # the page the action came from carries its guards
        passed.append("below-the-fold target found by the scroll ladder and clicked")
    finally:
        browser.close()


def check_collision(passed):
    """Two equal candidates is a structural change; replay must refuse, never pick the first."""
    browser = page_for(COLLISION)
    try:
        page = browser.observe(screenshot=False)
        try:
            player.find(browser, page, Match(kind="click", role="button", label="Select flight"))
        except m.Ambiguous:
            passed.append("duplicate labels rejected instead of guessed")
        else:
            raise AssertionError("ambiguous label resolved to a single action")
    finally:
        browser.close()


def check_stale_is_repairable(passed):
    """A target replaced between observe and act raises before dispatch, so it is safe to re-derive."""
    browser = page_for(ASYNC_RENDER)
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Date grid")
        browser.evaluate("document.querySelector('#open').outerHTML="
                         "document.querySelector('#open').outerHTML")
        try:
            browser.act(action, page)
        except StalePage:
            passed.append("a replaced node raises StalePage before any input is dispatched")
        else:
            raise AssertionError("a replaced node was clicked")
    finally:
        browser.close()


def check_async_expect(passed):
    """The panel renders 400 ms after the click; a single post-action read would miss it."""
    browser = page_for(ASYNC_RENDER)
    try:
        waiting = step(expect={"all_of": [{"kind": "click", "role": "tab", "label": "Dates"}],
                               "poll": {"attempts": 10, "interval_ms": 150}})
        result = player.replay(browser, recipe(waiting), params={})
        assert result["status"] == "ok", result
        passed.append("expect polled until the asynchronous panel rendered")

        browser.evaluate("document.querySelector('#panel').innerHTML=''")
        impatient = step(expect={"all_of": [{"kind": "click", "role": "tab", "label": "Dates"}],
                                 "poll": {"attempts": 1, "interval_ms": 0}})
        result = player.replay(browser, recipe(impatient), params={})
        assert result["status"] == "failed" and result["failure"] == "expect", result
        assert result["repairable"] is False, "the click landed; re-running would repeat it"
        passed.append("a single read races the render, and that failure is never auto-repaired")
    finally:
        browser.close()


def main():
    passed = []
    for check in (check_below_the_fold, check_collision, check_stale_is_repairable, check_async_expect):
        check(passed)
    print("\n".join(passed))
    print(f"PASS: {len(passed)} replay checks against a real DOM; no model calls")


if __name__ == "__main__":
    main()
