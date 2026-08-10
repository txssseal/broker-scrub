# broker-scrub

[![test](https://github.com/txssseal/broker-scrub/actions/workflows/test.yml/badge.svg)](https://github.com/txssseal/broker-scrub/actions/workflows/test.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**[brokerscrub.com](https://brokerscrub.com)** · self-hosted data-broker deletion
machine. Does what Cloaked / DeleteMe / Incogni charge a subscription for, using
the master list the brokers are legally required to publish: the
[CPPA Data Broker Registry](https://cppa.ca.gov/data_broker_registry/) — ~600
registered brokers, each with a mandatory privacy-contact email.

It sends each broker a statutory deletion demand, watches your inbox for replies,
auto-handles verification links, tracks the 45-day statutory clock, and re-runs on
a schedule because broker data decays. Everything runs on a box you control — a
native Python CLI (Docker optional); your identity and credentials never leave
your machine.

> **Setting this up with an AI assistant?** Point Claude Code / Cursor / Codex at
> **[AGENTS.md](AGENTS.md)** — it's a step-by-step runbook the agent can follow to
> stand up the whole thing safely (it will pause for your real details and your
> explicit go-ahead before anything is sent).

> **Disclaimer.** This tool submits legal privacy requests **on your own behalf,
> about your own data.** It is not legal advice. You are responsible for the
> accuracy of what you send and for complying with applicable law and the terms
> of service of any site you interact with (this especially applies to the
> optional form-filling agent). Statutory citations reflect the authors'
> reading of the TDPSA/CCPA and may be wrong or out of date — verify before you
> rely on them. Provided "as is", no warranty. See [LICENSE](LICENSE).

## The loop

1. `ingest` — pull the CPPA registry (name, contact email, website, privacy page)
2. `plan` — draft one deletion demand per broker (cites TDPSA §541.051(b)(4) +
   CCPA §1798.105; asserts authentication, bars new-account/excessive verification,
   states the 45-day clock runs from receipt)
3. `send` — dry-run writes `.eml` files to `~/.brokerscrub/outbox/` for inspection;
   `send --live` sends over SMTP, throttled with jitter and a daily cap
4. `poll` / `run` — watch IMAP for replies; auto-click **genuine** confirmation
   links only (never policy/portal/homepage links), on the broker's own domain,
   after resolving to a public IP (no DNS-rebinding); classify confirmations,
   bounces, and portal deflections
5. `followup` — fire a firm TDPSA reply at brokers that deflected to a portal or
   demanded an account (cites §541.052(e), §541.055(b), the clock, AG + penalty)
6. `status` / `overdue` — counts, plus brokers past the 45-day deadline (your
   AG-complaint candidates)
7. `recheck` — deletions decay (~90 days; brokers re-scrape public records);
   `--reopen` re-drafts the due ones
8. `mark <ref> confirmed|closed` — manually resolve a request handled out-of-band

## Install

broker-scrub is a plain Python 3.11+ CLI. It stores everything under
`~/.brokerscrub` (`chmod 600`), runs as you, needs no root and no Docker. Pick one
install path.

For Gmail (the easy mailbox): turn on 2-Step Verification and create an
[app password](https://myaccount.google.com/apppasswords) (use that, never your
account password); hosts `smtp.gmail.com:587` / `imap.gmail.com:993`.

### Install it (git clone)

```sh
git clone https://github.com/txssseal/broker-scrub.git && cd broker-scrub
pipx install .          # recommended — puts `brokerscrub` on your PATH (~/.local/bin)
# no pipx?  ->  pip install --user .    (plain `pip install .` can hit PEP 668
#               "externally-managed-environment" on modern distros — use a venv,
#               pipx, or --user so the binary lands on your PATH)
brokerscrub --version   # smoke test — confirms the command resolves before setup
```

That's it — you now have a `brokerscrub` command. (Rather not touch the server's
Python? `make binary` builds a `dist/brokerscrub` single file you `scp` and run —
it still needs Python 3.11+ on the target. Or use Docker, below.)

### Set it up

```sh
brokerscrub init                     # creates ~/.brokerscrub/{config.toml, brokerscrub.db}
$EDITOR ~/.brokerscrub/config.toml   # fill [identity], [smtp], [imap] — see config.example.toml
brokerscrub ingest                   # pull the live CPPA registry (~600 brokers)
brokerscrub plan                     # draft one demand per broker
brokerscrub preview Acxiom           # read a rendered letter first
brokerscrub send                     # DRY RUN — writes ~/.brokerscrub/outbox/*.eml, sends nothing
brokerscrub status
```

**Now open the `.eml` files in `~/.brokerscrub/outbox/` and confirm your name,
emails, and addresses are correct.** The next step is irreversible: it emails real
statutory deletion demands to brokers under your name and starts the 45-day
statutory clocks. Only run it after you've reviewed the dry-run:

```sh
brokerscrub send --live              # sends for real (throttled; 45-day clocks start)
brokerscrub run &                    # reply-watcher daemon (or use the systemd unit below)
```

Prefer secrets out of the file? Export `BROKERSCRUB_SMTP_PASSWORD` /
`BROKERSCRUB_IMAP_PASSWORD` — see [SECURITY.md](SECURITY.md). Data lives in
`~/.brokerscrub`; override with `BROKERSCRUB_HOME=/path`.

### Docker (optional — mainly for the form agent)

```sh
git clone https://github.com/txssseal/broker-scrub.git && cd broker-scrub
printf "UID=%s\nGID=%s\n" "$(id -u)" "$(id -g)" > .env   # Linux: run as your user
make build && make cli ARGS="init"   # data lives in ./data; use `make cli ARGS=…` / `make up`
```

Docker is only *required* for the optional opt-out **form agent** (it ships
Chromium). Everything else is happiest as the native executable above.

## Run it continuously

Run the reply-watcher as a systemd user service and schedule the send/recheck with
cron (unit + wrappers are in [`deploy/`](deploy/)):

```sh
# reply-watcher daemon (survives logout + reboot).
# The unit runs ~/.local/bin/brokerscrub — if `command -v brokerscrub` differs,
# edit ExecStart in the copied unit first.
mkdir -p ~/.config/systemd/user && cp deploy/brokerscrub.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now brokerscrub

# hourly send drain + monthly recheck — the deploy/*.sh wrappers add an flock
# guard so overlapping runs can't double-send:
( crontab -l 2>/dev/null; echo "0 * * * * $HOME/broker-scrub/deploy/send-daily.sh" ) | crontab -
( crontab -l 2>/dev/null; echo "0 9 1 * * $HOME/broker-scrub/deploy/recheck-monthly.sh" ) | crontab -
```

The hourly job drains up to `daily_cap` demands/day; the daemon catches replies;
the monthly job reopens decayed deletions. (Docker users: `make up` for the
daemon, and cron `make cli ARGS="send --live"` / `make cli ARGS="recheck --reopen"`
from the repo dir for the scheduled jobs.)

## Reply trust (why a spoofed email can't fake a deletion)

Broker domains come from the *public* registry, so a From-address proves nothing.
A reply only counts as **strong** (may confirm, click a link, or record a bounce)
when it threads to our Message-ID or quotes our per-request `BS-…` token — proof
the sender received our demand. A mere sender-domain match is **weak**: capped at
manual review, never auto-confirmed. Auto-clicks are limited to genuine
confirm/verify/opt-out links (not policy pages or portal landings), on the
broker's own domain, connecting to a validated public IP.

## Optional: the opt-out form agent

Many brokers reply with a link to a web form (OneTrust, Google Forms, Zendesk)
instead of honoring the email. The `optout-agent` image (Playwright + Chromium)
drives those:

```sh
docker compose --profile optout run --rm optout-agent optout --list      # what's parked
docker compose --profile optout run --rm optout-agent optout --limit 5   # dry-run fill + screenshots
```

Phase 1 (dry-run) fills each form from your identity and screenshots it, submitting
nothing. Phase 2 (autonomous submit + CAPTCHA solving) requires an Anthropic API
key and a CAPTCHA-solver key in `[agent]`, and **is genuinely ToS-gray — use at
your own risk.** Cloudflare-protected people-search sites are out of reach either
way; for those, see the manual opt-out URLs the tool records.

## Honest limitations

- **Email-only tier has a ceiling.** A large share of brokers deflect to
  identity-verification portals; the email demand alone won't delete you there —
  that's what `followup` and the form agent are for, and some still require a
  manual step.
- **CA residents:** use California's free [DROP](https://cppa.ca.gov/) (one request
  → all registered brokers) instead; this is the DIY path for everyone else.
- **Deletions decay.** New public records (a home purchase in your name) re-seed
  brokers within a quarter. `recheck` handles the registry tier; manual sites need
  redoing.
- **Not a silver bullet.** It creates a statute-cited paper trail + deadline (real
  leverage for AG complaints) and gets outright deletions from the subset that
  honor email — but it is not magic.

## Develop

```sh
make test    # unit + security + regression tests, plus a full GreenMail e2e loop
```

## Statutes cited

| What | Texas (TDPSA) | California (CCPA/CPRA) |
|---|---|---|
| Right to delete | Tex. Bus. & Com. §541.051(b)(4) | Cal. Civ. §1798.105 |
| No sale/share | §541.051(b)(5) | §1798.120 |
| 45-day response (from receipt) | §541.052(b) | §1798.130(a)(2) |
| Authenticate or specify what's needed | §541.052(e) | 11 CCR §7060 |
| No new-account requirement | §541.055(b) | 11 CCR §7026(c) |
| Appeal + AG complaint | §541.053 | — |
| Enforcement / penalty | §541.155 (AG only, ≤$7,500/violation) | §1798.155 |

Not legal advice; confirm citations against the current statutes before relying on them.
