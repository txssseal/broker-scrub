from datetime import timedelta
from pathlib import Path

from brokerscrub import letter, registry, sender
from brokerscrub.config import Config, Identity, SendCfg, Smtp
from brokerscrub.db import Store, new_token, parse_iso, utcnow
from brokerscrub.inbox import CONFIRM_RE, TOKEN_RE, process_message
from brokerscrub.verifier import (
    extract_links,
    host_resolves_public,
    link_allowed,
    registrable_domain,
)

FIXTURE = Path(__file__).parent / "fixtures" / "registry_sample.csv"


def make_identity():
    return Identity(full_name="Test User", aliases=["T. User", "Testy User"],
                    emails=["testuser@example.test"],
                    phones=["+1 555 0100"], addresses=["1 Main St, Austin TX"],
                    state="TX")


def make_config():
    return Config(identity=make_identity(),
                  smtp=Smtp(host="smtp.example.test", from_addr="testuser@example.test"),
                  send=SendCfg(plus_addressing=True))


# ---- registry --------------------------------------------------------------

def test_registry_parse_fixture():
    brokers, skipped = registry.parse(FIXTURE.read_text(encoding="utf-8-sig"))
    assert len(brokers) == 2
    assert len(skipped) == 1 and "No Email Broker" in skipped[0]
    acme = brokers[0]
    assert acme["name"] == "Acme Data LLC"
    assert acme["email"] == "privacy@acme-broker.test"
    assert "acme-broker.test" in acme["domains"]
    pf = brokers[1]
    assert pf["website"] == "https://www.peoplefinder-example.com"
    assert pf["domains"] == ["peoplefinder-example.com"]


def test_registry_ingest_upsert(tmp_path):
    store = Store(tmp_path / "t.db")
    text = FIXTURE.read_text(encoding="utf-8-sig")
    stats = registry.ingest(store, text, "fixture")
    assert stats["created"] == 2 and stats["updated"] == 0
    stats = registry.ingest(store, text, "fixture")
    assert stats["created"] == 0 and stats["updated"] == 2
    assert len(store.brokers()) == 2


# ---- domains / links -------------------------------------------------------

def test_registrable_domain():
    assert registrable_domain("mail.foo.co.uk") == "foo.co.uk"
    assert registrable_domain("https://sub.example.com/x?y=1") == "example.com"
    assert registrable_domain("acme-broker.test") == "acme-broker.test"
    assert registrable_domain("1.2.3.4") is None
    assert registrable_domain("[::1]") is None
    assert registrable_domain("localhost") is None
    assert registrable_domain("") is None


def test_extract_links_dedup():
    html = '<a href="https://a.example/one">x</a><a href="https://a.example/one">y</a>'
    text = "visit https://b.example/two. now"
    links = extract_links(text, html)
    assert links == ["https://a.example/one", "https://b.example/two"]


def test_link_allowed_blocks():
    ok, why = link_allowed("ftp://acme-broker.test/x", ["acme-broker.test"], [])
    assert not ok and "scheme" in why
    ok, why = link_allowed("http://10.0.0.1/x", ["acme-broker.test"], [])
    assert not ok
    ok, why = link_allowed("https://evil.example/x", ["acme-broker.test"], [])
    assert not ok and "not in broker allowlist" in why
    ok, _ = link_allowed("http://localhost:9999/x", ["acme-broker.test"], ["localhost"])
    assert ok


def test_private_hosts_not_public():
    assert not host_resolves_public("127.0.0.1")
    assert not host_resolves_public("10.1.2.3")
    assert not host_resolves_public("192.168.1.5")
    assert not host_resolves_public("this-host-does-not-exist.invalid")


# ---- letters / tokens ------------------------------------------------------

def test_token_format():
    for _ in range(50):
        t = new_token()
        assert TOKEN_RE.fullmatch(t), t
        assert not set("01OI") & set(t[3:])


