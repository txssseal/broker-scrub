"""Regression tests for confirmed state-machine / reply-auth / ingest findings.
Each fails against the pre-fix code."""

from pathlib import Path

from brokerscrub import registry
from brokerscrub.config import Config
from brokerscrub.db import Store
from brokerscrub.inbox import process_message

FIXTURE = Path(__file__).parent / "fixtures" / "registry_sample.csv"


def _raw(headers: dict, body: str) -> bytes:
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return (head + "\r\n" + body).encode()


def _sent(tmp_path, domains=("acme-broker.test",)):
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme Data LLC", email="privacy@acme-broker.test",
                                 domains=list(domains))
    req = store.create_request(bid)
    store.mark_sent(req["id"], "<orig-mid@example.test>")
    return store, store.request(req["id"]), bid


# ---- #5 confirmed is terminal: no downgrade -------------------------------

def test_confirmed_not_downgraded_by_followup(tmp_path):
    store, req, _ = _sent(tmp_path)
    store.set_status(req["id"], "confirmed", "deleted")
    # in-thread follow-up (survey) with an allowlisted link
    raw = _raw({"From": "privacy@acme-broker.test", "To": "testuser@example.test",
                "Subject": "Rate your experience", "In-Reply-To": "<orig-mid@example.test>"},
               "Thanks! Visit https://acme-broker.test/survey")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("ignored (confirmed")
    r = store.request(req["id"])
    assert r["status"] == "confirmed" and r["recheck_at"] is not None


# ---- #6 a never-sent draft is never flipped by a domain email --------------

def test_draft_not_flipped_by_domain_email(tmp_path):
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="PeopleFinder", email="privacy@pf.test",
                                 domains=["pf.test"])
    draft = store.create_request(bid)  # never sent
    raw = _raw({"From": "marketing@pf.test", "To": "testuser@example.test",
                "Subject": "Newsletter"}, "buy stuff")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("unmatched")
    assert store.request(draft["id"])["status"] == "draft"


# ---- #10 (LIKE injection) forged wildcard domain matches nothing ----------

def test_like_wildcard_domain_no_match(tmp_path):
    store, req, _ = _sent(tmp_path)
    assert store.find_open_request_by_domain("%.com") is None
    assert store.find_open_request_by_domain("acme-broker.test")["id"] == req["id"]


# ---- #4 spoofed (weak) confirmation cannot confirm ------------------------

def test_spoofed_domain_confirmation_is_manual_review(tmp_path):
    store, req, _ = _sent(tmp_path)
    raw = _raw({"From": "anything@acme-broker.test", "To": "testuser@example.test",
                "Subject": "done"},   # no token, no In-Reply-To -> weak
               "Your personal information has been deleted from our systems.")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert "manual review" in out
    assert store.request(req["id"])["status"] == "replied"   # NOT confirmed


# ---- mark_sent race guard --------------------------------------------------

def test_mark_sent_only_from_draft(tmp_path):
    store, req, _ = _sent(tmp_path)         # already sent once
    store.set_status(req["id"], "confirmed", "done")
    assert store.mark_sent(req["id"], "<new@x>") is False
    assert store.request(req["id"])["status"] == "confirmed"


# ---- #12 header-only DSN bounce is matched --------------------------------

def test_header_only_dsn_bounce(tmp_path):
    store, req, _ = _sent(tmp_path)
    dsn = (
        b"From: MAILER-DAEMON@mx.example.test\r\n"
        b"To: testuser@example.test\r\n"
        b"Subject: Undelivered Mail Returned to Sender\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\n550 no such user\r\n"
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Final-Recipient: rfc822; privacy@acme-broker.test\r\nAction: failed\r\n"
        b"--B\r\nContent-Type: text/rfc822-headers\r\n\r\n"
        b"Message-ID: <orig-mid@example.test>\r\n"
        b"Subject: Personal Data Deletion Request\r\n"
        b"--B--\r\n"
    )
    out = process_message(store, Config(), dsn, echo=lambda *_: None)
    assert out.startswith("bounced")
    assert store.request(req["id"])["status"] == "bounced"


