"""Full-loop integration test against GreenMail (real SMTP + IMAP).

Proves the entire machine: ingest -> plan -> send (SMTP) -> broker inbox ->
simulated broker reply -> IMAP poll -> verification link auto-visited ->
confirmation email -> confirmed with 90-day recheck scheduled.

If GREENMAIL_HOST is set (docker compose / server), GreenMail is required and
we wait for it. If unset (bare local dev), we try localhost and skip when
absent.
"""

import email
import email.policy
import http.server
import imaplib
import os
import re
import smtplib
import socket
import threading
import time
from email.message import EmailMessage
from pathlib import Path

import pytest
from click.testing import CliRunner

from brokerscrub import cli
from brokerscrub.db import Store

FIXTURE = Path(__file__).parent / "fixtures" / "registry_sample.csv"

GM_HOST = os.environ.get("GREENMAIL_HOST")
SMTP_PORT = int(os.environ.get("GREENMAIL_SMTP_PORT", "3025"))
IMAP_PORT = int(os.environ.get("GREENMAIL_IMAP_PORT", "3143"))

ME = "testuser@example.test"
BROKER = "privacy@acme-broker.test"


def _port_open(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _greenmail_host():
    if GM_HOST:  # explicit -> required; wait for container startup
        deadline = time.time() + 60
        while time.time() < deadline:
            if _port_open(GM_HOST, SMTP_PORT):
                return GM_HOST
            time.sleep(1)
        pytest.fail(f"GREENMAIL_HOST={GM_HOST} set but :{SMTP_PORT} never opened")
    if _port_open("localhost", SMTP_PORT):
        return "localhost"
    pytest.skip("GreenMail not running (set GREENMAIL_HOST or start it on :3025)")


class _Hits(http.server.BaseHTTPRequestHandler):
    paths = []

    def do_GET(self):
        _Hits.paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"verified ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def verify_server():
    _Hits.paths = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hits)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def _write_config(home: Path, gm: str, user: str = ME):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(f"""
[identity]
full_name = "Test User"
emails = ["{user}"]
state = "TX"

[smtp]
host = "{gm}"
port = {SMTP_PORT}
from_addr = "{user}"
use_tls = false
use_ssl = false

[imap]
host = "{gm}"
port = {IMAP_PORT}
username = "{user}"
password = "anything"
use_ssl = false

[send]
throttle_per_hour = 360000
jitter_seconds = 0
daily_cap = 100
plus_addressing = false

[verify]
timeout_seconds = 10
max_redirects = 3
insecure_allow_hosts = ["127.0.0.1", "localhost"]
""")


def _fetch_inbox(gm: str, user: str) -> list:
    conn = imaplib.IMAP4(gm, IMAP_PORT)
    conn.login(user, "anything")
    conn.select("INBOX")
    _, data = conn.search(None, "ALL")
    msgs = []
    for mid in (data[0].split() if data and data[0] else []):
        _, fetched = conn.fetch(mid, "(BODY.PEEK[])")
        if fetched and isinstance(fetched[0], tuple):
            msgs.append(email.message_from_bytes(
                fetched[0][1], policy=email.policy.default))
    conn.logout()
    return msgs


