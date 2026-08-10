See [AGENTS.md](AGENTS.md) — the full setup runbook for AI assistants (Claude
Code reads this file; the runbook lives in AGENTS.md to stay tool-neutral).

Critical: this tool sends real legal deletion demands to ~600 companies under the
human's real name. Default to dry-run. Never run `send --live`, `followup --live`,
or `optout --submit` without the human's real data in `~/.brokerscrub/config.toml`
(native) or `./data/config.toml` (Docker) AND their explicit confirmation. Never
commit `data/`, `config.toml`, or `.env`.