def test_letter_contents():
    ident = make_identity()
    body = letter.body(ident, "Acme Data LLC", "BS-ABCDEF2345")
    for needle in ("1798.105", "541.051(b)(4)", "1798.120", "541.055(a)", "45 days",
                   "Acme Data LLC", "Test User", "testuser@example.test",
                   "BS-ABCDEF2345", "T. User", "Testy User", "also appear"):
        assert needle in body, needle
    subj = letter.subject(ident, "BS-ABCDEF2345")
    # legal name is intentionally kept OUT of the subject line
    assert "Test User" not in subj
    assert "BS-ABCDEF2345" in subj and "TDPSA" in subj


# ---- state machine ---------------------------------------------------------

def test_request_lifecycle(tmp_path):
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme", email="p@acme.test",
                                 domains=["acme.test"])
    req = store.create_request(bid)
    assert req["status"] == "draft"
    assert store.open_request_for_broker(bid)["id"] == req["id"]

    store.mark_sent(req["id"], "<mid@x>")
    r = store.request(req["id"])
    assert r["status"] == "sent"
    deadline = parse_iso(r["deadline_at"])
    assert timedelta(days=44) < (deadline - utcnow()) <= timedelta(days=45)

    store.set_status(req["id"], "confirmed", "done")
    r = store.request(req["id"])
    assert r["confirmed_at"] and r["recheck_at"]
    recheck = parse_iso(r["recheck_at"])
    assert timedelta(days=89) < (recheck - utcnow()) <= timedelta(days=90)
    assert store.open_request_for_broker(bid) is None
    assert store.find_request_by_token(req["token"])["id"] == req["id"]
    assert store.find_request_by_message_id("<mid@x>")["id"] == req["id"]


# ---- message building ------------------------------------------------------

def test_build_message_headers(tmp_path):
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme", email="p@acme.test",
                                 domains=["acme.test"])
    req = store.create_request(bid)
    cfg = make_config()
    msg = sender.build_message(cfg, store.broker(bid), req)
    assert msg["To"] == "p@acme.test"
    assert "Test User" in msg["From"] and "testuser@example.test" in msg["From"]
    assert req["token"] in msg["Subject"]
    assert msg["Message-ID"].endswith("@example.test>")
    assert msg["Reply-To"] == f"testuser+{req['token'].lower()}@example.test"


# ---- reply classification --------------------------------------------------

def _mk_store_with_sent(tmp_path):
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme Data LLC", email="privacy@acme-broker.test",
                                 domains=["acme-broker.test"])
    req = store.create_request(bid)
    store.mark_sent(req["id"], "<orig-mid@example.test>")
    return store, store.request(req["id"])


def _raw(headers: dict, body: str) -> bytes:
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return (head + "\r\n" + body).encode()


def test_reply_confirmation_via_in_reply_to(tmp_path):
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "Privacy <privacy@acme-broker.test>",
                "To": "testuser@example.test",
                "Subject": "Re: your request",
                "In-Reply-To": "<orig-mid@example.test>"},
               "Your personal information has been deleted from our systems.")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("confirmed")
    r = store.request(req["id"])
    assert r["status"] == "confirmed" and r["recheck_at"]


def test_reply_token_match_lowercase(tmp_path):
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "no-reply@acme-broker.test",
                "To": f"testuser+{req['token'].lower()}@example.test",
                "Subject": "We received your request"},
               "We are processing it.")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("replied")
    assert store.request(req["id"])["status"] == "replied"


def test_reply_domain_match(tmp_path):
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "support@mail.acme-broker.test",
                "To": "testuser@example.test",
                "Subject": "About your privacy request"},
               "We need more information.")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("replied")


def test_bounce(tmp_path):
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "MAILER-DAEMON@mx.example.test",
                "To": "testuser@example.test",
                "Subject": "Undelivered Mail Returned to Sender",
                "In-Reply-To": "<orig-mid@example.test>"},
               "550 mailbox unavailable")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("bounced")
    assert store.request(req["id"])["status"] == "bounced"


def test_unmatched_reply(tmp_path):
    store, _ = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "rando@unrelated.example",
                "To": "testuser@example.test",
                "Subject": "hello"}, "spam")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("unmatched")


