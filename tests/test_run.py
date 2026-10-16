"""The catalog runner: scheduling, repair adoption, and the no-paid-calls guarantee."""

from datetime import datetime, timedelta

import pytest

from jev_ultrafast import model
from usecases import recipe as recipes
from usecases import run
from usecases.recipe import Match, Recipe, Step

PROVENANCE = {"goal": "Open the date grid", "recorded_at": "2026-10-15T09:00:00Z", "model": "jev-1.14.0"}


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    from usecases import player
    monkeypatch.setattr(player, "FIND_INTERVAL", 0)


@pytest.fixture(autouse=True)
def no_paid_calls(monkeypatch):
    """Mechanical proof that replay never reaches a model provider."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("replay must not call a model provider")

    monkeypatch.setattr(model, "post_json", forbidden)


def action(**kw):
    base = {"id": "e1", "kind": "click", "role": "button", "label": "Date grid", "node": 1, "value": ""}
    return {**base, **kw}


def page(*actions, fingerprint="f0"):
    return {"actions": list(actions) or [action()], "omitted_actions": 0,
            "scroll": {"y": 0, "height": 700}, "fingerprint": fingerprint,
            "url": "https://example.test/", "title": "Example"}


def recipe(name="demo", **kw):
    base = dict(
        start={"url": "https://example.test/?q={origin}"},
        steps=[Step(id="s1", intent="open the grid", op="CLICK",
                    match=Match(kind="click", role="button", label="Date grid"),
                    expect={"poll": {"attempts": 2, "interval_ms": 0}})],
        provenance=PROVENANCE,
    )
    return Recipe(name=name, **{**base, **kw})


class FakeBrowser:
    def __init__(self, url, pages=None, fail=False):
        self.url, self.closed, self.acted = url, False, []
        self.pages = pages or [page()]
        self.fail = fail

    def observe(self, screenshot=False):
        return self.pages[0]

    def act(self, action, page, text=None):
        self.acted.append(action["label"])

    def fresh(self, page, action=None):
        return True

    def close(self):
        self.closed = True


# --- scheduling ----------------------------------------------------------------------


def test_manual_recipes_are_never_due():
    assert not run.is_due(recipe(schedule=None), [])
    assert not run.is_due(recipe(schedule="manual"), [])


def test_a_daily_recipe_fires_once_after_its_hour():
    item = recipe(schedule="daily@07:00")
    morning = datetime.fromisoformat("2026-10-16T07:30:00+00:00")
    assert run.is_due(item, [], now=morning)
    ran = [{"recipe": "demo", "at": "2026-10-16T07:31:00+00:00", "status": "ok"}]
    assert not run.is_due(item, ran, now=datetime.fromisoformat("2026-10-16T08:31:00+00:00"))
    assert run.is_due(item, ran, now=datetime.fromisoformat("2026-10-17T07:01:00+00:00"))


def test_a_daily_recipe_does_not_fire_before_its_hour():
    assert not run.is_due(recipe(schedule="daily@07:00"), [],
                          now=datetime.fromisoformat("2026-10-16T06:59:00+00:00"))


def test_a_failed_run_leaves_the_recipe_still_due():
    failed = [{"recipe": "demo", "at": "2026-10-16T07:05:00+00:00", "status": "failed"}]
    assert run.is_due(recipe(schedule="daily@07:00"), failed,
                      now=datetime.fromisoformat("2026-10-16T07:30:00+00:00"))


def test_hourly():
    item = recipe(schedule="hourly")
    now = datetime.fromisoformat("2026-10-16T09:00:00+00:00")
    assert run.is_due(item, [{"recipe": "demo", "at": (now - timedelta(hours=2)).isoformat(),
                              "status": "ok"}], now=now)
    assert not run.is_due(item, [{"recipe": "demo", "at": (now - timedelta(minutes=5)).isoformat(),
                                  "status": "ok"}], now=now)


def test_an_unknown_schedule_is_an_error():
    with pytest.raises(ValueError, match="unknown schedule"):
        run.is_due(recipe(schedule="every full moon"), [])


# --- running -------------------------------------------------------------------------


def test_a_clean_replay_makes_no_model_call_and_closes_the_tab():
    browsers = []

    def factory(url):
        browsers.append(FakeBrowser(url))
        return browsers[-1]

    result = run.run_recipe(recipe(), {"origin": "MIA"}, browser_factory=factory)
    assert result["status"] == "ok"
    assert browsers[0].url == "https://example.test/?q=MIA"  # params fill the start url
    assert browsers[0].closed and browsers[0].acted == ["Date grid"]


def test_a_repairable_failure_re_records_and_adopts_a_new_version(tmp_path):
    missing = FakeBrowser("u", pages=[page(action(label="Price graph"))])
    recorded = recipe(steps=[Step(id="s1", intent="open the grid", op="CLICK",
                                  match=Match(kind="click", role="button", label="Date grid v2"))])

    def fake_recorder(name, goal, url, agent_factory=None, on_event=None, **kw):
        assert goal == PROVENANCE["goal"]  # the NL goal stays the source of truth
        return recorded

    import usecases.run as module
    original, module.recorder.record = module.recorder.record, fake_recorder
    try:
        result = run.run_recipe(recipe(), {"origin": "MIA"},
                                browser_factory=lambda url: missing, root=tmp_path)
    finally:
        module.recorder.record = original
    assert result["status"] == "repaired" and result["recipe"].version == 2
    reloaded = recipes.load("demo", root=tmp_path)
    assert reloaded.steps[0].match.label == "Date grid v2"
    assert reloaded.history[0]["steps"][0]["match"]["label"] == "Date grid"  # rollback kept


def test_no_repair_reports_the_break_without_touching_the_recipe(tmp_path):
    missing = FakeBrowser("u", pages=[page(action(label="Price graph"))])
    result = run.run_recipe(recipe(), {"origin": "MIA"}, repair=False,
                            browser_factory=lambda url: missing, root=tmp_path)
    assert result["status"] == "failed" and result["failure"] == "no_match"
    assert not list(tmp_path.glob("*.json"))


def test_an_unsafe_failure_is_never_repaired(tmp_path):
    # The step already acted; re-running the recipe would repeat the mutation.
    acted = FakeBrowser("u", pages=[page(action(label="Date grid"))])
    strict = recipe(steps=[Step(id="s1", intent="open the grid", op="CLICK",
                                match=Match(kind="click", role="button", label="Date grid"),
                                expect={"all_of": [{"role": "tab", "label": "Dates"}],
                                        "poll": {"attempts": 1, "interval_ms": 0}})])
    result = run.run_recipe(strict, {"origin": "MIA"},
                            browser_factory=lambda url: acted, root=tmp_path)
    assert result["status"] == "failed" and result["failure"] == "expect"
    assert not list(tmp_path.glob("*.json"))


# --- fanning out and the terminal verifier -------------------------------------------

SETS = [{"origin": "MIA", "n": 1}, {"origin": "MAD", "n": 2}]


@pytest.fixture
def wired(monkeypatch):
    """Stand in for the catalog's handlers; handlers.resolve has its own contract tests."""
    stored = []

    def store(records, _params):
        stored.append(list(records))
        return {"rows": len(records)}

    table = {"sets": lambda: SETS,
             "good": lambda _browser, _params: (["a", "b"], None),
             "bad": lambda _browser, _params: ([], "Dates tab not selected"),
             "store": store}
    monkeypatch.setattr(run.handlers, "resolve", lambda reference: table[reference])
    return stored