# ---- #19 unknown-charset body still scanned for confirmation ---------------

def test_unknown_charset_body_still_confirms(tmp_path):
    store, req, _ = _sent(tmp_path)
    raw = (
        b"From: privacy@acme-broker.test\r\n"
        b"To: testuser@example.test\r\n"
        b"Subject: done\r\n"
        b"In-Reply-To: <orig-mid@example.test>\r\n"
        b'Content-Type: text/plain; charset="x-unknown-1"\r\n'
        b"\r\n"
        b"Your personal information has been deleted from our systems.\r\n"
    )
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("confirmed")


# ---- #? control-char sanitization in echoed/stored From -------------------

def test_from_header_control_chars_stripped(tmp_path):
    store, _, _ = _sent(tmp_path)
    raw = _raw({"From": "evil\x1b[2Jattacker@nowhere.test", "To": "testuser@example.test",
                "Subject": "x"}, "spam")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert "\x1b" not in out


# ---- #8 partial CSV ingest never mass-deactivates -------------------------

def test_partial_csv_ingest_does_not_deactivate(tmp_path):
    store = Store(tmp_path / "t.db")
    registry.ingest(store, FIXTURE.read_text(encoding="utf-8-sig"), "full",
                    is_full_registry=True)
    active_before = len(store.brokers())
    assert active_before == 2
    one = ("Data broker name:,Data broker primary contact email address:,"
           "Data broker primary website:\n"
           "New Broker LLC,privacy@newbroker.test,https://newbroker.test\n")
    stats = registry.ingest(store, one, "manual.csv", is_full_registry=False)
    assert stats["deactivated"] == 0
    assert len(store.brokers()) == active_before + 1  # nothing deactivated


def test_full_ingest_deactivates_missing(tmp_path):
    store = Store(tmp_path / "t.db")
    registry.ingest(store, FIXTURE.read_text(encoding="utf-8-sig"), "full",
                    is_full_registry=True)
    # a fresh full registry that contains only 1 of the 2 -> floor(0.5) allows it
    smaller = ("Data broker name:,Data broker primary contact email address:,"
               "Data broker primary website:\n"
               "Acme Data LLC,privacy@acme-broker.test,https://acme-broker.test\n")
    stats = registry.ingest(store, smaller, "full2", is_full_registry=True)
    assert stats["deactivated"] == 1


# ---- #14 multi-URL registry cell parsing ----------------------------------

def test_multi_url_cell_split(tmp_path):
    csv = ("Data broker name:,Data broker primary contact email address:,"
           "Data broker primary website:\n"
           "BeenLike LLC,privacy@beenlike.test,"
           "https://www.beenlike.test; https://www.peoplelooker.test\n")
    brokers, _ = registry.parse(csv)
    b = brokers[0]
    assert b["website"] == "https://www.beenlike.test"
    assert "beenlike.test" in b["domains"] and "peoplelooker.test" in b["domains"]
    # no junk 'beenlike.test; https' style entries
    assert all(";" not in d and " " not in d for d in b["domains"])


def test_multitenant_onetrust_stored_as_full_host():
    csv = ("Data broker name:,Data broker primary contact email address:,"
           "Data broker primary website:,"
           "Data broker's primary website that contains details on how consumers "
           "can exercise their CA Consumer Privacy rights:\n"
           "Bigco Inc,privacy@bigco.test,https://bigco.test,"
           "https://privacyportal.onetrust.com/webform/abc123\n")
    brokers, _ = registry.parse(csv)
    doms = brokers[0]["domains"]
    assert "privacyportal.onetrust.com" in doms
    assert "onetrust.com" not in doms   # bare platform domain never trusted
