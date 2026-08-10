"""URL safety validation - prevent SSRF by validating URLs before fetching."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

# Tracking parameters to strip during canonicalization
TRACKING_PARAMS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
})


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if IP is in a blocked range."""
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    # Cloud metadata addresses
    return isinstance(ip, ipaddress.IPv4Address) and str(ip).startswith("169.254.169.254")


def validate_url_protocol(url: str) -> bool:
    """Validate URL uses HTTP or HTTPS protocol only."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https")
    except Exception:
        return False


def is_safe_url(url: str) -> bool:
    """Check if URL is safe (HTTP/HTTPS, not private/loopback/link-local).

    Does NOT do DNS resolution - use for initial URL validation.
    DNS-level checks should be done before actual fetching.
    """
    if not validate_url_protocol(url):
        return False
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        # Reject URLs with credentials
        if parsed.username or parsed.password:
            return False
        # Try to parse as IP address
        try:
            ip = ipaddress.ip_address(hostname)
            if _is_blocked_ip(ip):
                return False
        except ValueError:
            pass  # It's a domain name, not an IP - OK for now
        # Reject common private hostnames
        return hostname not in ("localhost", "0.0.0.0", "::1")
    except Exception:
        return False


def canonicalize_url(url: str) -> str:
    """Canonicalize URL: remove fragment, tracking params, normalize.

    Steps:
    1. Only accept http/https
    2. Lowercase scheme and host
    3. Remove default port (:80 for http, :443 for https)
    4. Remove fragment
    5. Remove tracking parameters (utm_*, fbclid, gclid)
    6. Sort remaining query parameters
    7. Normalize empty path to /
    8. Limit to 2048 characters
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower() if parsed.hostname else ""

    # Remove default port
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    # Build netloc
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"

    # Filter and sort query params
    pairs = []
    if parsed.query:
        from urllib.parse import parse_qsl

        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            if k.lower() not in TRACKING_PARAMS:
                pairs.append((k, v))
    pairs.sort()
    query = "&".join(f"{k}={v}" for k, v in pairs) if pairs else ""

    # Normalize path
    path = parsed.path or "/"

    # Reconstruct URL
    result = f"{scheme}://{netloc}{path}"
    if query:
        result += f"?{query}"

    # Truncate to 2048 chars
    if len(result) > 2048:
        result = result[:2048]

    return result


def compute_url_hash(canonical_url: str) -> str:
    """MD5 hash of canonical URL (matches articles.url_hash format)."""
    import hashlib

    return hashlib.md5(canonical_url.encode("utf-8"), usedforsecurity=False).hexdigest()


def extract_domain(url: str) -> str:
    """Extract display domain from URL (without www. prefix)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host