def test_skips_unallowlisted_link(tmp_path):
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "privacy@acme-broker.test",
                "To": "testuser@example.test",
                "Subject": "Please verify",
                "In-Reply-To": "<orig-mid@example.test>"},
               "Confirm here: https://evil-tracker.example/click?id=1")
    process_message(store, Config(), raw, echo=lambda *_: None)
    r = store.request(req["id"])
    assert r["status"] == "replied"  # not verified
    kinds = [e["kind"] for e in store.events_for(req["id"])]
    assert "link_skipped" in kinds and "link_visited" not in kinds


def test_is_action_link():
    from brokerscrub.inbox import is_action_link
    assert is_action_link("https://x.com/confirm?token=abc")
    assert is_action_link("https://x.com/verify/123")
    assert is_action_link("https://x.com/opt-out")
    assert is_action_link("https://x.com/unsubscribe?u=9")
    # policy pages / portal landings / homepages are NOT action links
    assert not is_action_link("https://x.com/privacy-policy/")
    assert not is_action_link("https://platform.fullcontact.com/your-privacy-choices")
    assert not is_action_link("https://x.com/")
    assert not is_action_link("https://x.com/help-center/privacy-requests")
    assert not is_action_link("https://x.com/?utm_source=signature")


def test_portal_deflection_not_counted_verified(tmp_path):
    """The FullContact case: 'verify your identity at our portal' must NOT be
    marked verified just because a portal link is on the broker's domain."""
    store, req = _mk_store_with_sent(tmp_path)
    raw = _raw({"From": "privacy@acme-broker.test", "To": "testuser@example.test",
                "Subject": "Re: your request", "In-Reply-To": "<orig-mid@example.test>"},
               "We need you to verify your identity. Visit our Privacy Choices "
               "portal: https://acme-broker.test/your-privacy-choices")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert "portal" in out
    r = store.request(req["id"])
    assert r["status"] == "replied"  # NOT verified
    kinds = [e["kind"] for e in store.events_for(req["id"])]
    assert "link_visited" not in kinds and "link_skipped" in kinds


def test_genuine_confirmation_link_is_verified(tmp_path, monkeypatch):
    store, req = _mk_store_with_sent(tmp_path)
    monkeypatch.setattr("brokerscrub.inbox.visit", lambda *a, **k: (True, "visited ok"))
    raw = _raw({"From": "privacy@acme-broker.test", "To": "testuser@example.test",
                "Subject": "confirm", "In-Reply-To": "<orig-mid@example.test>"},
               "Click to confirm your deletion: https://acme-broker.test/confirm?token=xyz")
    out = process_message(store, Config(), raw, echo=lambda *_: None)
    assert out.startswith("verified")
    assert store.request(req["id"])["status"] == "verified"


def test_followup_letter_and_threading(tmp_path):
    ident = make_identity()
    body = letter.followup_body(ident, "Acme Data LLC", "BS-ABCDEF2345")
    for needle in ("541.052(e)", "541.055(b)", "541.051(b)", "541.052(b)", "541.053",
                   "541.155", "Acme Data LLC", "Test User", "BS-ABCDEF2345",
                   "new account"):
        assert needle in body, needle
    assert letter.followup_subject("Personal Data Deletion Request X").startswith("Re:")
    assert letter.followup_subject("Re: X") == "Re: X"

    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme", email="p@acme.test", domains=["acme.test"])
    req = store.create_request(bid)
    store.mark_sent(req["id"], "<orig@example.test>")
    msg = sender.build_followup(make_config(), store.broker(bid), store.request(req["id"]))
    assert msg["In-Reply-To"] == "<orig@example.test>"
    assert msg["References"] == "<orig@example.test>"
    assert msg["Subject"].startswith("Re:")
    assert msg["To"] == "p@acme.test"


