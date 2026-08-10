"""Ingest the CPPA data broker registry (the legally-required master list).

Every California-registered broker must publish a privacy contact email in the
registry; that beats any vendor's hardcoded broker list and it's free. Column
names are matched by fuzzy predicates because the CPPA renames headers between
registry years.
"""

import csv
import io
import re

import requests

from .platforms import is_multitenant
from .verifier import hostname_of, registrable_domain

REGISTRY_URL = "https://cppa.ca.gov/data_broker_registry/registry.csv"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Registry cells frequently pack several URLs into one field, separated by
# semicolons/commas/whitespace ("https://a.com; https://b.com; ...").
URL_SPLIT_RE = re.compile(r"[;,\s]+")

# field -> predicate over the lowercased header cell
COLUMN_PREDICATES = {
    "name": lambda h: h.startswith("data broker name"),
    "dba": lambda h: "doing business as" in h,
    "website": lambda h: h.startswith("data broker primary website"),
    "email": lambda h: "contact email" in h,
    "phone": lambda h: "primary phone" in h,
    "street": lambda h: "street address" in h,
    "city": lambda h: h.startswith("data broker city"),
    "state": lambda h: h.startswith("data broker state"),
    "zip": lambda h: "zip" in h,
    "country": lambda h: h.startswith("data broker country"),
    "privacy_url": lambda h: "exercise" in h and "website" in h,
}


def fetch(url: str = REGISTRY_URL) -> str:
    resp = requests.get(url, timeout=60, headers={"User-Agent": "broker-scrub/1.0"})
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig", errors="replace")


def _map_columns(header: list[str]) -> dict:
    mapping = {}
    for field, pred in COLUMN_PREDICATES.items():
        for col in header:
            if pred(col.strip().lower()):
                mapping[field] = col
                break
    return mapping


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _split_urls(cell: str) -> list[str]:
    """One registry URL cell -> list of normalized URLs (handles multi-URL cells)."""
    out, seen = [], set()
    for frag in URL_SPLIT_RE.split(cell or ""):
        frag = frag.strip().strip(".,;")
        if not frag:
            continue
        u = _norm_url(frag)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _domain_for(host_or_url: str) -> str | None:
    """Allowlist entry for a candidate: exact host for multi-tenant platforms,
    registrable domain otherwise. Rejects anything with junk characters."""
    dom = registrable_domain(host_or_url)
    if not dom or any(c in dom for c in " ;,\t/\\"):
        return None
    if is_multitenant(dom):
        host = hostname_of(host_or_url)
        # a bare multi-tenant eTLD+1 with no subdomain is not broker-specific — drop it
        return host if host and host != dom else None
    return dom


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Returns (broker dicts, skipped-row descriptions)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("registry CSV has no header row")
    cols = _map_columns(list(reader.fieldnames))
    for required in ("name", "email"):
        if required not in cols:
            raise ValueError(f"registry CSV missing a recognizable '{required}' column; "
                             f"headers were: {reader.fieldnames[:6]}...")

    def get(row, field):
        return (row.get(cols[field], "") or "").strip() if field in cols else ""

    brokers, skipped = [], []
    for row in reader:
        name = get(row, "name")
        email = get(row, "email").lower()
        if not name:
            skipped.append(f"row {reader.line_num}: missing broker name (email {email!r})")
            continue
        if not EMAIL_RE.match(email):
            skipped.append(f"{name}: unusable email {email!r}")
            continue
        website_urls = _split_urls(get(row, "website"))
        privacy_urls = _split_urls(get(row, "privacy_url"))
        website = website_urls[0] if website_urls else ""
        privacy_url = privacy_urls[0] if privacy_urls else ""
        address = ", ".join(x for x in (get(row, "street"), get(row, "city"),
                                        get(row, "state"), get(row, "zip"),
                                        get(row, "country")) if x)
        domains = set()
        d = _domain_for(email.split("@", 1)[1])  # email domain drives reply-matching
        if d:
            domains.add(d)
        for candidate in (*website_urls, *privacy_urls):
            d = _domain_for(candidate)
            if d:
                domains.add(d)
        brokers.append(dict(
            name=name, dba=get(row, "dba"), email=email, website=website,
            privacy_url=privacy_url, phone=get(row, "phone"), address=address,
            domains=sorted(domains),
        ))
    return brokers, skipped


# Below this fraction of the currently-active brokers, a "full registry"
# ingest is assumed truncated/broken and deactivation is refused.
DEACTIVATE_FLOOR = 0.5


def ingest(store, text: str, source: str, *, is_full_registry: bool = True) -> dict:
    brokers, skipped = parse(text)

    # dedupe (name,email) so stats reflect distinct entries even if the CSV
    # ships duplicate rows (past CPPA registries have).
    deduped, seen = [], set()
    dupes = 0
    for b in brokers:
        key = (b["name"], b["email"])
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        deduped.append(b)

    created = updated = 0
    seen_ids = []
    for b in deduped:
        bid, was_created = store.upsert_broker(source=source, **b)
        seen_ids.append(bid)
        created += was_created
        updated += not was_created

    # Only a full-registry ingest may deactivate, and only if it isn't
    # suspiciously small — otherwise a partial/CSV ingest would silently
    # deactivate the whole registry mid-campaign.
    deactivated = 0
    warning = None
    if is_full_registry:
        active = len(store.brokers())
        if active and len(seen_ids) < active * DEACTIVATE_FLOOR:
            warning = (f"refused to deactivate: parsed {len(seen_ids)} brokers "
                       f"but {active} are active (<{int(DEACTIVATE_FLOOR*100)}%); "
                       f"looks truncated")
        else:
            deactivated = store.deactivate_brokers_not_in(seen_ids)
    return dict(total=len(deduped), created=created, updated=updated,
                skipped=skipped, deactivated=deactivated, duplicates=dupes,
                warning=warning)
