"""IMAP polling: match broker replies to requests, classify, act.

Reply authentication (trust levels)
-----------------------------------
Inbound email is trivially spoofable, and broker domains come from the PUBLIC
CPPA registry, so a From-domain match proves nothing. We grade each match:

  strong  In-Reply-To/References hits our stored Message-ID, OR our per-request
          token (BS-XXXXXXXXXX) appears in the mail, OR a DSN embeds our original
          Message-ID/token. These require the sender to have received our demand.
  weak    only the sender's registrable domain matches the broker allowlist.

Only STRONG matches may confirm a deletion, click a verification link, or record
a bounce. WEAK matches are capped at 'replied' (manual review) — a spoofed email
can never silently mark data deleted or make us fetch its link.

Polling (UID high-water mark, not \\Seen)
----------------------------------------
We track last processed UID + UIDVALIDITY per folder and fetch with BODY.PEEK[]
so a message the user reads on their phone isn't skipped and a crash mid-process
doesn't lose a reply. Processing failures are recorded per-UID and retried next
poll, up to a cap, then dead-lettered.
"""

import email
import email.policy
import imaplib
import re

from .config import Config
from .db import TERMINAL_STATUSES
from .verifier import extract_links, registrable_domain, visit

# case-insensitive: plus-addressing lowercases the token in Reply-To/To
TOKEN_RE = re.compile(r"\bBS-[A-HJ-NP-Z2-9]{10}\b", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")

CONFIRM_PATTERNS = [
    r"(?:has|have)\s+been\s+(?:deleted|removed|erased)",
    r"(?:deletion|removal|opt[- ]?out)\s+(?:request\s+)?(?:is|has been|was)?\s*"
    r"(?:complete|completed|processed|fulfilled|successful)",
    r"successfully\s+(?:deleted|removed|processed|opted)",
    r"we\s+have\s+(?:deleted|removed)\s+your",
    r"your\s+(?:data|information|record|profile)\s+(?:has|have|was|were)\s+been\s+"
    r"(?:deleted|removed|suppressed)",
    # generic "your request has been completed/fulfilled/processed" (past tense only,
    # so an acknowledgment like "request has been received/submitted" won't match)
    r"your\s+request\s+(?:has\s+been|have\s+been|was)\s+(?:completed|fulfilled|processed)",
    r"we\s+have\s+(?:completed|fulfilled|processed)\s+your\s+(?:request|deletion|removal)",
]
CONFIRM_RE = re.compile("|".join(CONFIRM_PATTERNS), re.IGNORECASE)

BOUNCE_FROM_RE = re.compile(r"mailer-daemon|postmaster", re.IGNORECASE)

# A link is only worth auto-clicking (and only then counts as "verified") if it
# looks like a genuine confirm/verify/opt-out ACTION — not a privacy-policy page,
# a portal landing, a homepage, or an email-signature link, all of which sit on
# the broker's own domain and would otherwise be clicked and miscounted as done.
ACTION_LINK_RE = re.compile(
    r"confirm|verif|validat|/complete|token=|unsubscrib|opt-?out|/dsar|/rtbf|"
    r"erase|/delete|delete-?request|request-?status|suppress", re.I)
NOISE_LINK_RE = re.compile(
    r"privacy-?policy|privacy-?statement|privacy-?rights|privacy-?choices|"
    r"privacy-?preferences|/help|utm_source=signature|/about|/contact|/terms|"
    r"policy|/#", re.I)

# Broker is punting to a self-service / ID-verification portal — the emailed
# demand will NOT be honored directly; a human or the form agent must finish it.
DEFLECT_RE = re.compile(
    r"verify your identity|privacy choices|self-service|"
    r"visit (?:our|the)[^.]{0,25}portal|through our[^.]{0,25}portal|"
    r"submit (?:a|your) request (?:through|via|at|using)|"
    r"complete[^.]{0,25}(?:web ?form|portal)|use our[^.]{0,25}portal", re.I)


def is_action_link(url: str) -> bool:
    return bool(ACTION_LINK_RE.search(url)) and not NOISE_LINK_RE.search(url)


def _sanitize(s: str) -> str:
    """Strip terminal control/escape sequences from attacker-controlled header
    text before it's echoed to a console or written to logs."""
    return CONTROL_RE.sub("", s or "")


def _decode_part(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError):
        # unknown/broken charset — best-effort so token/confirmation scanning still runs
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        for enc in (part.get_content_charset() or "utf-8", "latin-1"):
            try:
                return payload.decode(enc, errors="replace")
            except LookupError:
                continue
        return payload.decode("utf-8", errors="replace")


def _bodies(msg) -> tuple[str, str]:
    text, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.is_multipart():
            continue
        sub = part.get_content_subtype()
        if sub == "plain":
            text += _decode_part(part)
        elif sub == "html":
            html += _decode_part(part)
    return text, html


def _embedded_bounce_refs(msg) -> tuple[set, set]:
    """Message-IDs and tokens embedded in a DSN's returned-original parts
    (message/rfc822, text/rfc822-headers) — how we match header-only bounces."""
    msgids, tokens = set(), set()
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("message/rfc822", "text/rfc822-headers"):
            continue
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            embedded = payload[0]
        else:
            try:
                embedded = email.message_from_string(
                    part.get_payload(decode=True).decode("utf-8", "replace"),
                    policy=email.policy.default)
            except Exception:
                continue
        mid = embedded.get("Message-ID", "")
        msgids.update(MSGID_RE.findall(mid))
        blob = " ".join(f"{k}: {v}" for k, v in embedded.items())
        tokens.update(m.group(0).upper() for m in TOKEN_RE.finditer(blob))
    return msgids, tokens


def _match_request(store, msg, text: str, html: str):
    """Returns (request_row | None, how: str, trust: 'strong'|'weak'|'none')."""
    # 1. In-Reply-To / References against our stored Message-IDs (strong)
    refs = " ".join(filter(None, (msg.get("In-Reply-To", ""), msg.get("References", ""))))
    for mid in MSGID_RE.findall(refs):
        req = store.find_request_by_message_id(mid)
        if req:
            return req, f"message-id {mid}", "strong"

    # 2. our token anywhere in headers/body (strong)
    haystack = " ".join((msg.get("Subject", ""), msg.get("To", ""),
                         msg.get("Delivered-To", ""), text, html))
    m = TOKEN_RE.search(haystack)
    if m:
        token = m.group(0).upper()
        req = store.find_request_by_token(token)
        if req:
            return req, f"token {token}", "strong"

    # 3. DSN with the original message returned as embedded headers (strong)
    emids, etokens = _embedded_bounce_refs(msg)
    for mid in emids:
        req = store.find_request_by_message_id(mid)
        if req:
            return req, f"embedded message-id {mid}", "strong"
    for token in etokens:
        req = store.find_request_by_token(token)
        if req:
            return req, f"embedded token {token}", "strong"

    # 4. sender's registrable domain against the broker allowlist (WEAK, spoofable)
    sender = email.utils.parseaddr(msg.get("From", ""))[1]
    dom = registrable_domain(sender.partition("@")[2])
    if dom:
        req = store.find_open_request_by_domain(dom)
        if req:
            return req, f"sender domain {dom}", "weak"
    return None, "no match", "none"


def _imap_connect(cfg: Config) -> imaplib.IMAP4:
    cls = imaplib.IMAP4_SSL if cfg.imap.use_ssl else imaplib.IMAP4
    conn = cls(cfg.imap.host, cfg.imap.port)
    conn.login(cfg.imap.username, cfg.imap.password)
    return conn


def process_message(store, cfg: Config, raw_bytes: bytes, echo=print) -> str:
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    text, html = _bodies(msg)
    req, how, trust = _match_request(store, msg, text, html)
    sender = _sanitize(msg.get("From", "?"))
    subj = _sanitize(msg.get("Subject", "") or "")[:120]
    if req is None:
        return f"unmatched: from {sender} subj {subj!r}"

    broker = store.broker(req["broker_id"])
    # Terminal requests are done — a later follow-up/survey/spoof must not reopen them.
    if req["status"] in TERMINAL_STATUSES:
        store.add_event(req["id"], "post_terminal_reply",
                        f"ignored {req['status']} follow-up from {sender} ({how})")
        return f"ignored ({req['status']} already): {broker['name']}"

    store.add_event(req["id"], "reply_received",
                    f"from {sender} ({how}/{trust}) subj {subj!r}")

    is_bounce = BOUNCE_FROM_RE.search(sender) or msg.get_content_type() == "multipart/report"
    if is_bounce and trust == "strong":
        store.set_status(req["id"], "bounced", f"bounce from {sender} ({how})")
        return f"bounced: {broker['name']}"

    # WEAK (domain-only) matches are spoofable — never auto-confirm or click.
    if trust != "strong":
        store.set_status(req["id"], "replied",
                         f"weak domain-only match from {sender} — manual review")
        return f"replied (weak match, manual review): {broker['name']}"

    if CONFIRM_RE.search(text) or CONFIRM_RE.search(re.sub(r"<[^>]+>", " ", html)):
        store.set_status(req["id"], "confirmed", f"confirmation from {sender}")
        return f"confirmed: {broker['name']} (recheck in 90d)"

    deflected = bool(DEFLECT_RE.search(text) or DEFLECT_RE.search(re.sub(r"<[^>]+>", " ", html)))
    links = extract_links(text, html)
    action_links = [l for l in links if is_action_link(l)]
    other_links = [l for l in links if l not in action_links]

    # Only click genuine confirm/verify action links, and only if the broker isn't
    # explicitly deflecting to a portal. Everything else (policy pages, portal
    # landings, homepages) is recorded for the form agent, never counted as "done".
    visited = 0
    if action_links and not deflected:
        allowed_domains = store.broker_domains(broker)
        for url in action_links:
            ok, detail = visit(
                url, allowed_domains,
                timeout=cfg.verify.timeout_seconds,
                max_redirects=cfg.verify.max_redirects,
                insecure_hosts=cfg.verify.insecure_allow_hosts)
            store.add_event(req["id"], "link_visited" if ok else "link_skipped", detail)
            visited += 1 if ok else 0
    for url in (links if deflected else other_links):
        store.add_event(req["id"], "link_skipped", f"portal/policy link (needs manual/agent): {url}")

    if visited:
        store.set_status(req["id"], "verified",
                         f"{visited} genuine confirmation link(s) visited")
        return f"verified: {broker['name']} ({visited} confirmation link(s) clicked)"
    if deflected or links:
        store.set_status(req["id"], "replied",
                         "broker deflected to a self-service/ID-verification portal — "
                         "needs manual or form-agent action")
        return f"action needed (portal deflection): {broker['name']}"
    store.set_status(req["id"], "replied", "reply without links or confirmation language")
    return f"replied (manual review): {broker['name']}"


def _folder_uidvalidity(conn, folder: str) -> int | None:
    typ, data = conn.status(folder, "(UIDVALIDITY)")
    if typ != "OK" or not data or not data[0]:
        return None
    m = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
    return int(m.group(1)) if m else None


def poll_once(store, cfg: Config, echo=print) -> dict:
    conn = _imap_connect(cfg)
    folder = cfg.imap.folder
    stats = {"processed": 0, "unmatched": 0, "failed": 0, "dead": 0}
    try:
        uidvalidity = _folder_uidvalidity(conn, folder)
        conn.select(folder)
        stored_validity, last_uid = store.imap_state(folder)
        if uidvalidity is not None and stored_validity is None:
            # First run: baseline to the CURRENT highest UID so we only ever
            # process mail that arrives AFTER setup (broker replies to demands
            # we send). Never walk the user's pre-existing inbox, which may hold
            # thousands of unrelated messages. This is why a baseline `poll`
            # must run before the first `send --live` (it does in the docs flow).
            typ, data = conn.uid("SEARCH", None, "ALL")
            existing = [int(x) for x in (data[0].split() if typ == "OK" and data and data[0] else [])]
            last_uid = max(existing) if existing else 0
            store.set_imap_state(folder, uidvalidity, last_uid)
            echo(f"  baselined {folder} at UID {last_uid} "
                 f"({len(existing)} pre-existing message(s) skipped)")
        elif uidvalidity is not None and stored_validity != uidvalidity:
            echo(f"  UIDVALIDITY changed ({stored_validity}->{uidvalidity}); rebaselining")
            store.reset_imap_state(folder, uidvalidity)
            last_uid = 0

        typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        # 'n:*' returns the highest UID even when none exceed n — filter strictly.
        found = [int(x) for x in (data[0].split() if data and data[0] else [])]
        new_uids = sorted(u for u in found if u > last_uid)
        retry_uids = [u for u in store.pending_failed_uids(folder) if u <= last_uid]
        for uid in sorted(set(retry_uids) | set(new_uids)):
            typ, fetched = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                # a message that still exists but wouldn't fetch: record it so
                # the advancing high-water mark doesn't silently drop it
                store.record_uid_failure(folder, uid, f"FETCH returned {typ}")
                stats["failed"] += 1
                continue
            raw = fetched[0][1]
            try:
                outcome = process_message(store, cfg, raw, echo=echo)
                store.clear_uid_failure(folder, uid)
                stats["processed"] += 1
                if outcome.startswith("unmatched"):
                    stats["unmatched"] += 1
                echo(f"  [uid {uid}] {outcome}")
            except Exception as e:
                dead = store.record_uid_failure(folder, uid, str(e))
                stats["failed"] += 1
                store.add_event(None, "error", f"uid {uid} processing failed: {e}")
                if dead:
                    stats["dead"] += 1
                    echo(f"  [uid {uid}] DEAD-LETTERED after repeated failures: {e}")
                else:
                    echo(f"  [uid {uid}] processing failed (will retry): {e}")

        if new_uids and uidvalidity is not None:
            store.set_imap_state(folder, uidvalidity, max(last_uid, max(new_uids)))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return stats
