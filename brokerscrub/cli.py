"""broker-scrub CLI."""

import os
import time

import click

from . import __version__, letter, registry, sender
from . import config as cfg_mod
from .db import Store, parse_iso, utcnow

BOUNCE_COOLDOWN_DAYS = 30  # don't re-draft a bounced broker before this elapses


def _home():
    home = cfg_mod.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home


def _store(home):
    return Store(home / "brokerscrub.db")


@click.group()
@click.version_option(version=__version__, prog_name="brokerscrub")
def main():
    """Self-hosted data broker deletion machine (TDPSA + CCPA)."""


@main.command()
def init():
    """Create the data dir, database, and a config template."""
    home = _home()
    _store(home).close()
    path = cfg_mod.write_template(home)
    cfg_mod.harden(home)
    click.echo(f"data dir : {home}")
    click.echo(f"database : {home / 'brokerscrub.db'}")
    click.echo(f"config   : {path}  <- fill in [identity], [smtp], [imap]")


@main.command()
def harden():
    """Lock the data dir and every PII file (config, db, outbox, screenshots,
    logs, plan) to owner-only (chmod 600/700)."""
    home = _home()
    for action in cfg_mod.harden(home):
        click.echo(f"  {action}")
    click.echo("data dir hardened to owner-only")


@main.command()
@click.option("--url", default=registry.REGISTRY_URL, show_default=True)
@click.option("--csv", "csv_path", type=click.Path(exists=True),
              help="Ingest a local CSV instead of fetching the CPPA registry.")
def ingest(url, csv_path):
    """Load/refresh the broker list from the CPPA data broker registry."""
    home = _home()
    store = _store(home)
    if csv_path:
        text = open(csv_path, encoding="utf-8-sig").read()
        source = csv_path
    else:
        click.echo(f"fetching {url} ...")
        text = registry.fetch(url)
        source = url
    # a --csv ingest is treated as a partial add and never deactivates the
    # brokers it doesn't mention
    stats = registry.ingest(store, text, source, is_full_registry=not csv_path)
    click.echo(f"brokers: {stats['total']} in registry | "
               f"{stats['created']} new | {stats['updated']} updated | "
               f"{stats['deactivated']} no longer registered"
               + (f" | {stats['duplicates']} duplicate rows" if stats['duplicates'] else ""))
    if stats.get("warning"):
        click.echo(f"  WARNING: {stats['warning']}")
    for s in stats["skipped"]:
        click.echo(f"  skipped: {s}")
    store.close()


@main.command()
@click.option("--limit", type=int, default=None, help="Cap number of drafts created.")
@click.option("--match", default=None, help="Only brokers whose name/email matches.")
def plan(limit, match):
    """Create draft deletion requests for brokers without an open request."""
    home = _home()
    cfg = cfg_mod.load(home)
    if not cfg.identity.ready:
        raise click.ClickException(
            "identity not configured — fill in [identity] in config.toml first "
            "(the letters embed your name/emails; empty letters are useless)")
    store = _store(home)
    brokers = store.find_broker(match) if match else store.brokers()
    created = skipped = bounced_held = 0
    for b in brokers:
        if not b["active"]:
            continue
        if store.open_request_for_broker(b["id"]):
            skipped += 1
            continue
        latest = store.latest_request_for_broker(b["id"])
        if latest and latest["status"] in ("confirmed", "closed"):
            # decay is handled by `recheck --reopen`, not by plan, so plan never
            # re-drafts a done broker on its own
            skipped += 1
            continue
        if latest and latest["status"] == "bounced":
            # don't re-draft to the same dead address on every run — wait out a
            # cooldown so a persistently-bad contact surfaces instead of looping
            last = parse_iso(latest["last_event_at"])
            if last and (utcnow() - last).days < BOUNCE_COOLDOWN_DAYS:
                bounced_held += 1
                continue
        store.create_request(b["id"])
        created += 1
        if limit and created >= limit:
            break
    click.echo(f"drafts created: {created} | skipped (open/done): {skipped}"
               + (f" | bounced held (cooldown): {bounced_held}" if bounced_held else ""))
    store.close()


