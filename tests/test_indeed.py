"""Offline contracts for the Indeed ingest. No browser, no Indeed, no Flux."""

import json
from unittest.mock import Mock

import pytest

from usecases import indeed_resumes as indeed

PAGE = """Skip to main content
recruiter@example.com
Ann Lee
Driver · Springfield, OR
Applied to: CDL-A Company Driver, Salaried $90K-$110K, No-Touch • Portland, OR 97217
ann.lee@example.com
503-555-0100
"""


def browser_with(text):
    return Mock(evaluate=Mock(return_value=json.dumps({"text": text})))


def test_contact_reads_name_email_phone_and_position(monkeypatch):
    monkeypatch.setenv("INDEED_ACCOUNT_EMAIL", "recruiter@example.com")
    person, position = indeed.contact(browser_with(PAGE), "Ann Lee")
    assert person == {
        "firstName": "Ann",
        "lastName": "Lee",
        "email": "ann.lee@example.com",
        "phone": "503-555-0100",
    }
    assert position == "CDL-A Company Driver, Salaried $90K-$110K, No-Touch"


def test_contact_never_reports_the_employers_own_address(monkeypatch):
    monkeypatch.setenv("INDEED_ACCOUNT_EMAIL", "recruiter@example.com")
    person, _ = indeed.contact(browser_with("Applied to: Driver\nrecruiter@example.com\n"), "Ann Lee")
    assert "email" not in person


def test_contact_prefers_a_real_address_over_indeeds_relay(monkeypatch):
    monkeypatch.setenv("INDEED_ACCOUNT_EMAIL", "boss@example.com")
    text = "Applied to: Driver\nrelay_9k@indeedemail.com\nann.lee@example.com\n"
    person, _ = indeed.contact(browser_with(text), "Ann Lee")
    assert person["email"] == "ann.lee@example.com"


def test_candidate_id_comes_from_the_view_url():
    assert indeed.candidate_id("https://employers.indeed.com/candidates/view?id=aaaa1111bbbb&l=MG") == "aaaa1111bbbb"


def test_slug_matches_a_resume_filename_to_its_candidate():
    assert indeed.slug("Mary Jo Smith") == "maryjosmith"
    assert indeed.slug("Ann Lee") in "resumeannlee.pdf".replace(".pdf", "")


def test_read_resume_rejects_a_non_pdf_payload():
    import base64

    body = base64.b64encode(b"<html>").decode()
    payload = json.dumps({"name": "ResumeAnnLee.pdf", "type": "text/html", "b64": body})
    browser = Mock(call=Mock(return_value={"result": {"value": payload}}))
    with pytest.raises(ValueError, match="not a PDF"):
        indeed.read_resume(browser, "Ann Lee")


def test_ingest_raises_on_a_flux_error(monkeypatch):
    monkeypatch.setenv("FLUX_INGEST_URL", "https://flux.test/ingest")
    monkeypatch.setenv("FLUX_API_TOKEN", "token")
    failure = Mock(is_error=True, status_code=503, text="not_configured")
    monkeypatch.setattr(indeed.httpx, "post", Mock(return_value=failure))
    with pytest.raises(ValueError, match="503"):
        indeed.ingest({"firstName": "A", "lastName": "B"}, "Driver", "abc", "r.pdf", b"%PDF-1.7")


def test_ingest_sends_the_multipart_contract(monkeypatch):
    monkeypatch.setenv("FLUX_INGEST_URL", "https://flux.test/ingest")
    monkeypatch.setenv("FLUX_API_TOKEN", "token")
    post = Mock(return_value=Mock(is_error=False, text='{"status":"created"}'))
    monkeypatch.setattr(indeed.httpx, "post", post)
    indeed.ingest({"firstName": "Ann", "lastName": "Lee"}, "Driver", "abc123", "r.pdf", b"%PDF-1.7")
    sent = json.loads(post.call_args.kwargs["data"]["data"])
    assert sent["externalSourceName"] == "indeed"
    assert sent["externalApplicationId"] == "abc123"
    assert sent["position"] == "Driver"
    assert sent["candidate"]["firstName"] == "Ann"
    assert post.call_args.kwargs["files"]["file"][0] == "r.pdf"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
