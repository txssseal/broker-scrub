# AGENTS.md — setup runbook for AI coding assistants

You are an AI assistant helping a human self-host **broker-scrub** to delete their
personal data from data brokers. This file is your runbook. It's written for
Claude Code, Cursor, Codex, Aider, and similar tools. Follow it top to bottom.
Human-facing overview is in [README.md](README.md).

---

## 0. Hard rules — read before doing anything

1. **This tool sends real legal demands to ~600 real companies under the human's
   real name, and starts statutory deadlines.** That is irreversible and
   outward-facing.
2. **NEVER run `send --live`, `followup --live`, or `optout --submit` until BOTH:**
   (a) the human's real identity is in their config (`~/.brokerscrub/config.toml`
   native, or `./data/config.toml` under Docker), and (b) the human has explicitly
   confirmed "yes, send." Default to dry-run for everything and show the output first.
3. **Do NOT invent the human's PII.** Names, emails, phones, addresses, DOB must
   come from the human. Ask for them; do not guess or scrape them.
4. **Never commit secrets.** Native config lives in `~/.brokerscrub` (outside any
   repo); `data/`, `config.toml`, and `.env` are gitignored. `config.toml` holds
   credentials + PII — keep it out of git.
5. **If the human is a California resident, stop and tell them** to use the free
   state program [DROP](https://cppa.ca.gov/) (one request → all registered
   brokers) instead of this DIY tool.
6. Treat statutory citations as the authors' reading, not verified legal advice.
   Don't tell the human it's guaranteed to work.

---

Commands below use the native `brokerscrub <cmd>` form. If the human chose Docker
instead, the same command is `make cli ARGS="<cmd>"` and data lives in `./data`.

## 1. Prerequisites (check, install if missing)

- **Python 3.11+** on the host. broker-scrub is a plain CLI — no Docker needed for
  the core tool. (Docker is only for the optional opt-out form agent, §6.)
- **A dedicated mailbox with SMTP + IMAP.** Gmail works well: the human must
  enable 2-Step Verification and create an **app password**
  (myaccount.google.com/apppasswords). A dedicated address keeps broker replies
  out of their main inbox. Do NOT use the human's account password.
- A Linux host is ideal for the always-on daemon/cron (home server, VPS).

## 2. First-run setup (safe — nothing is sent)

Install:

```sh
git clone https://github.com/txssseal/broker-scrub.git && cd broker-scrub
pip install .              # or: pipx install .   (isolated)
brokerscrub init           # creates ~/.brokerscrub/{config.toml, brokerscrub.db} (chmod 600)
```

(No Python on the target? `make binary` builds a single `dist/brokerscrub` to scp
and run. Prefer containers? `docker compose` — see README Docker option.)

Now collect the human's details and write them into `~/.brokerscrub/config.toml`
(schema in `config.example.toml`). **Ask the human for:**

- `full_name` (legal name) and `aliases` (other names brokers list them under —
  maiden name, middle-name-vs-first-name variants; this matters a lot)
- `emails` — every address a broker might hold (personal, work, old ones)
- `phones` — current and past
- `addresses` — current and **every former address** (the strongest match key)
- `dob` (optional) and `state` (residence)
- SMTP/IMAP host/port/username/app-password and the `from_addr`

Do NOT include SSNs, driver's-license, or financial account numbers — brokers
don't need them and it would broadcast them.

## 3. Draft and dry-run (safe — nothing is sent)

```sh
brokerscrub ingest            # pull ~600 brokers from the live CPPA registry (HTTPS)
brokerscrub plan              # draft one demand per broker
brokerscrub preview Acxiom    # show the human a rendered letter
brokerscrub send              # DRY RUN — writes ~/.brokerscrub/outbox/*.eml, sends nothing
brokerscrub poll              # first IMAP poll: logs in and baselines the inbox
                              #   read-only, so only mail arriving AFTER now is processed
```

`poll` confirms the IMAP credentials work; SMTP is only exercised by a real
`send --live`, so it can't be fully dry-tested. Show the human the
`~/.brokerscrub/outbox/*.eml` files and confirm every identifier — a typo goes to
~600 companies. (Docker: prefix each command with `make cli ARGS="…"`; files are
in `./data/outbox`.)

