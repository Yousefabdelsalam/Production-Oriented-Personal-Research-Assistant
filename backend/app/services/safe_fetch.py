"""SSRF-resistant URL fetching.

Rules enforced here (not by the caller):
  * http/https only, no credentials in the URL, no empty host
  * hostname resolved by us; EVERY resolved address must be globally routable
    (blocks loopback, RFC1918, link-local/metadata 169.254.x, CGNAT, multicast, reserved, IPv6 ULA, v4-mapped v6 ...)
  * the connection is pinned to the validated IP (Host header + TLS SNI keep the original name),
    so a DNS answer that changes between check and connect (rebinding) cannot redirect us to an internal address
  * redirects are followed manually, max N, each hop re-validated
  * response size is capped while streaming; content-type allow-listed
  * 401/402/403 are reported as access-controlled; authentication and paywalls are never bypassed
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.errors import FetchBlocked, FetchError

ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf")
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain", ".lan", ".home", ".corp", ".intranet")
USER_AGENT = "PersonalResearchAssistant/0.1 (+local; respects access controls)"

Resolver = Callable[[str, int], list[str]]


def default_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed for {host}: {e}", code="dns_failed", retryable=True) from e
    return sorted({i[4][0] for i in infos})


@dataclass
class FetchResult:
    final_url: str
    status: int
    content_type: str
    body: bytes
    redirects: list[str]
    pinned_ip: str


def _check_ip(ip_s: str, host: str) -> None:
    ip = ipaddress.ip_address(ip_s.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise FetchBlocked(f"Blocked: {host} resolves to a non-public address ({ip}).", code="url_blocked_private")


def validate_url(url: str, resolver: Resolver = default_resolver) -> tuple[str, str, int, str]:
    """Returns (normalized_url, host, port, pinned_ip). Raises FetchBlocked / FetchError."""
    if not url or len(url) > 2000:
        raise FetchBlocked("URL is empty or too long.", code="url_invalid")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchBlocked(f"Only http/https URLs are allowed (got '{parts.scheme or 'none'}').", code="url_scheme")
    if parts.username or parts.password:
        raise FetchBlocked("URLs with embedded credentials are not allowed.", code="url_credentials")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchBlocked("URL has no host.", code="url_invalid")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or "." not in host and ":" not in host:
        raise FetchBlocked(f"Blocked: '{host}' looks like an internal host name.", code="url_blocked_private")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:  # IP literals are judged directly; they never go through (possibly injected/misbehaving) DNS
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        ips = resolver(host, port)
    if not ips:
        raise FetchError(f"No addresses found for {host}.", code="dns_failed")
    for ip in ips:
        _check_ip(ip, host)
    normalized = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))
    return normalized, host, port, ips[0]


def canonical_url(url: str) -> str:
    """Stable identity for dedupe: lowercase scheme/host, drop fragment and default port, drop trailing slash on root."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def fetch_url(
    url: str,
    *,
    timeout_s: float = 15.0,
    max_bytes: int = 5_000_000,
    max_redirects: int = 5,
    resolver: Resolver = default_resolver,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    redirects: list[str] = []
    current = url
    for hop in range(max_redirects + 1):
        normalized, host, port, ip = validate_url(current, resolver)
        parts = urlsplit(normalized)
        ip_host = f"[{ip}]" if ":" in ip else ip
        default_port = 443 if parts.scheme == "https" else 80
        netloc = ip_host if port == default_port else f"{ip_host}:{port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        host_header = host if port == default_port else f"{host}:{port}"
        headers = {"Host": host_header, "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.8,*/*;q=0.1"}
        try:
            with httpx.Client(timeout=timeout_s, follow_redirects=False, transport=transport, trust_env=False) as client:
                with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError("Redirect without Location header.", code="bad_redirect")
                        redirects.append(normalized)
                        current = urljoin(normalized, loc)
                        continue
                    if resp.status_code in (401, 402, 403, 407):
                        raise FetchError(
                            f"The page requires authentication or payment (HTTP {resp.status_code}); access controls are not bypassed.",
                            code="access_controlled",
                        )
                    if resp.status_code == 404 or resp.status_code == 410:
                        raise FetchError(f"Page not found (HTTP {resp.status_code}).", code="not_found")
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise FetchError(f"Server returned HTTP {resp.status_code}.", code="http_error", retryable=True)
                    if resp.status_code >= 400:
                        raise FetchError(f"Server returned HTTP {resp.status_code}.", code="http_error")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(ALLOWED_TYPES):
                        raise FetchError(f"Unsupported content type '{ctype}'.", code="unsupported_content_type")
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise FetchError(f"Response too large ({int(clen)} bytes).", code="response_too_large")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError(f"Response exceeded {max_bytes} bytes.", code="response_too_large")
                        chunks.append(chunk)
                    return FetchResult(normalized, resp.status_code, ctype or "text/html", b"".join(chunks), redirects, ip)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            raise FetchError(f"Network error fetching {host}: {type(e).__name__}", code="network_error", retryable=True) from e
        except httpx.HTTPError as e:
            raise FetchError(f"HTTP error fetching {host}: {type(e).__name__}", code="network_error") from e
    raise FetchError(f"Too many redirects (>{max_redirects}).", code="too_many_redirects")