@main.command()
@click.argument("broker_match")
def preview(broker_match):
    """Print the deletion letter for one broker without sending."""
    home = _home()
    cfg = cfg_mod.load(home)
    store = _store(home)
    matches = store.find_broker(broker_match)
    if not matches:
        raise click.ClickException(f"no broker matches {broker_match!r}")
    b = matches[0]
    click.echo(f"To: {b['email']}")
    click.echo(f"Subject: {letter.subject(cfg.identity, 'BS-PREVIEW555')}")
    click.echo()
    click.echo(letter.body(cfg.identity, b["name"], "BS-PREVIEW555"))
    store.close()


@main.command()
@click.option("--live", is_flag=True,
              help="Actually send. Without this flag, .eml files are written to outbox/.")
@click.option("--limit", type=int, default=None)
def send(live, limit):
    """Send draft requests (dry-run by default)."""
    home = _home()
    cfg = cfg_mod.load(home)
    if live:
        if not cfg.smtp.ready:
            raise click.ClickException("[smtp] not configured in config.toml")
        if not cfg.identity.ready:
            raise click.ClickException("[identity] not configured in config.toml")
    store = _store(home)
    stats = sender.send_drafts(store, cfg, live=live, limit=limit,
                               outbox_dir=home / "outbox", echo=click.echo)
    click.echo(f"sent: {stats['sent']} | dry-run: {stats['dry_run']} | "
               f"errors: {stats['errors']} | held by daily cap: {stats['capped']}")
    store.close()


@main.command()
def poll():
    """Check IMAP once: match replies, click verification links, update statuses."""
    from . import inbox
    home = _home()
    cfg = cfg_mod.load(home)
    if not cfg.imap.ready:
        raise click.ClickException("[imap] not configured in config.toml")
    store = _store(home)
    stats = inbox.poll_once(store, cfg, echo=click.echo)
    click.echo(f"processed: {stats['processed']} | unmatched: {stats['unmatched']}")
    store.close()


@main.command()
@click.option("--interval", type=int, default=900, show_default=True,
              help="Seconds between IMAP polls.")
def run(interval):
    """Daemon loop: poll IMAP on an interval. Safe to run before config is
    filled in — it waits until [imap] is configured."""
    from . import inbox
    home = _home()
    while True:
        try:
            cfg = cfg_mod.load(home)
        except Exception as e:
            # a malformed config (mid-edit typo) must not crash-loop the daemon
            click.echo(f"config.toml unreadable/invalid: {e}; retrying in {interval}s", err=True)
            time.sleep(interval)
            continue
        if not cfg.imap.ready:
            click.echo("waiting: [imap] not configured in config.toml yet")
        else:
            store = _store(home)
            try:
                stats = inbox.poll_once(store, cfg, echo=click.echo)
                click.echo(f"poll: processed {stats['processed']}, "
                           f"unmatched {stats['unmatched']}")
            except Exception as e:
                click.echo(f"poll error (will retry): {e}", err=True)
            finally:
                store.close()
        time.sleep(interval)