def test_without_a_provider_a_recipe_runs_once_from_the_overrides():
    assert run.param_sets(recipe(), {"origin": "MIA"}) == [{"origin": "MIA"}]


def test_a_provider_fans_the_recipe_out_over_its_work(wired):
    assert run.param_sets(recipe(params_from="sets"), {}) == SETS


def test_overrides_filter_the_provider_instead_of_replacing_it(wired):
    assert run.param_sets(recipe(params_from="sets"), {"origin": "MAD"}) == [SETS[1]]


def test_a_filter_matching_nothing_is_an_error(wired):
    with pytest.raises(ValueError, match="no parameter set matches"):
        run.param_sets(recipe(params_from="sets"), {"origin": "LHR"})


def test_the_verifier_runs_after_replay_and_gates_the_store(wired):
    item = recipe(extract={"handler": "good"}, store={"handler": "store"})
    result = run.run_recipe(item, {"origin": "MIA"}, browser_factory=FakeBrowser)
    assert result["status"] == "ok" and result["detail"] == {"rows": 2}
    assert wired == [["a", "b"]]


def test_a_failed_verifier_stores_nothing_and_is_never_repaired(wired, tmp_path):
    # Replay reporting ok is not evidence; the code-owned verifier is the authority.
    item = recipe(extract={"handler": "bad"}, store={"handler": "store"})
    result = run.run_recipe(item, {"origin": "MIA"}, browser_factory=FakeBrowser, root=tmp_path)
    assert result["status"] == "failed" and result["failure"] == "verify"
    assert result["repairable"] is False and wired == []
    assert not list(tmp_path.glob("*.json"))  # a verify failure never rewrites the recipe


def test_a_broken_step_never_reaches_the_verifier(wired):
    item = recipe(extract={"handler": "bad"}, store={"handler": "store"})
    missing = FakeBrowser("u", pages=[page(action(label="Price graph"))])
    result = run.run_recipe(item, {"origin": "MIA"}, repair=False, browser_factory=lambda url: missing)
    assert result["failure"] == "no_match" and wired == []


def test_the_ledger_round_trips(tmp_path):
    path = tmp_path / "runs.jsonl"
    run.record_run({"recipe": "demo", "at": "2026-10-16T07:00:00+00:00", "status": "ok"}, path=path)
    run.record_run({"recipe": "demo", "at": "2026-10-16T08:00:00+00:00", "status": "failed"}, path=path)
    entries = run.ledger_entries(path)
    assert len(entries) == 2
    assert run.last_success("demo", entries).hour == 7
