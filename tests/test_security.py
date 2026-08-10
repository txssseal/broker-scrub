"""Regression tests for the confirmed security findings. Each is written to
FAIL against the pre-fix code and pass after."""

import socket

import pytest

from brokerscrub import verifier
from brokerscrub.platforms import is_multitenant

# ---- #1 SSRF via DNS-rebinding TOCTOU --------------------------------------

def test_visit_resolves_once_and_pins(monkeypatch):
    """The host is resolved exactly once and we connect to THAT address — a
    second (rebound) lookup can never influence the fetch."""
    calls = {"resolve": 0}
    PUBLIC = "93.184.216.34"

    def fake_getaddrinfo(host, *a, **k):
        calls["resolve"] += 1
        # public on the first lookup, private on any subsequent one
        ip = PUBLIC if calls["resolve"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    connected = {}

    def fake_pinned_fetch(url, pinned_ip, timeout):
        connected["ip"] = pinned_ip
        return 200, None

    monkeypatch.setattr(verifier.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(verifier, "_pinned_fetch", fake_pinned_fetch)

    ok, detail = verifier.visit("https://verify.acme-broker.test/x", ["acme-broker.test"])
    assert ok, detail
    assert connected["ip"] == PUBLIC, "must connect to the validated (first) IP"
    assert calls["resolve"] == 1, "must resolve exactly once — no rebinding window"


def test_visit_blocks_private_resolution(monkeypatch):
    """A host that resolves to a private/internal IP is refused and never fetched."""
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", 0))]

    fetched = {"n": 0}

    def fake_pinned_fetch(url, pinned_ip, timeout):
        fetched["n"] += 1
        return 200, None

    monkeypatch.setattr(verifier.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(verifier, "_pinned_fetch", fake_pinned_fetch)

    ok, detail = verifier.visit("https://verify.acme-broker.test/x", ["acme-broker.test"])
    assert not ok
    assert "public" in detail
    assert fetched["n"] == 0, "must not fetch when resolution is non-public"


def test_visit_revalidates_each_redirect_hop(monkeypatch):
    """A 302 to an off-allowlist / private host is blocked at the next hop."""
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))]

    def fake_pinned_fetch(url, pinned_ip, timeout):
        if "acme-broker.test" in url:
            return 302, "http://10.0.0.1/internal"   # redirect to internal
        raise AssertionError(f"should never fetch {url}")

    monkeypatch.setattr(verifier.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(verifier, "_pinned_fetch", fake_pinned_fetch)

    ok, detail = verifier.visit("https://verify.acme-broker.test/x", ["acme-broker.test"])
    assert not ok and "blocked" in detail


# ---- #15 multi-tenant platform hosts require exact match -------------------

def test_multitenant_requires_exact_host():
    assert is_multitenant("onetrust.com")
    # bare eTLD+1 in the allowlist must NOT trust an arbitrary tenant subdomain
    ok, why = verifier.link_allowed(
        "https://evil-tenant.onetrust.com/webform", ["onetrust.com"], [])
    assert not ok and "multi-tenant" in why
    # the broker's exact webform host IS trusted
    ok, why = verifier.link_allowed(
        "https://privacyportal.onetrust.com/webform/abc",
        ["privacyportal.onetrust.com"], [])
    assert ok, why
    # a different tenant host is still refused even with an exact entry present
    ok, why = verifier.link_allowed(
        "https://attacker.onetrust.com/x", ["privacyportal.onetrust.com"], [])
    assert not ok


def test_normal_broker_domain_still_matches_subdomains():
    ok, why = verifier.link_allowed(
        "https://links.acme-broker.test/confirm", ["acme-broker.test"], [])
    assert ok, why


# ---- real HTTPS pin path (TLS SNI + cert against host, connect to pinned IP) --

@pytest.mark.network
def test_https_pinned_fetch_against_real_host():
    """Exercises the one path unit tests monkeypatch away: wrap_socket with
    server_hostname + manual conn.sock over real TLS. Skips if offline."""
    try:
        ok, detail = verifier.visit("https://example.com/", ["example.com"], timeout=15)
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"network unavailable: {e}")
    if not ok and ("does not resolve" in detail or "request failed" in detail):
        pytest.skip(f"network unavailable: {detail}")
    assert ok, detail
    assert "pinned" in detail