@main.command()
def status():
    """Summary counts plus overdue and recheck-due brokers."""
    home = _home()
    warn = cfg_mod.perms_warning(home)
    if warn:
        click.echo(f"WARNING: {warn}\n", err=True)
    store = _store(home)
    counts = store.status_counts()
    total_brokers = len(store.brokers(active_only=False))
    active = len(store.brokers())
    click.echo(f"brokers: {active} active / {total_brokers} known")
    if counts:
        click.echo("requests: " + " | ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    else:
        click.echo("requests: none yet — run `brokerscrub plan`")
    overdue = store.overdue()
    if overdue:
        click.echo(f"\nOVERDUE ({len(overdue)}) — past the 45-day statutory deadline:")
        for r in overdue:
            days = (utcnow() - parse_iso(r["deadline_at"])).days
            click.echo(f"  {r['broker_name']} <{r['broker_email']}> "
                       f"ref {r['token']} — {days}d over")
    due = store.recheck_due()
    if due:
        click.echo(f"\nRECHECK DUE ({len(due)}) — confirmed >90d ago, deletions decay:")
        for r in due:
            click.echo(f"  {r['broker_name']} — confirmed {r['confirmed_at']}")
    bounced = store.requests(status="bounced")
    if bounced:
        click.echo(f"\nBOUNCED ({len(bounced)}) — dead contact address, needs a manual lookup:")
        for r in bounced:
            b = store.broker(r["broker_id"])
            click.echo(f"  {b['name']} <{b['email']}>")
    store.close()


@main.command()
def overdue():
    """Brokers past the statutory deadline — your AG complaint candidates."""
    home = _home()
    store = _store(home)
    rows = store.overdue()
    if not rows:
        click.echo("nothing overdue")
        return
    for r in rows:
        days = (utcnow() - parse_iso(r["deadline_at"])).days
        click.echo(f"{r['broker_name']}\t{r['broker_email']}\tref {r['token']}\t"
                   f"sent {r['sent_at']}\t{days} days past deadline")
    click.echo(f"\n{len(rows)} brokers in violation. Complaints: "
               "TX AG consumer complaints portal / CPPA cppa.ca.gov/complaints")
    store.close()


@main.command()
@click.option("--reopen", is_flag=True, help="Create fresh drafts for every due recheck.")
def recheck(reopen):
    """List confirmed deletions older than 90 days (brokers re-scrape)."""
    home = _home()
    store = _store(home)
    rows = store.recheck_due()
    if not rows:
        click.echo("no rechecks due")
        store.close()
        return
    created = skipped = 0
    for r in rows:
        click.echo(f"{r['broker_name']} — confirmed {r['confirmed_at']}, "
                   f"recheck was due {r['recheck_at']}")
        if not reopen:
            continue
        # supersede the old confirmed row so it stops reappearing here every run
        store.clear_recheck(r["id"])
        if store.open_request_for_broker(r["broker_id"]):
            skipped += 1  # already has an in-flight request; don't duplicate
            continue
        store.create_request(r["broker_id"])
        created += 1
    if reopen:
        click.echo(f"\n{created} new drafts created"
                   + (f", {skipped} skipped (already in-flight)" if skipped else "")
                   + " — run `brokerscrub send --live`")
    store.close()


@main.command()
@click.option("--submit", is_flag=True,
              help="Actually submit the forms (autonomous). Default fills + screenshots only.")
@click.option("--limit", type=int, default=None)
@click.option("--list", "list_only", is_flag=True, help="Just list the drivable DSAR form targets.")
def optout(submit, limit, list_only):
    """Drive broker DSAR web forms the daemon parked (OneTrust/Google Forms/etc.).

    Default is a dry-run: each form is loaded and filled from your identity and a
    screenshot is saved to data/optout_shots/, but nothing is submitted. --submit
    turns on autonomous submission (needs a browser image + CAPTCHA solver)."""
    from . import optout as oa
    home = _home()
    cfg = cfg_mod.load(home)
    store = _store(home)
    targets = oa.dsar_targets(store)
    click.echo(f"{len(targets)} drivable DSAR form(s) parked by the daemon")
    if list_only:
        for t in targets[:200]:
            click.echo(f"  [{t['status']}] {t['broker_name']}: {t['url'][:88]}")
        store.close()
        return
    if not cfg.identity.ready:
        raise click.ClickException("[identity] not configured")
    shots = home / "optout_shots"
    shots.mkdir(exist_ok=True)
    os.chmod(shots, 0o700)  # screenshots render the filled PII
    done = 0
    for t in targets:
        if limit and done >= limit:
            break
        shot = shots / f"{t['broker_id']}.png"
        try:
            res = oa.process_form(t["url"], cfg.identity, submit=submit,
                                  screenshot_path=str(shot), echo=click.echo)
            if shot.exists():
                os.chmod(shot, 0o600)
        except Exception as e:
            click.echo(f"  [error] {t['broker_name']}: {e}")
            continue
        done += 1
        store.add_event(t["request_id"],
                        "optout_submitted" if res["submitted"] else "optout_filled",
                        f"{t['url']} :: filled={len(res['filled'])} missed={len(res['missed'])} "
                        f"captcha={res['captcha']} :: {res['note']}")
        click.echo(f"  {t['broker_name']}: filled {len(res['filled'])} / "
                   f"missed {len(res['missed'])} | captcha={res['captcha']} — {res['note']}")
    click.echo(f"done: {done} form(s) {'submitted' if submit else 'filled (dry-run)'}")
    store.close()


@main.command()
@click.option("--live", is_flag=True, help="Actually send. Default is a dry-run.")
@click.option("--limit", type=int, default=None)
@click.option("--match", default=None, help="Only brokers whose name/email matches.")
@click.option("--all-replied", is_flag=True,
              help="Target every 'replied' request, not just portal deflections.")
def followup(live, limit, match, all_replied):
    """Send a firm TDPSA follow-up to brokers that deflected to a portal / account.

    Threads a reply to the original demand citing § 541.052(e) (authenticate or
    specify what's needed), § 541.055(b) (no new account), the 45-day clock, and
    the AG-complaint + penalty exposure."""
    home = _home()
    cfg = cfg_mod.load(home)
    if live and not cfg.smtp.ready:
        raise click.ClickException("[smtp] not configured in config.toml")
    store = _store(home)
    # only requests we actually sent (have a Message-ID to thread to)
    reqs = [r for r in store.requests(status="replied") if r["message_id"]]
    # dedup: never follow up twice on the same request (safe to re-run/resume)
    already = {row["request_id"] for row in store.conn.execute(
        "SELECT DISTINCT request_id FROM events WHERE kind='followup_sent'")}
    reqs = [r for r in reqs if r["id"] not in already]
    if not all_replied:
        deflect = []
        for r in reqs:
            evs = store.events_for(r["id"])
            if any(("portal" in e["detail"].lower() or "deflect" in e["detail"].lower())
                   for e in evs):
                deflect.append(r)
        reqs = deflect
    if match:
        reqs = [r for r in reqs
                if match.lower() in (store.broker(r["broker_id"])["name"] or "").lower()
                or match.lower() in (store.broker(r["broker_id"])["email"] or "").lower()]
    if limit:
        reqs = reqs[:limit]
    click.echo(f"{len(reqs)} follow-up target(s)"
               + ("" if all_replied else " (portal deflections)"))
    stats = sender.send_followups(store, cfg, reqs, live=live, echo=click.echo)
    click.echo(f"followups sent: {stats['sent']} | dry-run: {stats['dry_run']} | "
               f"errors: {stats['errors']}")
    store.close()


@main.command()
def reaudit():
    """Correct the 'verified' bucket: downgrade requests whose only auto-clicked
    links were policy/portal/homepage pages (not genuine confirmation actions)
    back to 'replied' (manual/agent action needed)."""
    import re as _re

    from .inbox import is_action_link
    home = _home()
    store = _store(home)
    downgraded = kept = 0
    for r in store.requests(status="verified"):
        urls = []
        for e in store.events_for(r["id"]):
            if e["kind"] == "link_visited":
                m = _re.search(r"https?://[^\s]+", e["detail"])
                if m:
                    urls.append(m.group(0))
        if urls and any(is_action_link(u) for u in urls):
            kept += 1
        else:
            store.set_status(r["id"], "replied",
                             "reaudit: only policy/portal links were clicked — "
                             "not a genuine confirmation")
            downgraded += 1
    click.echo(f"verified reaudit: kept {kept} (real confirmation click) | "
               f"downgraded {downgraded} to replied (portal/manual action needed)")
    store.close()


@main.command()
@click.argument("token")
@click.argument("new_status", type=click.Choice(["confirmed", "closed", "bounced", "replied"]))
@click.option("--note", default="manual override")
def mark(token, new_status, note):
    """Manually set a request's status by its ref token.

    Use `closed` for a request you resolved out-of-band (e.g. the broker's
    web-only flow, no email confirmation) so it stops showing as overdue and
    plan won't re-open it. `confirmed` also schedules the 90-day recheck."""
    home = _home()
    store = _store(home)
    req = store.find_request_by_token(token.strip().upper())
    if not req:
        raise click.ClickException(f"no request with token {token!r}")
    store.set_status(req["id"], new_status, note, force=True)
    click.echo(f"{token.upper()} -> {new_status}")
    store.close()


@main.command()
@click.argument("broker_match")
def history(broker_match):
    """Full event log for one broker's requests."""
    home = _home()
    store = _store(home)
    matches = store.find_broker(broker_match)
    if not matches:
        raise click.ClickException(f"no broker matches {broker_match!r}")
    b = matches[0]
    click.echo(f"{b['name']} <{b['email']}> domains={b['domains']}")
    req = store.latest_request_for_broker(b["id"])
    if not req:
        click.echo("no requests yet")
        store.close()
        return
    click.echo(f"request {req['token']} status={req['status']} "
               f"sent={req['sent_at']} deadline={req['deadline_at']}")
    for e in store.events_for(req["id"]):
        click.echo(f"  {e['ts']} {e['kind']}: {e['detail']}")
    store.close()


if __name__ == "__main__":
    main()
