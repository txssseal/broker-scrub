# Contributing

Thanks for helping improve broker-scrub.

## Dev setup

```sh
git clone https://github.com/txssseal/broker-scrub.git && cd broker-scrub
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Before you open a PR

```sh
ruff check .              # lint (config in pyproject.toml)
pytest -m "not network"   # unit / security / regression tests (fast, offline, no Docker)
make test                 # full suite incl. the GreenMail SMTP+IMAP e2e loop — needs Docker
```

`make test` spins up GreenMail via `docker compose`; without Docker, the e2e
tests skip and `pytest -m "not network"` covers units/security/regressions.

- Add a test for any behavior change — especially anything touching reply
  parsing, link visiting, or the state machine (those have security tests).
- **Never** put real credentials or personal identifiers in code, tests, or
  fixtures. Use fake data (`example.test`, `Jane Public`).
- Keep the reply-trust and SSRF guarantees intact (see `verifier.py` /
  `inbox.py` docstrings); don't weaken a security test to make CI green.

## Good first contributions

- Handling for a broker DSAR platform the opt-out agent doesn't yet fill.
- More confirmation/deflection language patterns (with tests).
- Non-Gmail SMTP/IMAP setup notes.

For anything large, open an issue first so we can align on approach.
Found a security issue? See [SECURITY.md](SECURITY.md) — report it privately.
