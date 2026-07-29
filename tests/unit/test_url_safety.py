"""Unit tests for URL safety validation module."""

from __future__ import annotations

import hashlib

import pytest
from utils.url_safety import (
    canonicalize_url,
    compute_url_hash,
    extract_domain,
    is_safe_url,
    validate_url_protocol,
)


class TestValidateUrlProtocol:
    """validate_url_protocol tests."""

    @pytest.mark.parametrize("url", ["http://example.com", "https://example.com"])
    def test_accepts_http_https(self, url):
        assert validate_url_protocol(url) is True

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,<script>", "ftp://example.com", ""],
    )
    def test_rejects_other_protocols(self, url):
        assert validate_url_protocol(url) is False


class TestIsSafeUrl:
    """is_safe_url tests."""

    @pytest.mark.parametrize(
        "url",
        ["http://example.com", "https://example.com/path", "https://example.com:8080/path?q=1"],
    )
    def test_accepts_safe_urls(self, url):
        assert is_safe_url(url) is True

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,<script>"],
    )
    def test_rejects_non_http_protocols(self, url):
        assert is_safe_url(url) is False

    def test_rejects_localhost(self):
        assert is_safe_url("http://localhost/path") is False

    def test_rejects_127_0_0_1(self):
        assert is_safe_url("http://127.0.0.1/path") is False

    def test_rejects_10_x(self):
        assert is_safe_url("http://10.0.0.1/path") is False

    def test_rejects_192_168_x(self):
        assert is_safe_url("http://192.168.1.1/path") is False

    def test_rejects_172_16_x(self):
        assert is_safe_url("http://172.16.0.1/path") is False

    def test_rejects_169_254_169_254(self):
        assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_rejects_ipv6_loopback(self):
        assert is_safe_url("http://[::1]/path") is False

    def test_rejects_urls_with_credentials(self):
        assert is_safe_url("http://user:password@example.com/path") is False

    def test_rejects_empty_hostname(self):
        assert is_safe_url("http:///path") is False


class TestCanonicalizeUrl:
    """canonicalize_url tests."""

    def test_removes_fragment(self):
        url = "https://example.com/path#section"
        assert canonicalize_url(url) == "https://example.com/path"

    def test_removes_utm_params(self):
        url = "https://example.com/path?utm_source=foo&utm_medium=bar&keep=this"
        result = canonicalize_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "keep=this" in result

    def test_removes_fbclid_gclid(self):
        url = "https://example.com/path?fbclid=abc&gclid=def&keep=this"
        result = canonicalize_url(url)
        assert "fbclid" not in result
        assert "gclid" not in result
        assert "keep=this" in result

    def test_keeps_other_params(self):
        url = "https://example.com/path?id=123&lang=en"
        result = canonicalize_url(url)
        assert "id=123" in result
        assert "lang=en" in result

    def test_sorts_params(self):
        url = "https://example.com/path?b=2&a=1&c=3"
        result = canonicalize_url(url)
        assert result == "https://example.com/path?a=1&b=2&c=3"

    def test_lowercases_host(self):
        url = "https://EXAMPLE.COM/Path"
        result = canonicalize_url(url)
        assert result.startswith("https://example.com/Path")

    def test_removes_default_port_http(self):
        url = "http://example.com:80/path"
        result = canonicalize_url(url)
        assert result == "http://example.com/path"

    def test_removes_default_port_https(self):
        url = "https://example.com:443/path"
        result = canonicalize_url(url)
        assert result == "https://example.com/path"

    def test_normalizes_empty_path(self):
        url = "https://example.com"
        result = canonicalize_url(url)
        assert result == "https://example.com/"

    def test_rejects_invalid_scheme(self):
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            canonicalize_url("ftp://example.com/path")


class TestComputeUrlHash:
    """compute_url_hash tests."""

    def test_returns_32_char_md5_hex(self):
        result = compute_url_hash("https://example.com/path")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_matches_md5(self):
        url = "https://example.com/path"
        expected = hashlib.md5(url.encode("utf-8")).hexdigest()
        assert compute_url_hash(url) == expected

    def test_deterministic(self):
        url = "https://example.com/path"
        assert compute_url_hash(url) == compute_url_hash(url)


class TestExtractDomain:
    """extract_domain tests."""

    def test_extracts_domain(self):
        assert extract_domain("https://example.com/path") == "example.com"

    def test_removes_www_prefix(self):
        assert extract_domain("https://www.example.com/path") == "example.com"

    def test_handles_http(self):
        assert extract_domain("http://example.com") == "example.com"

    def test_handles_empty_url(self):
        assert extract_domain("") == ""

    def test_preserves_subdomain(self):
        assert extract_domain("https://blog.example.com/path") == "blog.example.com"
