"""Verification-link handling with an SSRF-safe, DNS-rebinding-proof fetcher.

Broker replies often require clicking a confirmation link before the deletion
request is processed. We auto-visit those — but only when:

  1. the link's scheme is http/https, AND
  2. its host is allowlisted for that broker (registrable domain for normal
     brokers; EXACT host for multi-tenant platforms like onetrust.com), AND
  3. every IP the host resolves to is globally routable.

Critically, we resolve the host EXACTLY ONCE, validate those addresses, then
connect to the validated IP literal while presenting the original hostname for
the Host header and TLS SNI/cert check. requests/urllib never get a second
chance to re-resolve, so a DNS-rebinding reply (public IP on the guard lookup,
127.0.0.1 on the fetch lookup) cannot slip past. Every redirect hop repeats
the same resolve-validate-pin dance.
"""

import http.client
import ipaddress
import re
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import tldextract

from .platforms import is_multitenant

# Offline extractor: bundled public-suffix snapshot, no network fetch, no disk
# cache (cache_dir=None) so it runs cleanly as a non-root container user.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def registrable_domain(host_or_url: str) -> str | None:
    """eTLD+1 for a host/URL ('mail.foo.co.uk' -> 'foo.co.uk'); None for IPs/garbage."""
    host = hostname_of(host_or_url)
    if not host:
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    ext = _extract(host)
    reg = (ext.top_domain_under_public_suffix
           if hasattr(ext, "top_domain_under_public_suffix") else ext.registered_domain)
    if reg:
        return reg
    # Suffix not in the public-suffix snapshot (new gTLD, .test, internal):
    # fall back to the last two labels so allowlisting still works.
    labels = [l for l in host.split(".") if l]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return None


def hostname_of(host_or_url: str) -> str | None:
    """Bare lowercased hostname from a host or URL; None if unparseable."""
    if not host_or_url:
        return None
    s = host_or_url.strip()
    if "://" in s:
        s = urlparse(s).hostname or ""
    else:
        s = s.split("/", 1)[0].split(":", 1)[0]
        s = s.strip("[]")
    return s.lower() or None


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v and v.lower().startswith(("http://", "https://")):
                    self.links.append(v)


def extract_links(text_body: str, html_body: str) -> list[str]:
    links = []
    if html_body:
        p = _LinkParser()
        try:
            p.feed(html_body)
        except Exception:
            pass
        links.extend(p.links)
    if text_body:
        links.extend(URL_RE.findall(text_body))
    seen, out = set(), []
    for l in links:
        l = l.rstrip(".,;")
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def resolve_public_ips(host: str) -> list[str]:
    """All resolved addresses, but only if EVERY one is globally routable.
    Returns [] on any private/loopback/link-local/reserved address or failure
    (fail closed — an SSRF guard that returns a partial list is no guard)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    addrs = []
    for info in infos:
        a = info[4][0]
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return []
        if not ip.is_global:
            return []
        addrs.append(a)
    return addrs


def host_resolves_public(host: str) -> bool:
    return bool(resolve_public_ips(host))


def link_allowed(url: str, allowed_domains: list[str], insecure_hosts: list[str]) -> tuple[bool, str]:
    """Scheme + allowlist check only — no DNS here (visit() resolves once).

    Normal brokers are matched by registrable domain. Multi-tenant platform
    hosts (onetrust.com et al.) must match the FULL host, because the bare
    eTLD+1 is shared by every tenant."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed"
    host = parsed.hostname
    if not host:
        return False, "no host in URL"
    host = host.lower()
    if host in insecure_hosts:
        return True, "insecure_allow_hosts (test only)"
    dom = registrable_domain(host)
    if not dom:
        return False, f"host {host!r} has no registrable domain (IP literal?)"
    if is_multitenant(dom):
        if host in allowed_domains:
            return True, f"exact multi-tenant host {host!r} allowed"
        return False, (f"multi-tenant platform {dom!r}; exact host {host!r} not in "
                       f"broker allowlist (bare {dom!r} is never trusted)")
    if dom in allowed_domains or host in allowed_domains:
        return True, "allowed"
    return False, f"domain {dom!r} not in broker allowlist {allowed_domains}"


# ---- pinned single-hop fetch (no redirect following) -----------------------

def _pinned_fetch(url: str, pinned_ip: str, timeout: int) -> tuple[int, str | None]:
    """GET url but connect to pinned_ip; keep original host for Host header and
    TLS SNI/cert. Does NOT follow redirects — returns (status, location)."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    family = socket.AF_INET6 if ":" in pinned_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((pinned_ip, port))
        if parsed.scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)  # SNI + cert vs host
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request("GET", path, headers={
            "Host": parsed.netloc,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) broker-scrub/1.0",
            "Accept": "*/*",
            "Connection": "close",
        })
        resp = conn.getresponse()
        location = resp.getheader("Location")
        resp.read(65536)  # drain a bounded amount, then discard
        conn.close()
        return resp.status, location
    finally:
        try:
            sock.close()
        except Exception:
            pass


def visit(url: str, allowed_domains: list[str], *, timeout: int = 20,
          max_redirects: int = 5, insecure_hosts: list[str] | None = None) -> tuple[bool, str]:
    """Resolve-validate-pin-GET each hop. Returns (ok, detail)."""
    insecure_hosts = insecure_hosts or []
    current = url
    for _ in range(max_redirects + 1):
        ok, why = link_allowed(current, allowed_domains, insecure_hosts)
        if not ok:
            return False, f"blocked at {current}: {why}"
        host = urlparse(current).hostname
        if host and host.lower() in insecure_hosts:
            try:  # test hosts: resolve without the public-IP requirement
                pinned_ip = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)[0][4][0]
            except OSError as e:
                return False, f"resolve failed at {current}: {e}"
        else:
            ips = resolve_public_ips(host)
            if not ips:
                return False, f"host {host!r} does not resolve to public addresses"
            pinned_ip = ips[0]
        try:
            status, location = _pinned_fetch(current, pinned_ip, timeout)
        except (OSError, ssl.SSLError, http.client.HTTPException) as e:
            return False, f"request failed at {current}: {e}"
        if status in (301, 302, 303, 307, 308):
            if not location:
                return False, f"redirect without Location at {current}"
            current = urljoin(current, location)
            continue
        if 200 <= status < 300:
            return True, f"visited {current} -> HTTP {status} (pinned {pinned_ip})"
        return False, f"{current} -> HTTP {status}"
    return False, f"too many redirects (> {max_redirects})"
