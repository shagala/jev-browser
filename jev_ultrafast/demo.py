"""Loopback-only inspector for the Jev browser agent."""

import atexit
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent import Agent
from .questions import MAX_STEPS

ROOT = Path(__file__).parent
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT = None
SCENARIOS = {"travel", "research", "flights"}

# A recipe run is minutes long, so it gets its own lock and its own thread: the inspector
# must stay steppable while one is going.
RECIPE_LOCK = threading.Lock()
RECIPE_WORKER = None
RECIPE_RUN = {"status": "idle", "recipe": None, "mode": None, "events": [], "results": []}


class Busy(Exception):
    """Something long-running already holds the browser."""


def catalog():
    """Import the recipe catalog lazily.

    `usecases` is the repository's own catalog: it is not shipped in the wheel, and it
    imports this module. Either reason alone rules out a module-level import.
    """
    from usecases import recipe as recipes
    from usecases import run as runner

    return recipes, runner


def safe_start_url(raw):
    """Only public http(s). This page asks a local service to drive a real browser.

    Best effort, not a guarantee: the name is resolved once here and again by Chrome, so
    a rebinding DNS record can still move. It stops the ordinary mistakes — file://,
    localhost, the router, cloud metadata.
    """
    raw = (raw or "").strip()
    if len(raw) > 2000:
        raise ValueError("That URL is too long")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter an http or https URL")
    if raw.startswith(f"{ORIGIN}/fixture.html"):
        return raw  # the bundled fixture is the one loopback address that is allowed
    try:
        resolved = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve {parsed.hostname}") from None
    for family, _type, _proto, _canon, address in resolved:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"{parsed.hostname} resolves to a private address ({ip})")
    return raw


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def response_state():
    state = AGENT.snapshot() if AGENT else {"page": None, "status": "idle", "history": [], "decision": None}
    return {**state, "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"), "max_steps": MAX_STEPS}


def close_browser():
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def command(name, body):
    global AGENT
    if name == "reset":
        scenario = body.get("scenario", "flights")
        if scenario == "custom":
            url = safe_start_url(body.get("url"))
        elif scenario in SCENARIOS:
            url = ("https://www.google.com/travel/flights?hl=en" if scenario == "flights"
                   else f"{ORIGIN}/fixture.html?scenario={scenario}")
        else:
            raise ValueError("Unknown demo scenario")
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        close_browser()
        AGENT = Agent(
            url,
            goal,
            screenshots=True,
            record_dir=Path.cwd() / "artifacts" / "frames" if body.get("record") else None,
        )
        AGENT.state["scenario"] = scenario
    else:
        if AGENT is None:
            raise ValueError("Start a demo first")
        AGENT.command(name, body)
    return response_state()


# --- the recipe catalog ----------------------------------------------------------------


def recipe_state():
    with RECIPE_LOCK:
        return {**RECIPE_RUN, "events": list(RECIPE_RUN["events"]),
                "results": list(RECIPE_RUN["results"]),
                "running": bool(RECIPE_WORKER and RECIPE_WORKER.is_alive())}


def recipe_catalog():
    """The catalog, or why there isn't one. A pip-installed demo has no `usecases`."""
    try:
        recipes, runner = catalog()
        entries = runner.ledger_entries()
        listed = recipes.load_all()
    except Exception as error:
        return {"available": False, "reason": str(error), "recipes": []}
    out = []
    for item in listed:
        previous = runner.last_success(item.name, entries)
        out.append({
            "name": item.name,
            "version": item.version,
            "schedule": item.schedule or "manual",
            "goal": item.provenance.get("goal"),
            "recorded_at": item.provenance.get("recorded_at"),
            "model": item.provenance.get("model"),
            "fans_out": bool(item.params_from),
            "last_ok": previous.isoformat(timespec="seconds") if previous else None,
            "steps": [{"id": s.id, "intent": s.intent, "op": s.op,
                       "label": s.match.label if s.match else None,
                       "role": s.match.role if s.match else None}
                      for s in item.steps],
        })
    return {"available": True, "reason": None, "recipes": out}


def recipe_job(item, mode, overrides):
    """Replay every parameter set, appending one trace the UI polls."""
    _recipes, runner = catalog()

    def emit(stage, step, detail):
        with RECIPE_LOCK:
            RECIPE_RUN["events"].append({"stage": stage, "step": step, "detail": str(detail),
                                         "at": round(time.time(), 3)})

    status = "ok"
    try:
        for params in runner.param_sets(item, overrides):
            emit("params", None, " ".join(f"{key}={value}" for key, value in params.items()))
            started = time.monotonic()
            result = runner.run_recipe(item, params, repair=(mode == "run"), on_event=emit)
            item = result["recipe"]  # a repair rewrites the recipe mid-fan-out
            if result["status"] == "failed":
                status = "failed"
            with RECIPE_LOCK:
                RECIPE_RUN["results"].append({
                    "params": params, "status": result["status"], "failure": result["failure"],
                    "reason": result["reason"], "detail": result.get("detail"),
                    "ms": round((time.monotonic() - started) * 1000),
                })
    except Exception as error:  # a worker thread has nowhere to raise
        emit("error", None, str(error))
        status = "failed"
    with RECIPE_LOCK:
        RECIPE_RUN["status"] = status


def start_recipe(body):
    global RECIPE_WORKER
    if RECIPE_WORKER and RECIPE_WORKER.is_alive():
        raise Busy("A recipe run is already in progress")
    mode = body.get("mode", "replay")
    if mode not in {"replay", "run"}:
        raise ValueError("mode must be 'replay' or 'run'")
    overrides = body.get("params") or {}
    if not isinstance(overrides, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
    ):
        raise ValueError("params must be an object of strings")
    recipes, _runner = catalog()
    name = body.get("name", "")
    if name not in {item.name for item in recipes.load_all()}:
        raise ValueError(f"Unknown recipe {name!r}")
    item = recipes.load(name)
    with RECIPE_LOCK:
        RECIPE_RUN.update(status="running", recipe=item.name, mode=mode, events=[], results=[])
    RECIPE_WORKER = threading.Thread(target=recipe_job, args=(item, mode, overrides), daemon=True)
    RECIPE_WORKER.start()
    return recipe_state()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/state":
            with LOCK:
                return self.send(200, json.dumps(response_state()))
        # Both read their own guarded state, so a long replay never blocks them on LOCK.
        if path == "/api/recipes":
            return self.send(200, json.dumps(recipe_catalog()))
        if path == "/api/recipes/state":
            return self.send(200, json.dumps(recipe_state()))
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        files = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/fixture.html": ("fixture.html", "text/html"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local demo requests only"}))
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "A browser step is already running"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 8192:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            name = self.path.removeprefix("/api/")
            # start_recipe returns at once; the run itself is on its own thread and lock.
            result = start_recipe(body) if name == "recipes/run" else command(name, body)
            self.send(200, json.dumps(result))
        except Busy as error:
            self.send(409, json.dumps({"error": str(error)}))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "Local demo failed; no automatic retry. Reset to recover."}))
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    atexit.register(close_browser)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Ultrafast: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