## 4. Go live — ONLY after explicit human confirmation

```sh
brokerscrub send --live       # send, throttled by [send] throttle_per_hour/daily_cap
brokerscrub run &             # reply-watcher daemon (or install deploy/brokerscrub.service)
```

Automate the drain + recheck with cron (flock-guarded wrappers in `deploy/`):

```sh
( crontab -l 2>/dev/null; echo "0 * * * * $HOME/broker-scrub/deploy/send-daily.sh" ) | crontab -
( crontab -l 2>/dev/null; echo "0 9 1 * * $HOME/broker-scrub/deploy/recheck-monthly.sh" ) | crontab -
```

(Docker: `make up` for the daemon; cron `make cli ARGS="send --live"` /
`make cli ARGS="recheck --reopen"` from the repo dir.)

## 5. Operate

```sh
brokerscrub status                  # counts + overdue + bounced
brokerscrub overdue                 # past the 45-day deadline → AG-complaint candidates
brokerscrub poll                    # one manual IMAP check (the daemon does this every 15m)
brokerscrub history Acme            # full event log for one broker
brokerscrub followup                # DRY RUN: firm TDPSA reply to portal-deflectors
brokerscrub followup --live         # send them (human-confirmed)
brokerscrub recheck --reopen        # re-draft deletions older than 90 days
brokerscrub mark BS-XXXXXXXXXX closed   # resolve one handled out-of-band
```

## 6. Optional: the opt-out form agent

Some brokers deflect to a web form (OneTrust/Google Forms/Zendesk). The
`optout-agent` image (Playwright + Chromium) drives them:

```sh
docker compose --profile optout run --rm optout-agent optout --list
docker compose --profile optout run --rm optout-agent optout --limit 5   # dry-run fill + screenshots
```

This agent runs in Docker against `./data`. If the human installed natively (data
in `~/.brokerscrub`), point the compose `optout-agent` volume at `~/.brokerscrub`
(or set `BROKERSCRUB_HOME`) so it sees the parked DSAR links; otherwise it reads an
empty DB. Screenshots land in that data dir's `optout_shots/`.

Phase 2 (autonomous submit + CAPTCHA solving) needs `[agent] anthropic_api_key`
and `captcha_solver` in config, is **ToS-gray, and must be human-approved.**
Cloudflare-walled people-search sites can't be automated — surface their opt-out
URLs for the human to do manually.

## 7. Verify your work

```sh
pytest -m "not network"    # unit / security / regression tests (offline, no Docker)
make test                  # + full GreenMail e2e loop — dev-only, needs Docker
```

## 8. Troubleshooting (real gotchas)

- **Config seems ignored / permission errors** → native: the config must be at
  `~/.brokerscrub/config.toml`; run `brokerscrub harden` to fix perms and
  `brokerscrub status` (it warns on drift). Daemon health: `systemctl --user status
  brokerscrub` / `journalctl --user -u brokerscrub`. *(Docker only: `Permission
  denied: /data/config.toml` means the container ran as the wrong UID — ensure
  `.env` has host UID/GID and use the `make` targets, which export it.)*
- **Daemon "verified" ≠ deleted.** "verified" means a genuine confirmation link
  was clicked; many brokers deflect to portals (status stays "replied"). Use
  `followup` for those; `reaudit` corrects any historical over-counting.
- **Opt-out agent image build fails** on `playwright install` → the base must be
  Debian bookworm (Python 3.12); newer Debian breaks Playwright's dep install.
- **Bounces** (`status` → BOUNCED) are dead broker addresses; find an alternate
  contact or use the broker's web opt-out.

## 9. What "done" looks like

Config filled with real data; tests green; `send --live` run with human consent;
the `brokerscrub run` daemon (or the systemd unit) running; crons installed;
`status` shows demands delivered and confirmations arriving over the following
weeks. Remind the human:
deletions decay, portal-deflectors need `followup`/manual action, and new public
records re-seed brokers.
