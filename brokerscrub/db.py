"""SQLite store: brokers, deletion requests, event log.

Request lifecycle:
    draft -> sent -> (replied) -> verified -> confirmed
                  -> bounced
Overdue and recheck-due are computed from timestamps, not stored as states,
so a late confirmation never has to fight a stale status.
"""

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

RESPONSE_DAYS = 45   # CCPA §1798.130(a)(2) / TDPSA §541.055(a)
RECHECK_DAYS = 90    # brokers re-ingest public records; deletions decay

OPEN_STATUSES = ("draft", "sent", "replied", "verified")
# A genuine broker reply can only arrive for a request we actually SENT, so
# domain-matching an inbound email must never touch a never-sent draft.
MATCHABLE_STATUSES = ("sent", "replied", "verified")
# Terminal: process_message ignores follow-ups to these, and set_status refuses
# to downgrade out of a "done" state. 'closed' = manually resolved (e.g. a
# web-only deletion with no email confirmation).
TERMINAL_STATUSES = ("confirmed", "bounced", "closed")
DONE_STATUSES = ("confirmed", "closed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS brokers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dba TEXT DEFAULT '',
    email TEXT NOT NULL,
    website TEXT DEFAULT '',
    privacy_url TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    domains TEXT NOT NULL DEFAULT '[]',
    source TEXT DEFAULT '',
    first_seen_at TEXT,
    last_seen_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, email)
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    broker_id INTEGER NOT NULL REFERENCES brokers(id),
    status TEXT NOT NULL DEFAULT 'draft',
    token TEXT NOT NULL UNIQUE,
    message_id TEXT,
    sent_at TEXT,
    deadline_at TEXT,
    confirmed_at TEXT,
    recheck_at TEXT,
    last_event_at TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    request_id INTEGER REFERENCES requests(id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS imap_state (
    folder TEXT PRIMARY KEY,
    uidvalidity INTEGER,
    last_uid INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS imap_failed (
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT DEFAULT '',
    dead INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder, uid)
);

CREATE INDEX IF NOT EXISTS idx_requests_broker ON requests(broker_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if s else None


def new_token() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
    return "BS-" + "".join(secrets.choice(alphabet) for _ in range(10))


class Store:
    def __init__(self, path: Path | str):
        # timeout => busy_timeout: wait up to 30s for a lock instead of erroring
        # (the daemon poll, the daily sender, and manual CLI runs all share this
        # file). WAL lets a reader and a writer proceed concurrently.
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---- brokers -------------------------------------------------------

    def upsert_broker(self, *, name, email, dba="", website="", privacy_url="",
                      phone="", address="", domains=(), source="") -> tuple[int, bool]:
        """Returns (broker_id, created)."""
        now = iso(utcnow())
        cur = self.conn.execute(
            "SELECT id FROM brokers WHERE name = ? AND email = ?", (name, email))
        row = cur.fetchone()
        if row:
            self.conn.execute(
                """UPDATE brokers SET dba=?, website=?, privacy_url=?, phone=?,
                   address=?, domains=?, source=?, last_seen_at=?, active=1 WHERE id=?""",
                (dba, website, privacy_url, phone, address,
                 json.dumps(sorted(set(domains))), source, now, row["id"]))
            self.conn.commit()
            return row["id"], False
        cur = self.conn.execute(
            """INSERT INTO brokers (name, dba, email, website, privacy_url, phone,
               address, domains, source, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name, dba, email, website, privacy_url, phone, address,
             json.dumps(sorted(set(domains))), source, now, now))
        self.conn.commit()
        return cur.lastrowid, True

    def deactivate_brokers_not_in(self, ids) -> int:
        """Deactivate active brokers whose id is not in this ingest's set.
        Set-membership (not a timestamp compare) so a re-ingest within the same
        wall-clock second still works. Empty set deactivates nothing."""
        ids = list(ids)
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"UPDATE brokers SET active=0 WHERE active=1 AND id NOT IN ({placeholders})",
            tuple(ids))
        self.conn.commit()
        return cur.rowcount

    def brokers(self, active_only=True):
        q = "SELECT * FROM brokers" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return self.conn.execute(q).fetchall()

    def broker(self, broker_id: int):
        return self.conn.execute("SELECT * FROM brokers WHERE id=?", (broker_id,)).fetchone()

    def find_broker(self, needle: str):
        return self.conn.execute(
            "SELECT * FROM brokers WHERE name LIKE ? OR email LIKE ? ORDER BY name",
            (f"%{needle}%", f"%{needle}%")).fetchall()

    def broker_domains(self, broker_row) -> list[str]:
        return json.loads(broker_row["domains"] or "[]")

    # ---- requests ------------------------------------------------------

    def open_request_for_broker(self, broker_id: int):
        q = ("SELECT * FROM requests WHERE broker_id=? AND status IN (%s) "
             "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(OPEN_STATUSES)))
        return self.conn.execute(q, (broker_id, *OPEN_STATUSES)).fetchone()

    def latest_request_for_broker(self, broker_id: int):
        return self.conn.execute(
            "SELECT * FROM requests WHERE broker_id=? ORDER BY id DESC LIMIT 1",
            (broker_id,)).fetchone()

    def create_request(self, broker_id: int) -> sqlite3.Row:
        token = new_token()
        now = iso(utcnow())
        cur = self.conn.execute(
            "INSERT INTO requests (broker_id, status, token, last_event_at) VALUES (?,?,?,?)",
            (broker_id, "draft", token, now))
        self.conn.commit()
        req = self.request(cur.lastrowid)
        self.add_event(req["id"], "created", f"draft for broker {broker_id}")
        return req

    def request(self, request_id: int):
        return self.conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def requests(self, status: str | None = None):
        if status:
            return self.conn.execute(
                "SELECT * FROM requests WHERE status=? ORDER BY id", (status,)).fetchall()
        return self.conn.execute("SELECT * FROM requests ORDER BY id").fetchall()

    def mark_sent(self, request_id: int, message_id: str) -> bool:
        """Transition draft->sent atomically. The `AND status='draft'` guard
        means a request another process already advanced is not clobbered back
        to 'sent' (and its 45-day clock not restarted). Returns True if sent."""
        now = utcnow()
        deadline = now + timedelta(days=RESPONSE_DAYS)
        cur = self.conn.execute(
            """UPDATE requests SET status='sent', message_id=?, sent_at=?,
               deadline_at=?, last_event_at=? WHERE id=? AND status='draft'""",
            (message_id, iso(now), iso(deadline), iso(now), request_id))
        self.conn.commit()
        if cur.rowcount:
            self.add_event(request_id, "sent", message_id)
            return True
        return False

    def set_status(self, request_id: int, status: str, note: str = "", *,
                   force: bool = False) -> bool:
        """Apply a status transition. A 'confirmed' request is terminal: a later
        inbound email (survey, follow-up, spoof) must not downgrade it back to
        verified/replied/bounced and lose its 90-day recheck. Returns True if
        applied."""
        cur = self.request(request_id)
        if (cur and cur["status"] in DONE_STATUSES and status not in DONE_STATUSES
                and not force):
            self.add_event(request_id, "ignored_transition",
                           f"refused {cur['status']}->{status}: {note}")
            return False
        now = utcnow()
        if status == "confirmed":
            self.conn.execute(
                """UPDATE requests SET status=?, confirmed_at=?, recheck_at=?,
                   last_event_at=? WHERE id=?""",
                (status, iso(now), iso(now + timedelta(days=RECHECK_DAYS)),
                 iso(now), request_id))
        else:
            self.conn.execute(
                "UPDATE requests SET status=?, last_event_at=? WHERE id=?",
                (status, iso(now), request_id))
        self.conn.commit()
        self.add_event(request_id, status, note)
        return True

    def clear_recheck(self, request_id: int):
        """Mark a confirmed request as superseded after --reopen so it stops
        reappearing in recheck_due() every run."""
        self.conn.execute(
            "UPDATE requests SET recheck_at=NULL WHERE id=?", (request_id,))
        self.conn.commit()

    def find_request_by_message_id(self, message_id: str):
        if not message_id:
            return None
        return self.conn.execute(
            "SELECT * FROM requests WHERE message_id=?", (message_id,)).fetchone()

    def find_request_by_token(self, token: str):
        return self.conn.execute(
            "SELECT * FROM requests WHERE token=?", (token,)).fetchone()

    def find_open_request_by_domain(self, domain: str):
        """Match a reply to a request by the sender's registrable domain.

        Uses json_each for an EXACT array-membership test — the old LIKE
        '%"domain"%' let a forged sender like 'x@%.com' inject wildcards and
        match unrelated brokers. Excludes drafts (a reply can't precede a send)."""
        if not domain:
            return None
        rows = self.conn.execute(
            "SELECT r.* FROM requests r JOIN brokers b ON b.id = r.broker_id "
            "WHERE r.status IN (%s) AND EXISTS "
            "(SELECT 1 FROM json_each(b.domains) je WHERE je.value = ?) "
            "ORDER BY r.id DESC" % ",".join("?" * len(MATCHABLE_STATUSES)),
            (*MATCHABLE_STATUSES, domain)).fetchall()
        return rows[0] if rows else None

    # ---- reporting -----------------------------------------------------

    def status_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM requests GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def overdue(self):
        """Sent but unresolved requests past their statutory deadline."""
        now = iso(utcnow())
        return self.conn.execute(
            """SELECT r.*, b.name broker_name, b.email broker_email
               FROM requests r JOIN brokers b ON b.id = r.broker_id
               WHERE r.status IN ('sent','replied','verified')
                 AND r.deadline_at IS NOT NULL AND r.deadline_at < ?
               ORDER BY r.deadline_at""", (now,)).fetchall()

    def recheck_due(self):
        now = iso(utcnow())
        return self.conn.execute(
            """SELECT r.*, b.name broker_name, b.email broker_email
               FROM requests r JOIN brokers b ON b.id = r.broker_id
               WHERE r.status='confirmed' AND r.recheck_at IS NOT NULL AND r.recheck_at < ?
               ORDER BY r.recheck_at""", (now,)).fetchall()

    # ---- events --------------------------------------------------------

    def add_event(self, request_id: int | None, kind: str, detail: str = ""):
        self.conn.execute(
            "INSERT INTO events (request_id, ts, kind, detail) VALUES (?,?,?,?)",
            (request_id, iso(utcnow()), kind, detail[:2000]))
        self.conn.commit()

    def events_for(self, request_id: int):
        return self.conn.execute(
            "SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()

    # ---- IMAP UID tracking ---------------------------------------------

    def imap_state(self, folder: str):
        row = self.conn.execute(
            "SELECT uidvalidity, last_uid FROM imap_state WHERE folder=?",
            (folder,)).fetchone()
        return (row["uidvalidity"], row["last_uid"]) if row else (None, 0)

    def set_imap_state(self, folder: str, uidvalidity: int, last_uid: int):
        self.conn.execute(
            "INSERT INTO imap_state (folder, uidvalidity, last_uid) VALUES (?,?,?) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_uid=excluded.last_uid",
            (folder, uidvalidity, last_uid))
        self.conn.commit()

    def reset_imap_state(self, folder: str, uidvalidity: int):
        """UIDVALIDITY changed — the server renumbered UIDs; start over."""
        self.conn.execute("DELETE FROM imap_failed WHERE folder=?", (folder,))
        self.conn.execute(
            "INSERT INTO imap_state (folder, uidvalidity, last_uid) VALUES (?,?,0) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
            "last_uid=0", (folder, uidvalidity))
        self.conn.commit()

    def record_uid_failure(self, folder: str, uid: int, error: str,
                           max_attempts: int = 5) -> bool:
        """Track a UID that failed processing so it's retried next poll. Returns
        True once it has exhausted retries and is dead-lettered."""
        self.conn.execute(
            "INSERT INTO imap_failed (folder, uid, attempts, last_error) VALUES (?,?,1,?) "
            "ON CONFLICT(folder, uid) DO UPDATE SET attempts=attempts+1, last_error=excluded.last_error",
            (folder, uid, error[:500]))
        row = self.conn.execute(
            "SELECT attempts FROM imap_failed WHERE folder=? AND uid=?",
            (folder, uid)).fetchone()
        dead = row["attempts"] >= max_attempts
        if dead:
            self.conn.execute(
                "UPDATE imap_failed SET dead=1 WHERE folder=? AND uid=?", (folder, uid))
        self.conn.commit()
        return dead

    def clear_uid_failure(self, folder: str, uid: int):
        self.conn.execute(
            "DELETE FROM imap_failed WHERE folder=? AND uid=?", (folder, uid))
        self.conn.commit()

    def pending_failed_uids(self, folder: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT uid FROM imap_failed WHERE folder=? AND dead=0 ORDER BY uid",
            (folder,)).fetchall()
        return [r["uid"] for r in rows]

    def sent_count_last_24h(self) -> int:
        cutoff = iso(utcnow() - timedelta(hours=24))
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind='sent' AND ts > ?", (cutoff,)).fetchone()
        return row["n"]
