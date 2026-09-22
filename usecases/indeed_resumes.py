"""Download new Indeed applicants' resumes and ingest them into Flux, then mark them Reviewing.

uv run --env-file .env python -m usecases.indeed_resumes \\
  --list-url '<candidates?statusName=New...>' [--dry-run] [--limit N]

The candidate list, the resume blob, and the status control are all read from the observed DOM.
Indeed's own status is the ledger: a candidate leaves the New queue only after a successful ingest.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from browser_harness.helpers import cdp

from jev_ultrafast.browser import Browser
from jev_ultrafast.demo import load_environment

RESUMES = Path("artifacts/resumes")
PROCESSED_STATUS = "Reviewing"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE = re.compile(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}")
CANDIDATE_LINKS = """JSON.stringify([...document.querySelectorAll('a[href*="/candidates/view"]')]
  .map(a => ({name: (a.innerText || '').replace(/\\s+/g, ' ').trim(), href: a.href}))
  .filter(c => c.name))"""
# The resume is already a Blob in the page; read its bytes directly instead of driving a file download.
READ_RESUME = """(async () => {
  const wanted = %s;
  const anchor = [...document.querySelectorAll('a[download]')]
    .find(a => (a.getAttribute('download') || '').replace(/[^A-Za-z]/g, '').toLowerCase().includes(wanted));
  if (!anchor) return null;
  const blob = await (await fetch(anchor.href)).blob();
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return JSON.stringify({name: anchor.getAttribute('download'), type: blob.type, b64: btoa(binary)});
})()"""
DETAILS = """JSON.stringify({text: document.body.innerText})"""
APPLIED_TO = re.compile(r"Applied to:\s*([^\n]+)")


def slug(name):
    return re.sub(r"[^A-Za-z]", "", name).lower()


def candidate_id(href):
    return parse_qs(urlparse(href).query).get("id", [""])[0]


def read_resume(browser, name):
    """Poll until the candidate's own resume blob is present; returns (filename, bytes)."""
    expression = READ_RESUME % json.dumps(slug(name))
    for _ in range(20):
        response = browser.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        value = response.get("result", {}).get("value")
        if value:
            payload = json.loads(value)
            data = base64.b64decode(payload["b64"])
            if not data.startswith(b"%PDF"):
                raise ValueError(f"{payload['name']} is not a PDF ({payload['type']}, {len(data)} bytes)")
            return payload["name"], data
        time.sleep(0.75)
    raise ValueError(f"no resume download appeared for {name}")


def contact(browser, name):
    details = json.loads(browser.evaluate(DETAILS))
    account = os.environ.get("INDEED_ACCOUNT_EMAIL", "")
    emails = [e for e in EMAIL.findall(details["text"]) if e != account and not e.endswith("indeed.com")]
    # Indeed relays through <handle>@indeedemail.com; prefer the candidate's own address when it is shown.
    emails.sort(key=lambda e: e.endswith("indeedemail.com"))
    phones = PHONE.findall(details["text"])
    first, _, last = name.partition(" ")
    person = {"firstName": first, "lastName": last or first}
    if emails:
        person["email"] = emails[0]
    if phones:
        person["phone"] = phones[0]
    applied = APPLIED_TO.search(details["text"])
    position = applied.group(1).split("•")[0].strip() if applied else ""
    return person, position


def ingest(person, position, candidate, filename, data):
    payload = {
        "externalSourceName": "indeed",
        "externalApplicationId": candidate,
        "position": position,
        "candidate": person,
    }
    response = httpx.post(
        os.environ["FLUX_INGEST_URL"],
        headers={"Authorization": f"Bearer {os.environ['FLUX_API_TOKEN']}"},
        data={"data": json.dumps(payload)},
        files={"file": (filename, data, "application/pdf")},
        # Flux screens the resume synchronously; observed ~65 s for a 64 KB PDF.
        timeout=float(os.environ.get("FLUX_TIMEOUT", "300")),
    )
    if response.is_error:
        raise ValueError(f"Flux returned HTTP {response.status_code}: {response.text[:200]}")
    return response.text[:200]