def test_harden_and_perms_warning(tmp_path):
    import os

    from brokerscrub import config as c
    home = tmp_path / "data"
    (home / "outbox").mkdir(parents=True)
    (home / "config.toml").write_text("secret")
    (home / "outbox" / "a.eml").write_text("full dossier")
    os.chmod(home / "config.toml", 0o644)
    os.chmod(home / "outbox" / "a.eml", 0o644)
    assert c.perms_warning(home) is not None      # 644 config flagged
    c.harden(home)
    assert os.stat(home / "config.toml").st_mode & 0o077 == 0   # owner-only now
    assert os.stat(home / "outbox" / "a.eml").st_mode & 0o077 == 0
    assert c.perms_warning(home) is None


def test_env_secret_override(tmp_path):
    import os

    from brokerscrub import config as c
    home = tmp_path / "data"
    home.mkdir()
    (home / "config.toml").write_text(
        '[smtp]\npassword = "fromfile"\n[imap]\npassword = "fromfile"\n')
    os.environ["BROKERSCRUB_SMTP_PASSWORD"] = "fromenv"
    try:
        cfg = c.load(home)
        assert cfg.smtp.password == "fromenv"   # env wins
        assert cfg.imap.password == "fromfile"  # file used when no env
    finally:
        del os.environ["BROKERSCRUB_SMTP_PASSWORD"]


def test_classify_link():
    from brokerscrub.optout import classify_link
    assert classify_link("https://privacyportal.onetrust.com/webform/a/b") == "form"
    assert classify_link("https://docs.google.com/forms/d/e/x/viewform") == "form"
    assert classify_link("https://acme-broker.test/opt-out") == "form"
    assert classify_link("https://foo.zendesk.com/hc/requests/123") == "form"
    assert classify_link("http://optout.aboutads.info/?c=2&lang=EN") == "cookie"
    assert classify_link("https://optout.networkadvertising.org/") == "cookie"
    assert classify_link("https://acme-broker.test/privacy-policy/") == "skip"
    assert classify_link("https://cdn.example/logo.png") == "skip"
    assert classify_link("https://aka.ms/LearnAboutSenderId") == "skip"


def test_optout_field_mapping():
    from brokerscrub.optout import _mapping
    ident = Identity(full_name="Jane Q Public", emails=["jane@example.test"],
                     phones=["+1 555-0100"],
                     addresses=["123 Main St, Austin, TX 78701"])
    fills, extra = _mapping(ident)
    assert extra["first"] == "Jane" and extra["last"] == "Public"
    assert extra["state"] == "TX"
    vals = dict(fills)
    assert vals[r"first\s*name"] == "Jane"
    assert vals[r"e-?mail"] == "jane@example.test"
    assert vals[r"^city|town"] == "Austin"
    assert vals[r"zip|postal"] == "78701"
    assert "Main St" in vals[r"street|address\s*line\s*1|^address"]


def test_dsar_targets_filters(tmp_path):
    from brokerscrub.optout import dsar_targets
    store = Store(tmp_path / "t.db")
    bid, _ = store.upsert_broker(name="Acme", email="p@acme.test", domains=["acme.test"])
    req = store.create_request(bid)
    store.mark_sent(req["id"], "<m@x>")
    store.add_event(req["id"], "link_skipped",
                    "blocked at https://privacyportal.onetrust.com/webform/a/b: ...")
    store.add_event(req["id"], "link_skipped",
                    "blocked at http://optout.aboutads.info/?c=2: cookie opt-out")
    store.add_event(req["id"], "link_skipped",
                    "https://acme.test/privacy-policy/ -> HTTP 403")
    targets = dsar_targets(store)
    assert len(targets) == 1
    assert "onetrust" in targets[0]["url"]


def test_confirm_regex():
    positives = [
        "your data has been deleted",
        "Records have been removed per your request",
        "Your opt-out is complete",
        "we have successfully processed your deletion request",
        "Confirmation: Your request has been completed",
        "your request was fulfilled",
        "We have completed your request",
    ]
    negatives = [
        "we received your deletion request",
        "please verify your identity",
        "click to confirm your request",
        "your request has been received",
        "your request is being processed",
        "your ticket has been created",
    ]
    for p in positives:
        assert CONFIRM_RE.search(p), p
    for n in negatives:
        assert not CONFIRM_RE.search(n), n
