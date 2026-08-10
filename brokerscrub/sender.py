"""SMTP sending with throttle, daily cap, and dry-run-by-default."""

import os
import random
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import letter
from .config import Config


def _plus_address(addr: str, token: str) -> str:
    local, _, domain = addr.partition("@")
    return f"{local}+{token.lower()}@{domain}"


def build_message(cfg: Config, broker_row, req_row) -> EmailMessage:
    token = req_row["token"]
    msg = EmailMessage()
    msg["From"] = f"{cfg.identity.full_name} <{cfg.smtp.from_addr}>"
    msg["To"] = broker_row["email"]
    msg["Subject"] = letter.subject(cfg.identity, token)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp.from_addr.partition("@")[2] or None)
    if cfg.send.plus_addressing:
        msg["Reply-To"] = _plus_address(cfg.smtp.from_addr, token)
    msg.set_content(letter.body(cfg.identity, broker_row["name"], token))
    return msg


def build_followup(cfg: Config, broker_row, req_row) -> EmailMessage:
    """A firm TDPSA reply threaded to the original demand (In-Reply-To), so it
    stays in the same conversation and our matcher re-links it."""
    token = req_row["token"]
    orig_mid = req_row["message_id"]
    msg = EmailMessage()
    msg["From"] = f"{cfg.identity.full_name} <{cfg.smtp.from_addr}>"
    msg["To"] = broker_row["email"]
    msg["Subject"] = letter.followup_subject(letter.subject(cfg.identity, token))
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp.from_addr.partition("@")[2] or None)
    if orig_mid:
        msg["In-Reply-To"] = orig_mid
        msg["References"] = orig_mid
    if cfg.send.plus_addressing:
        msg["Reply-To"] = _plus_address(cfg.smtp.from_addr, token)
    msg.set_content(letter.followup_body(cfg.identity, broker_row["name"], token))
    return msg


def send_followups(store, cfg: Config, requests_list, *, live: bool, echo=print) -> dict:
    """Send firm TDPSA follow-ups. Dry-run by default; --live threads a reply to
    each broker's original demand."""
    stats = {"sent": 0, "dry_run": 0, "errors": 0}
    interval = 3600.0 / max(cfg.send.throttle_per_hour, 1)
    server = None
    try:
        for i, req in enumerate(requests_list):
            broker = store.broker(req["broker_id"])
            msg = build_followup(cfg, broker, req)
            if not live:
                stats["dry_run"] += 1
                echo(f"[dry-run followup] {broker['name']} <{broker['email']}> ref {req['token']}")
                continue
            try:
                if server is None:
                    server = _smtp_connect(cfg)
                server.send_message(msg)
                store.add_event(req["id"], "followup_sent",
                                f"firm TDPSA reply to {broker['email']} ({msg['Message-ID']})")
                stats["sent"] += 1
                echo(f"[followup {stats['sent']}] {broker['name']} <{broker['email']}>")
            except (smtplib.SMTPException, OSError) as e:
                server = None
                store.add_event(req["id"], "error", f"followup failed: {e}")
                stats["errors"] += 1
                echo(f"[error] {broker['name']}: {e}")
            if i < len(requests_list) - 1:
                time.sleep(interval + random.uniform(0, cfg.send.jitter_seconds))
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return stats


def _smtp_connect(cfg: Config) -> smtplib.SMTP:
    if cfg.smtp.use_ssl:
        server = smtplib.SMTP_SSL(cfg.smtp.host, cfg.smtp.port, timeout=60)
    else:
        server = smtplib.SMTP(cfg.smtp.host, cfg.smtp.port, timeout=60)
        if cfg.smtp.use_tls:
            server.starttls()
    if cfg.smtp.username:
        server.login(cfg.smtp.username, cfg.smtp.password)
    return server


def send_drafts(store, cfg: Config, *, live: bool, limit: int | None,
                outbox_dir, echo=print) -> dict:
    """Send (or dry-run) all draft requests. Dry-run writes .eml files to the
    outbox and leaves statuses untouched; --live sends and starts the 45-day
    statutory clock."""
    drafts = store.requests(status="draft")
    if limit:
        drafts = drafts[:limit]
    stats = {"sent": 0, "dry_run": 0, "errors": 0, "capped": 0}
    if not drafts:
        return stats

    outbox_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(outbox_dir, 0o700)
    interval = 3600.0 / max(cfg.send.throttle_per_hour, 1)
    server = None
    try:
        for i, req in enumerate(drafts):
            # Re-read: the snapshot can be stale (a concurrent poll may have
            # advanced this request while we slept between sends).
            fresh = store.request(req["id"])
            if not fresh or fresh["status"] != "draft":
                continue
            broker = store.broker(req["broker_id"])
            msg = build_message(cfg, broker, req)
            if not live:
                path = outbox_dir / f"{req['token']}_{broker['id']}.eml"
                path.write_bytes(bytes(msg))
                os.chmod(path, 0o600)  # each .eml is a full PII dossier
                stats["dry_run"] += 1
                echo(f"[dry-run] {broker['name']} <{broker['email']}> -> {path.name}")
                continue

            if store.sent_count_last_24h() >= cfg.send.daily_cap:
                stats["capped"] = len(drafts) - i
                echo(f"daily cap of {cfg.send.daily_cap} reached; "
                     f"{stats['capped']} drafts left for next run")
                break

            sent_ok = False
            for attempt in (1, 2):  # one reconnect-and-retry (covers idle-timeout drops)
                try:
                    if server is None:
                        server = _smtp_connect(cfg)
                    server.send_message(msg)
                    sent_ok = True
                    break
                except smtplib.SMTPServerDisconnected:
                    server = None
                    if attempt == 1:
                        store.add_event(req["id"], "reconnect",
                                        "SMTP dropped mid-send; reconnecting and retrying once")
                    else:
                        store.add_event(req["id"], "error",
                                        "SMTP disconnected on retry; will retry next run")
                except (smtplib.SMTPException, OSError) as e:
                    # OSError covers gaierror / ConnectionRefused / SSL — not SMTPException subclasses
                    server = None
                    store.add_event(req["id"], "error", f"send failed: {e}")
                    echo(f"[error] {broker['name']}: {e}")
                    break

            if sent_ok:
                if store.mark_sent(req["id"], msg["Message-ID"]):
                    stats["sent"] += 1
                    echo(f"[sent {stats['sent']}] {broker['name']} <{broker['email']}> "
                         f"ref {req['token']}")
                else:
                    # delivered but another process already advanced the row —
                    # surface it rather than silently double-counting
                    store.add_event(req["id"], "warning",
                                    "message sent but request was no longer 'draft'")
            else:
                stats["errors"] += 1

            if i < len(drafts) - 1:
                time.sleep(interval + random.uniform(0, cfg.send.jitter_seconds))
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return stats