def _wait_for(fn, timeout=15, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for {what}")


def _send_raw(gm: str, msg: EmailMessage):
    with smtplib.SMTP(gm, SMTP_PORT, timeout=30) as s:
        s.send_message(msg)


def _mark_all_seen(gm: str, user: str):
    """Simulate the user reading mail on their phone before the daemon polls."""
    conn = imaplib.IMAP4(gm, IMAP_PORT)
    conn.login(user, "anything")
    conn.select("INBOX")
    _, data = conn.search(None, "ALL")
    for mid in (data[0].split() if data and data[0] else []):
        conn.store(mid, "+FLAGS", "\\Seen")
    conn.logout()


def test_full_loop(tmp_path, monkeypatch, verify_server):
    gm = _greenmail_host()
    home = tmp_path / "data"
    monkeypatch.setenv("BROKERSCRUB_HOME", str(home))
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli.main, list(args), catch_exceptions=False)
        assert result.exit_code == 0, f"{args}: {result.output}"
        return result.output

    # init creates db; our config overwrites the template
    run("init")
    _write_config(home, gm)

    out = run("ingest", "--csv", str(FIXTURE))
    assert "2 new" in out

    out = run("plan")
    assert "drafts created: 2" in out

    # dry-run first: writes .eml, sends nothing, statuses stay draft
    out = run("send")
    assert "dry-run: 2" in out
    assert len(list((home / "outbox").glob("*.eml"))) == 2
    store = Store(home / "brokerscrub.db")
    assert store.status_counts() == {"draft": 2}
    store.close()

    # live send over real SMTP
    out = run("send", "--live")
    assert "sent: 2" in out

    # the demand actually landed in the broker's inbox
    broker_msgs = _wait_for(lambda: _fetch_inbox(gm, BROKER),
                            what="deletion demand in broker inbox")
    demand = broker_msgs[0]
    # name is intentionally NOT in the subject; it belongs in the body
    assert "Test User" not in demand["Subject"]
    token = re.search(r"BS-[A-Z0-9]{10}", demand["Subject"]).group(0)
    orig_mid = demand["Message-ID"]
    body = demand.get_content()
    assert "Test User" in body
    assert "1798.105" in body and "541.051(b)(4)" in body

    # baseline poll BEFORE any reply arrives (mirrors the real flow: the docs
    # tell you to run a baseline poll before send --live). Our inbox is empty.
    out = run("poll")
    assert "baselined" in out

    # broker replies asking to verify via link -> poll auto-visits it
    reply = EmailMessage()
    reply["From"] = f"Acme Privacy <{BROKER}>"
    reply["To"] = ME
    reply["Subject"] = f"Re: {demand['Subject']}"
    reply["In-Reply-To"] = orig_mid
    reply.set_content(
        "To proceed, please confirm your request by clicking:\n"
        f"http://127.0.0.1:{verify_server}/verify/{token}\n")
    _send_raw(gm, reply)
    _wait_for(lambda: _fetch_inbox(gm, ME), what="reply in our inbox")
    _mark_all_seen(gm, ME)  # already-read mail must STILL be processed (UID, not \Seen)

    out = run("poll")
    assert "verified: Acme Data LLC" in out
    assert f"/verify/{token}" in _Hits.paths, "verification link was not visited"

    store = Store(home / "brokerscrub.db")
    req = store.find_request_by_token(token)
    assert req["status"] == "verified"
    store.close()

    # broker confirms completion -> confirmed + recheck scheduled
    confirm = EmailMessage()
    confirm["From"] = f"Acme Privacy <{BROKER}>"
    confirm["To"] = ME
    confirm["Subject"] = "Your request is complete"
    confirm["In-Reply-To"] = orig_mid
    confirm.set_content("Your personal information has been deleted from our systems.")
    _send_raw(gm, confirm)
    _wait_for(lambda: len(_fetch_inbox(gm, ME)) >= 2, what="confirmation in inbox")

    out = run("poll")
    assert "confirmed: Acme Data LLC" in out

    store = Store(home / "brokerscrub.db")
    req = store.find_request_by_token(token)
    assert req["status"] == "confirmed"
    assert req["recheck_at"] is not None
    store.close()

    out = run("status")
    assert "confirmed: 1" in out and "sent: 1" in out

    out = run("history", "Acme")
    assert "link_visited" in out and "confirmed" in out


def test_first_poll_baselines_existing_inbox(tmp_path, monkeypatch):
    """The first poll must NOT walk a pre-existing inbox — it baselines to the
    current newest UID and only processes mail that arrives afterward."""
    gm = _greenmail_host()
    home = tmp_path / "data"
    monkeypatch.setenv("BROKERSCRUB_HOME", str(home))
    user = "baseline-user@example.test"
    _write_config(home, gm, user=user)

    from brokerscrub import config as cfgmod
    from brokerscrub import inbox
    from brokerscrub.db import Store

    # seed the inbox with pre-existing mail BEFORE first poll
    for n in range(3):
        m = EmailMessage()
        m["From"] = f"someone{n}@unrelated.test"
        m["To"] = user
        m["Subject"] = f"pre-existing mail {n}"
        m.set_content("old mail the tool must ignore")
        _send_raw(gm, m)
    _wait_for(lambda: len(_fetch_inbox(gm, user)) >= 3, what="pre-existing mail")

    cfg = cfgmod.load(home)
    store = Store(home / "brokerscrub.db")

    # first poll: baselines, processes NOTHING despite 3 messages present
    stats = inbox.poll_once(store, cfg, echo=lambda *_: None)
    assert stats["processed"] == 0, f"first poll should skip existing inbox, got {stats}"

    # a message arriving AFTER the baseline IS processed
    later = EmailMessage()
    later["From"] = "new@unrelated.test"
    later["To"] = user
    later["Subject"] = "arrived after baseline"
    later.set_content("should be seen")
    _send_raw(gm, later)
    _wait_for(lambda: len(_fetch_inbox(gm, user)) >= 4, what="post-baseline mail")

    stats = inbox.poll_once(store, cfg, echo=lambda *_: None)
    assert stats["processed"] == 1, f"post-baseline mail should be processed, got {stats}"
    store.close()