def mark_processed(browser):
    """Set the candidate's status so the next run's New queue no longer offers them."""
    page = browser.observe(screenshot=False)
    combo = next((a for a in page["actions"] if a["kind"] == "click" and "Select Status" in a["label"]), None)
    if not combo:
        raise ValueError("no status control on this page")
    browser.act(combo, page)
    time.sleep(1.2)
    page = browser.observe(screenshot=False)
    option = next((a for a in page["actions"] if a["label"].strip() == PROCESSED_STATUS), None)
    if not option:
        raise ValueError(f"{PROCESSED_STATUS} not offered in the status menu")
    browser.act(option, page)
    time.sleep(1.5)


def process(list_url, *, dry_run=False, limit=None, show=False, live=False, on_event=None):
    """Walk the New queue: read each resume blob, ingest it, then mark the candidate.

    on_event(stage, name, detail) reports progress; the CLI prints it and the inspector renders it.
    Returns the number of failures.
    """
    emit = on_event or (lambda *_args: None)

    def frame(browser, label=""):
        """Optional JPEG for the inspector's live view; the CLI path never pays for it."""
        if not live:
            return
        try:
            shot = browser.call("Page.captureScreenshot", format="jpeg", quality=60)["data"]
            emit("frame", None, {"jpeg": shot, "url": browser.evaluate("location.href"), "title": label})
        except Exception:
            pass

    RESUMES.mkdir(parents=True, exist_ok=True)
    browser = Browser(list_url)
    failures = 0
    try:
        if show:
            # The agent works in a background tab by default; surface it for a live demo.
            cdp("Target.activateTarget", targetId=browser.target)
        time.sleep(6)
        page = browser.observe(screenshot=False)
        if "secure.indeed.com/auth" in page["url"] or "Sign In" in page["title"]:
            raise ValueError("Indeed is signed out; sign in to employers.indeed.com in this Chrome profile and retry")
        candidates = json.loads(browser.evaluate(CANDIDATE_LINKS))
        if limit:
            candidates = candidates[:limit]
        emit("listed", None, [c["name"] for c in candidates])
        frame(browser, "New applicants")
        for entry in candidates:
            name = entry["name"]
            try:
                emit("opening", name, "opening profile")
                browser.call("Page.navigate", url=entry["href"])
                time.sleep(5)
                frame(browser, f"{name} · profile")
                filename, data = read_resume(browser, name)
                person, position = contact(browser, name)
                path = RESUMES / f"{candidate_id(entry['href'])}-{filename}"
                path.write_bytes(data)
                emit("downloaded", name, f"{len(data):,} B → {path}")
                frame(browser, f"{name} · resume read")
                if dry_run:
                    emit("dry-run", name, f"would ingest {person} | {position}")
                    continue
                emit("uploading", name, "ingesting into Flux (screening takes ~1 min)")
                reply = ingest(person, position, candidate_id(entry["href"]), filename, data)
                emit("uploaded", name, reply)
                mark_processed(browser)
                emit("done", name, f"marked {PROCESSED_STATUS}")
                frame(browser, f"{name} · {PROCESSED_STATUS}")
            except Exception as error:
                failures += 1
                emit("failed", name, str(error))
    finally:
        browser.close()
    emit("finished", None, failures)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-url", required=True, help="Indeed candidates URL filtered to statusName=New")
    parser.add_argument("--dry-run", action="store_true", help="Download only; no upload, no status change.")
    parser.add_argument("--limit", type=int, help="Process at most N candidates.")
    parser.add_argument("--show", action="store_true", help="Bring the agent's tab to the front so the run is visible.")
    args = parser.parse_args(argv)
    load_environment()

    def report(stage, name, detail):
        if stage == "listed":
            print(f"{len(detail)} new candidate(s): {', '.join(detail)}", flush=True)
        elif stage == "finished":
            print(f"{detail} failure(s)" if detail else "all candidates processed", flush=True)
        else:
            print(f"  {name:20} {stage:11} {detail}", flush=True)

    return 1 if process(
        args.list_url, dry_run=args.dry_run, limit=args.limit, show=args.show, on_event=report
    ) else 0


if __name__ == "__main__":
    sys.exit(main())
