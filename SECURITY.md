# Security & where your data lives

broker-scrub handles your PII and email credentials. Here is exactly where that
data lives, what protects it, and how to run it safely.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue. Use a
[GitHub Security Advisory](https://github.com/txssseal/broker-scrub/security/advisories/new).
Include a reproducer and your assessed severity. Since this tool handles user PII
and clicks links from untrusted email, anything that could leak a user's data or
be induced to fetch/submit on their behalf is in scope.

## Where PII and secrets live

Everything lives under your data dir — `~/.brokerscrub/` for a native install, or
`./data/` under Docker (`BROKERSCRUB_HOME=/data`). Paths below use the native
default.

| Location | Contents | Protection |
|---|---|---|
| `~/.brokerscrub/config.toml` | **Source of truth**: your identity (name, aliases, emails, phones, addresses, DOB) **and** SMTP/IMAP credentials | `chmod 600`. This is the one file you edit. |
| `~/.brokerscrub/brokerscrub.db` (+ `-wal`/`-shm`) | Broker list (public), request state, reply/event metadata. Minimal direct PII. | `chmod 600` after `harden` |
| `~/.brokerscrub/outbox/*.eml` | Dry-run rendered demands — **each is a full dossier** (name + all emails/phones/addresses/DOB) | `chmod 600`; dir `700` |
| `~/.brokerscrub/optout_shots/*.png` | Screenshots of filled opt-out forms (your PII rendered) | `chmod 600`; dir `700` |
| `~/.brokerscrub/*.log`, `removal-plan.md` | Send/poll logs; any saved recon | `chmod 600` |

There is **no `secrets.yml`** — everything is one `config.toml`. Nothing sensitive
lives in the code, the git repo, or the Docker image: the data dir lives outside
the repo (native) or is gitignored + dockerignored (Docker `./data`), and
`config.toml`/`.env` are gitignored. Verified before release.

## What protects it

- **The data dir never enters git or a built image** — it lives in `~/.brokerscrub`
  (outside any repo), and the Docker `./data` path plus `config.toml`/`.env` are
  gitignored and dockerignored.
- **It runs as you.** Native, it's your own user; under Docker the container runs
  as your host UID (`user:` from `.env`) so `./data` stays host-owned.
- **`brokerscrub harden`** locks the data dir (`700`) and every PII file (`600`)
  to owner-only. `init` runs it automatically; `status` warns if `config.toml`
  drifts to group/other-readable.

## Keep secrets out of the file (optional, recommended)

Credentials can come from the environment instead of `config.toml`, so the app
password never sits on disk:

```sh
export BROKERSCRUB_SMTP_PASSWORD='...'
export BROKERSCRUB_IMAP_PASSWORD='...'
```

(e.g. via a systemd `EnvironmentFile`, Docker secret, or your secrets manager).
Env values override the file.

## Hardening checklist

1. Run on a host with **full-disk encryption** (the data dir is plaintext at rest).
2. `brokerscrub harden` after setup (and it's safe to re-run anytime).
3. Prefer the **env-var** secrets above, or at least confirm `config.toml` is `600`.
4. Use a **dedicated mailbox** and a revocable **app password** — never your
   account password. Rotate the app password if it's ever exposed (e.g. pasted
   into a chat/ticket).
5. Keep the data dir out of git. Native `~/.brokerscrub` already is (outside the
   repo); the Docker `./data`, `config.toml`, and `.env` are gitignored — keep it
   that way.

This is a personal-use tool with no warranty (see [LICENSE](LICENSE)).
