"""Autonomous DSAR opt-out form filler.

Many brokers reply with a link to a web form (OneTrust webform, a Google Form,
a Zendesk/Freshservice ticket, a broker-native /optout page) rather than
honoring the emailed demand directly. The IMAP daemon parks those links
(``link_skipped`` events). This module drives a real headless browser to fill
and submit them from the stored identity.

Phases:
  1. FILL (no key needed): load each form, map identity -> labeled fields, fill,
     screenshot, DO NOT submit. Fully testable; proves the field mapping.
  2. AUTONOMOUS (needs ANTHROPIC_API_KEY + a CAPTCHA solver key): fall back to a
     Claude agent for novel layouts, solve the CAPTCHA via the solver service,
     and submit.

Playwright is imported lazily so the main (daemon/sender) image, which does not
ship a browser, is unaffected — only the `optout-agent` image needs it.
"""

import re
from urllib.parse import urlparse

# --- which parked links are actual fillable DSAR intake forms ----------------

# structured platforms + broker-native intake paths we can drive
DSAR_HOST_HINTS = (
    "privacyportal.onetrust.com", "onetrust.com/webform",
    "docs.google.com/forms", "forms.gle",
    "submit-irm.trustarc.com", "trustarc.com",
    ".zendesk.com/hc/", "freshservice.com", "atlassian.net/servicedesk",
    "privacyportal-", "privacy-center", "dsar", "dsr",
)
DSAR_PATH_HINTS = ("optout", "opt-out", "opt_out", "removal", "remove",
                   "privacy-center", "dsar", "dsr", "ccpa", "data-request",
                   "do-not-sell", "webform", "requests")

# never-fillable: cookie opt-outs (set browser cookies, nothing to submit),
# tracking/wrapper/asset noise, and plain policy pages
SKIP_HOST = ("aboutads.info", "networkadvertising.org", "youradchoices",
             "optout.networkadvertising", "google.com/url", "safelinks.protection",
             "proofpoint.com", "aka.ms", "growthdot.com", "hubspotusercontent",
             "linkedin.com", "twitter.com", "facebook.com")
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".pdf")


def classify_link(url: str) -> str:
    """'form' (drivable DSAR form), 'cookie' (browser cookie opt-out), or 'skip'."""
    u = url.lower()
    path = urlparse(u).path or ""
    if any(h in u for h in SKIP_HOST) or u.endswith(SKIP_SUFFIX):
        # DAA/NAI cookie opt-outs are real but browser-cookie based, flag separately
        if any(h in u for h in ("aboutads.info", "networkadvertising", "youradchoices")):
            return "cookie"
        return "skip"
    if path.rstrip("/").endswith(("privacy-policy", "privacy", "terms")):
        return "skip"
    if any(h in u for h in DSAR_HOST_HINTS):
        return "form"
    if any(p in path for p in DSAR_PATH_HINTS):
        return "form"
    return "skip"


def dsar_targets(store) -> list[dict]:
    """Distinct drivable DSAR form links parked by the daemon, with their broker."""
    rows = store.conn.execute(
        "SELECT e.detail, r.id req_id, r.broker_id, r.status "
        "FROM events e JOIN requests r ON r.id = e.request_id "
        "WHERE e.kind = 'link_skipped' ORDER BY e.id").fetchall()
    seen, out = set(), []
    for row in rows:
        m = re.search(r"https?://[^\s:]+", row["detail"])
        if not m:
            continue
        url = m.group(0).rstrip(".,);")
        if classify_link(url) != "form" or url in seen:
            continue
        seen.add(url)
        broker = store.broker(row["broker_id"])
        out.append(dict(url=url, request_id=row["req_id"], broker_id=row["broker_id"],
                        broker_name=broker["name"] if broker else "?",
                        status=row["status"]))
    return out


# --- field mapping: label/aria/placeholder text -> identity value ------------

def _mapping(identity):
    """Ordered (label-regex, value) pairs. First address/phone/email is used for
    single-value fields; the letter already carries the full history."""
    addr = identity.addresses[0] if identity.addresses else ""
    # crude split of "123 Main St, Austin, TX 78701"
    street = city = state = zip_ = ""
    parts = [p.strip() for p in addr.split(",")]
    if parts:
        street = parts[0]
    if len(parts) >= 2:
        city = parts[1]
    if len(parts) >= 3:
        m = re.match(r"([A-Za-z]{2})\s*(\d{5})?", parts[2])
        if m:
            state, zip_ = m.group(1), (m.group(2) or "")
    first = identity.full_name.split()[0] if identity.full_name else ""
    last = identity.full_name.split()[-1] if identity.full_name else ""
    email = identity.emails[0] if identity.emails else ""
    phone = identity.phones[0] if identity.phones else ""
    return [
        (r"first\s*name", first),
        (r"middle\s*name", identity.full_name.split()[1] if len(identity.full_name.split()) > 2 else ""),
        (r"last\s*name|surname|family\s*name", last),
        (r"full\s*name|^name$|your\s*name", identity.full_name),
        (r"e-?mail", email),
        (r"phone|telephone|mobile", phone),
        (r"address\s*line\s*2|apt|suite|unit", ""),
        (r"street|address\s*line\s*1|^address", street),
        (r"^city|town", city),
        (r"zip|postal", zip_),
    ], dict(state=state, first=first, last=last, email=email)


