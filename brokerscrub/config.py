"""Configuration loading. All state lives under BROKERSCRUB_HOME (default ./data)."""

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def home_dir() -> Path:
    # ~/.brokerscrub by default so the single-file binary works from any cwd;
    # the Docker image overrides this with BROKERSCRUB_HOME=/data.
    return Path(os.environ.get("BROKERSCRUB_HOME", "~/.brokerscrub")).expanduser().resolve()


@dataclass
class Identity:
    full_name: str = ""
    aliases: list = field(default_factory=list)  # other names brokers may list you under
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    addresses: list = field(default_factory=list)
    state: str = "TX"
    dob: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.full_name and self.emails)


@dataclass
class Smtp:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    use_tls: bool = True    # STARTTLS
    use_ssl: bool = False   # implicit TLS (port 465)

    @property
    def ready(self) -> bool:
        return bool(self.host and self.from_addr)


@dataclass
class Imap:
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""
    folder: str = "INBOX"
    use_ssl: bool = True

    @property
    def ready(self) -> bool:
        return bool(self.host and self.username)


@dataclass
class SendCfg:
    throttle_per_hour: int = 20
    jitter_seconds: int = 30
    daily_cap: int = 100
    plus_addressing: bool = False


@dataclass
class VerifyCfg:
    timeout_seconds: int = 20
    max_redirects: int = 5
    # Test-only escape hatch: hosts here bypass the public-IP and broker-domain
    # checks. Never put a real broker host in this list.
    insecure_allow_hosts: list = field(default_factory=list)


@dataclass
class Config:
    identity: Identity = field(default_factory=Identity)
    smtp: Smtp = field(default_factory=Smtp)
    imap: Imap = field(default_factory=Imap)
    send: SendCfg = field(default_factory=SendCfg)
    verify: VerifyCfg = field(default_factory=VerifyCfg)


def _fill(dc_cls, data: dict):
    kwargs = {}
    for name in dc_cls.__dataclass_fields__:
        if name in data:
            kwargs[name] = data[name]
    return dc_cls(**kwargs)


def load(home: Path | None = None) -> Config:
    home = home or home_dir()
    path = home / "config.toml"
    if not path.exists():
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = Config(
        identity=_fill(Identity, raw.get("identity", {})),
        smtp=_fill(Smtp, raw.get("smtp", {})),
        imap=_fill(Imap, raw.get("imap", {})),
        send=_fill(SendCfg, raw.get("send", {})),
        verify=_fill(VerifyCfg, raw.get("verify", {})),
    )
    # Secrets may be supplied via env instead of the file, so the password never
    # has to sit on disk (e.g. BROKERSCRUB_SMTP_PASSWORD from a systemd
    # EnvironmentFile or a secrets manager). Env wins over the file.
    cfg.smtp.password = os.environ.get("BROKERSCRUB_SMTP_PASSWORD", cfg.smtp.password)
    cfg.imap.password = os.environ.get("BROKERSCRUB_IMAP_PASSWORD", cfg.imap.password)
    return cfg


def perms_warning(home: Path | None = None) -> str | None:
    """Return a warning if config.toml is readable by group/other (secrets leak)."""
    home = home or home_dir()
    path = home / "config.toml"
    if not path.exists():
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return (f"{path} is group/other-accessible (mode {oct(mode & 0o777)}); "
                f"it holds credentials + PII. Run `brokerscrub harden` or chmod 600.")
    return None


# Files under the data dir that contain PII and must not be world-readable.
_SENSITIVE_GLOBS = ("config.toml", "config.toml.bak", "brokerscrub.db*",
                    "removal-plan.md", "*.log")
_SENSITIVE_DIRS = ("outbox", "optout_shots")


def harden(home: Path | None = None) -> list[str]:
    """Lock the data dir and every PII artifact to owner-only. Returns actions."""
    home = home or home_dir()
    actions = []
    if home.exists():
        os.chmod(home, 0o700)
        actions.append(f"chmod 700 {home}")
    for pattern in _SENSITIVE_GLOBS:
        for p in home.glob(pattern):
            if p.is_file():
                os.chmod(p, 0o600)
    for d in _SENSITIVE_DIRS:
        dp = home / d
        if dp.is_dir():
            os.chmod(dp, 0o700)
            for p in dp.rglob("*"):
                if p.is_file():
                    os.chmod(p, 0o600)
    actions.append("chmod 600 all PII files (config, db, outbox/*.eml, screenshots, logs, plan)")
    return actions


CONFIG_TEMPLATE = """\
# broker-scrub configuration. This file contains credentials and PII — it is
# chmod 600 and lives only in the data volume, never in git.

[identity]
full_name = ""            # REQUIRED — your full legal name
aliases = []              # other names brokers may list you under, e.g. ["Jane Public", "Jane Q Public"]
emails = [""]             # REQUIRED — all emails brokers may hold on you
phones = []               # e.g. ["+1 512 555 0100"]
addresses = []            # current + previous, one string each
state = "TX"              # your state of residence
dob = ""                  # optional, "YYYY-MM-DD"; some brokers require it to match records

[smtp]
host = ""                 # e.g. "smtp.gmail.com" (use a Gmail app password)
port = 587
username = ""
password = ""
from_addr = ""            # the address the deletion demands are sent from
use_tls = true
use_ssl = false

[imap]
host = ""                 # e.g. "imap.gmail.com"
port = 993
username = ""
password = ""
folder = "INBOX"
use_ssl = true

[send]
throttle_per_hour = 20    # keep modest — burst-sending gets you spam-filtered
jitter_seconds = 30
daily_cap = 100
plus_addressing = false   # true on Gmail: Reply-To user+bs-<token>@gmail.com for exact reply matching

[verify]
timeout_seconds = 20
max_redirects = 5
"""


def write_template(home: Path) -> Path:
    path = home / "config.toml"
    if not path.exists():
        path.write_text(CONFIG_TEMPLATE)
        os.chmod(path, 0o600)
    return path
