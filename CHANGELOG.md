# Changelog

All notable changes are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

## 1.0.0 - 2026-08-10

Initial public release.

### Added
- CPPA data broker registry ingest (~600 brokers) with multi-URL and
  multi-tenant-platform handling.
- Per-broker deletion demands citing TDPSA (§§ 541.051–541.055) and CCPA
  (§ 1798.105), with name aliases and a strengthened authentication paragraph.
- Throttled SMTP sending (dry-run by default) with daily cap, jitter, and
  reconnect-and-retry.
- IMAP reply tracking via UID high-water mark (never walks the existing inbox),
  strong/weak reply authentication, confirmation/bounce/portal-deflection
  classification, and DNS-rebinding-safe verification-link visiting.
- `followup` — firm TDPSA reply to portal-deflecting brokers.
- 45-day statutory deadline tracking (`overdue`) and 90-day recheck (`recheck`).
- Optional Playwright opt-out **form agent** (`optout`) for DSAR web forms.
- Security hardening: owner-only data files (`harden`), env-var secrets, config
  permission warnings; see [SECURITY.md](SECURITY.md).
- Install via `git clone` + `pip install .` / `pipx install .`, a native
  single-file executable (`make binary`), or Docker.
- `AGENTS.md` setup runbook for AI assistants.
