"""Loopback-only inspector for the Jev browser agent."""

import atexit
import json
import os
import secrets
import threading
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
# The Indeed ingest runs for minutes; it gets its own lock so it never blocks the inspector's own steps.
INGEST_LOCK = threading.Lock()
INGEST = {"running": False, "candidates": [], "failures": 0, "error": None, "dry_run": True}


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
        if scenario not in {"travel", "research", "flights"}:
            raise ValueError("Unknown demo scenario")
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        close_browser()
        AGENT = Agent(
            "https://www.google.com/travel/flights?hl=en"
            if scenario == "flights"
            else f"{ORIGIN}/fixture.html?scenario={scenario}",
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


def ingest_event(stage, name, detail):
    if stage == "listed":
        INGEST["candidates"] = [{"name": n, "stage": "queued", "detail": ""} for n in detail]
        return
    if stage == "finished":
        INGEST.update(running=False, failures=detail)
        return
    row = next((c for c in INGEST["candidates"] if c["name"] == name), None)
    if row is None:
        row = {"name": name, "stage": stage, "detail": ""}
        INGEST["candidates"].append(row)
    row.update(stage=stage, detail=str(detail))


def start_ingest(body):
    from usecases.indeed_resumes import process

    list_url = (body.get("list_url") or "").strip()
    if not list_url.startswith("https://employers.indeed.com/"):
        raise ValueError("Enter an employers.indeed.com candidates URL")
    if INGEST["running"] or not INGEST_LOCK.acquire(blocking=False):
        raise ValueError("An ingest run is already in progress")
    limit = body.get("limit")
    INGEST.update(running=True, candidates=[], failures=0, error=None, dry_run=bool(body.get("dry_run", True)))

    def worker():
        try:
            process(
                list_url,
                dry_run=INGEST["dry_run"],
                limit=int(limit) if limit else None,
                show=bool(body.get("show")),
                on_event=ingest_event,
            )
        except Exception as error:
            INGEST.update(running=False, error=str(error))
        finally:
            INGEST["running"] = False
            INGEST_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return dict(INGEST)


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
        if path == "/api/ingest/state":
            return self.send(200, json.dumps(INGEST))
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
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 8192:
            return self.send(400, json.dumps({"error": "Invalid request size"}))
        raw = self.rfile.read(length)
        if self.path == "/api/ingest/start":
            # Handled outside LOCK: the run takes minutes and must not block the inspector.
            try:
                return self.send(200, json.dumps(start_ingest(json.loads(raw))))
            except (ValueError, RuntimeError) as error:
                return self.send(400, json.dumps({"error": str(error)}))
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "A browser step is already running"}))
        try:
            body = json.loads(raw)
            result = command(self.path.removeprefix("/api/"), body)
            self.send(200, json.dumps(result))
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
