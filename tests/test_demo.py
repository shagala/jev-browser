"""Inspector endpoints: start-URL validation, catalog guards, and the replay trace.

Offline. No browser is opened and no model is called: the catalog is substituted.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jev_ultrafast import demo

# --- start URL validation --------------------------------------------------------------


@pytest.fixture
def resolves(monkeypatch):
    """Resolve every name to one address, so validation never touches DNS."""
    def set_to(address):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda *_a, **_k: [(socket.AF_INET, None, None, "", (address, 0))])
    return set_to


def test_a_public_https_url_is_accepted(resolves):
    resolves("93.184.216.34")
    assert demo.safe_start_url("https://example.com/path") == "https://example.com/path"


@pytest.mark.parametrize("raw", ["file:///etc/passwd", "javascript:alert(1)", "about:blank", "", "   "])
def test_only_http_schemes_are_accepted(raw):
    with pytest.raises(ValueError, match="http or https"):
        demo.safe_start_url(raw)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"])
def test_private_and_loopback_addresses_are_refused(resolves, address):
    # 169.254.169.254 is the cloud metadata endpoint; this page drives a real browser.
    resolves(address)
    with pytest.raises(ValueError, match="private address"):
        demo.safe_start_url("https://anything.test/")


def test_the_bundled_fixture_is_the_one_allowed_loopback_url():
    assert demo.safe_start_url(f"{demo.ORIGIN}/fixture.html?scenario=travel")


def test_an_unresolvable_host_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: (_ for _ in ()).throw(socket.gaierror()))
    with pytest.raises(ValueError, match="Cannot resolve"):
        demo.safe_start_url("https://nope.invalid/")


def test_an_overlong_url_is_refused():
    with pytest.raises(ValueError, match="too long"):
        demo.safe_start_url("https://example.com/" + "a" * 2000)


# --- the catalog and its trace -----------------------------------------------------------


class FakeRecipe:
    def __init__(self, name="demo"):
        self.name, self.version, self.schedule = name, 3, "daily@07:00"
        self.params_from = "fake:sets"
        self.provenance = {"goal": "Open the date grid", "recorded_at": "2026-10-16T09:00:00+00:00",
                           "model": "jev-1.14.0"}
        self.steps = []


@pytest.fixture
def fake_catalog(monkeypatch):
    """Stand in for `usecases`, which the shipped wheel does not contain."""
    plan = {"sets": [{"origin": "MIA"}], "results": [], "delay": 0}

    class Recipes:
        RECIPES = None

        @staticmethod
        def load_all():
            return [FakeRecipe()]

        @staticmethod
        def load(name):
            if name != "demo":
                raise FileNotFoundError(name)
            return FakeRecipe()

    class Runner:
        @staticmethod
        def ledger_entries():
            return []

        @staticmethod
        def last_success(_name, _entries):
            return None

        @staticmethod
        def param_sets(_recipe, overrides):
            return [{**s, **overrides} for s in plan["sets"]]

        @staticmethod
        def run_recipe(recipe, params, *, repair, on_event):
            on_event("step", "s1", "open the grid")
            time.sleep(plan["delay"])
            on_event("finished", None, "ok")
            return {"status": "ok", "step": None, "failure": None, "reason": None,
                    "repairable": False, "recipe": recipe, "detail": {"rows": 49}}

    monkeypatch.setattr(demo, "catalog", lambda: (Recipes, Runner))
    monkeypatch.setattr(demo, "RECIPE_WORKER", None)
    demo.RECIPE_RUN.update(status="idle", recipe=None, mode=None, events=[], results=[])
    return plan


def wait_for_idle(timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not demo.recipe_state()["running"]:
            return demo.recipe_state()
        time.sleep(0.02)
    raise AssertionError("the recipe worker never finished")


def test_the_catalog_lists_what_was_recorded(fake_catalog):
    body = demo.recipe_catalog()
    assert body["available"] and body["recipes"][0]["name"] == "demo"
    assert body["recipes"][0]["version"] == 3 and body["recipes"][0]["fans_out"] is True
    assert body["recipes"][0]["last_ok"] is None


def test_a_missing_catalog_is_reported_not_raised(monkeypatch):
    # A pip-installed demo has no `usecases` package; the panel must still render.
    def missing():
        raise ModuleNotFoundError("No module named 'usecases'")

    monkeypatch.setattr(demo, "catalog", missing)
    body = demo.recipe_catalog()
    assert body["available"] is False and "usecases" in body["reason"] and body["recipes"] == []


def test_an_unknown_recipe_is_rejected_before_any_thread_starts(fake_catalog):
    with pytest.raises(ValueError, match="Unknown recipe"):
        demo.start_recipe({"name": "nope"})
    assert demo.recipe_state()["status"] == "idle"


def test_an_unknown_mode_is_rejected(fake_catalog):
    with pytest.raises(ValueError, match="replay"):
        demo.start_recipe({"name": "demo", "mode": "delete-everything"})


def test_params_must_be_flat_strings(fake_catalog):
    with pytest.raises(ValueError, match="object of strings"):
        demo.start_recipe({"name": "demo", "params": {"origin": {"nested": 1}}})


def test_a_run_records_a_trace_the_panel_can_render(fake_catalog):
    demo.start_recipe({"name": "demo", "mode": "replay"})
    run = wait_for_idle()
    assert run["status"] == "ok" and run["recipe"] == "demo"
    assert [e["stage"] for e in run["events"]] == ["params", "step", "finished"]
    assert run["results"][0]["status"] == "ok" and run["results"][0]["detail"] == {"rows": 49}


def test_a_second_run_while_one_is_going_is_refused(fake_catalog):
    fake_catalog["delay"] = 0.4
    demo.start_recipe({"name": "demo"})
    try:
        with pytest.raises(demo.Busy):
            demo.start_recipe({"name": "demo"})
    finally:
        wait_for_idle()


def test_a_worker_failure_is_recorded_rather_than_lost(fake_catalog, monkeypatch):
    recipes, runner = demo.catalog()
    monkeypatch.setattr(runner, "param_sets",
                        staticmethod(lambda *_a, **_k: (_ for _ in ()).throw(ValueError("no sets"))))
    demo.start_recipe({"name": "demo"})
    run = wait_for_idle()
    assert run["status"] == "failed"
    assert run["events"][-1]["stage"] == "error" and "no sets" in run["events"][-1]["detail"]


# --- HTTP guards -------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), demo.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(demo, "PORT", port)
    monkeypatch.setattr(demo, "ORIGIN", f"http://127.0.0.1:{port}")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def post(base, path, body, *, token=demo.TOKEN):
    request = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **({"X-Demo-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_a_post_without_the_token_is_forbidden(server, fake_catalog):
    status, body = post(server, "/api/recipes/run", {"name": "demo"}, token=None)
    assert status == 403 and "Local demo requests only" in body["error"]


def test_the_catalog_is_served_over_http(server, fake_catalog):
    with urllib.request.urlopen(f"{server}/api/recipes") as response:
        body = json.loads(response.read())
    assert body["available"] and body["recipes"][0]["name"] == "demo"


def test_an_unknown_recipe_over_http_is_a_400(server, fake_catalog):
    status, body = post(server, "/api/recipes/run", {"name": "nope"})
    assert status == 400 and "Unknown recipe" in body["error"]


def test_a_concurrent_run_over_http_is_a_409(server, fake_catalog):
    fake_catalog["delay"] = 0.4
    assert post(server, "/api/recipes/run", {"name": "demo"})[0] == 200
    try:
        status, body = post(server, "/api/recipes/run", {"name": "demo"})
        assert status == 409 and "already in progress" in body["error"]
    finally:
        wait_for_idle()


def test_a_rejected_start_url_never_opens_a_browser(server, monkeypatch):
    opened = []
    monkeypatch.setattr(demo, "Agent", lambda *a, **k: opened.append(a) or None)
    status, body = post(server, "/api/reset",
                        {"scenario": "custom", "url": "http://127.0.0.1:22/", "goal": "do a thing"})
    assert status == 400 and "private address" in body["error"]
    assert opened == []
