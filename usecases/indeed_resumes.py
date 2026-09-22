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
APPLIED_TO = re.compile(r"Applied to:\s*\n([^\n]+)")


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-url", required=True, help="Indeed candidates URL filtered to statusName=New")
    parser.add_argument("--dry-run", action="store_true", help="Download only; no upload, no status change.")
    parser.add_argument("--limit", type=int, help="Process at most N candidates.")
    parser.add_argument("--show", action="store_true", help="Bring the agent's tab to the front so the run is visible.")
    args = parser.parse_args(argv)
    load_environment()
    RESUMES.mkdir(parents=True, exist_ok=True)
    browser = Browser(args.list_url)
    failures = 0
    try:
        if args.show:
            # The agent works in a background tab by default; surface it for a live demo.
            cdp("Target.activateTarget", targetId=browser.target)
        time.sleep(6)
        candidates = json.loads(browser.evaluate(CANDIDATE_LINKS))[: args.limit]
        print(f"{len(candidates)} new candidate(s): {', '.join(c['name'] for c in candidates)}", flush=True)
        for entry in candidates:
            name = entry["name"]
            try:
                browser.call("Page.navigate", url=entry["href"])
                time.sleep(5)
                filename, data = read_resume(browser, name)
                person, position = contact(browser, name)
                path = RESUMES / f"{candidate_id(entry['href'])}-{filename}"
                path.write_bytes(data)
                note = f"{len(data):,} B → {path}"
                if args.dry_run:
                    print(f"  {name:20} dry-run  {note}  {person} | {position[:40]}", flush=True)
                    continue
                reply = ingest(person, position, candidate_id(entry["href"]), filename, data)
                mark_processed(browser)
                print(f"  {name:20} ok       {note}  ingested, marked {PROCESSED_STATUS}  {reply[:60]}", flush=True)
            except Exception as error:
                failures += 1
                print(f"  {name:20} FAILED   {error}", flush=True)
    finally:
        browser.close()
    print(f"{failures} failure(s)" if failures else "all candidates processed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