# --- the driver --------------------------------------------------------------

def process_form(url: str, identity, *, submit: bool, screenshot_path=None,
                 solver=None, timeout_ms: int = 30000, echo=print) -> dict:
    """Load a DSAR form, fill it from `identity`, optionally submit.

    Returns a result dict. With submit=False (dry-run) nothing is sent — the
    form is filled and screenshotted for inspection. Playwright is imported here
    so the main image never needs a browser."""
    from playwright.sync_api import sync_playwright

    result = dict(url=url, filled=[], missed=[], captcha=False, submitted=False,
                  status="", note="")
    fills, extra = _mapping(identity)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            result["status"] = str(resp.status if resp else "?")
            page.wait_for_timeout(2500)  # let the form's JS render

            if _looks_blocked(page):
                result["note"] = "blocked by bot-protection (Cloudflare/challenge)"
                if screenshot_path:
                    page.screenshot(path=screenshot_path)
                return result

            for pattern, value in fills:
                if not value:
                    continue
                if _fill_labeled(page, pattern, value):
                    result["filled"].append(pattern)
                else:
                    result["missed"].append(pattern)

            _select_option(page, r"right|request\s*type|exercise", ("Delete", "Deletion", "Erase"))
            _select_option(page, r"country", (extra.get("state") and "United States",))
            _select_option(page, r"^state|province|region", (_STATE_NAMES.get(extra["state"], extra["state"]),))
            _check_acknowledgment(page)

            result["captcha"] = _has_captcha(page)
            if screenshot_path:
                page.screenshot(path=screenshot_path, full_page=True)

            if not submit:
                result["note"] = "dry-run: filled, not submitted"
                return result

            # --- phase 2 (autonomous) ---
            if result["captcha"]:
                if solver is None:
                    result["note"] = "CAPTCHA present, no solver configured — not submitted"
                    return result
                solved = solver(page)  # phase-2 hook
                if not solved:
                    result["note"] = "CAPTCHA solve failed — not submitted"
                    return result
            if _click_submit(page):
                page.wait_for_timeout(3000)
                result["submitted"] = True
                result["note"] = "submitted"
            else:
                result["note"] = "no submit button found"
            return result
        finally:
            ctx.close()
            browser.close()


def _fill_labeled(page, pattern, value) -> bool:
    rx = re.compile(pattern, re.I)
    for getter in (
        lambda: page.get_by_label(rx),
        lambda: page.get_by_placeholder(rx),
        lambda: page.locator(f"input[aria-label*='{pattern}' i], textarea[aria-label*='{pattern}' i]"),
    ):
        try:
            loc = getter().first
            if loc.count() and loc.is_visible():
                loc.fill(str(value), timeout=3000)
                return True
        except Exception:
            continue
    return False


def _select_option(page, label_pattern, wanted) -> bool:
    wanted = [w for w in wanted if w]
    if not wanted:
        return False
    rx = re.compile(label_pattern, re.I)
    try:
        loc = page.get_by_label(rx).first
        if loc.count():
            for w in wanted:
                try:
                    loc.select_option(label=w, timeout=2000)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    # listbox/option pattern (OneTrust uses role=option)
    for w in wanted:
        try:
            opt = page.get_by_role("option", name=re.compile(w, re.I)).first
            if opt.count() and opt.is_visible():
                opt.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _check_acknowledgment(page):
    try:
        for cb in page.get_by_role("checkbox").all():
            if cb.is_visible() and not cb.is_checked():
                cb.check(timeout=2000)
    except Exception:
        pass


def _has_captcha(page) -> bool:
    html = ""
    try:
        html = page.content().lower()
    except Exception:
        return False
    return any(s in html for s in ("captcha", "recaptcha", "hcaptcha", "turnstile"))


def _looks_blocked(page) -> bool:
    try:
        t = (page.title() or "").lower()
    except Exception:
        return False
    return any(s in t for s in ("just a moment", "attention required", "access denied"))


def _click_submit(page) -> bool:
    for name in (r"submit", r"send\s*request", r"begin", r"continue", r"opt\s*out"):
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I)).first
            if btn.count() and btn.is_enabled():
                btn.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


_STATE_NAMES = {"TX": "Texas", "CA": "California", "NY": "New York", "NJ": "New Jersey",
                "FL": "Florida", "IL": "Illinois"}
