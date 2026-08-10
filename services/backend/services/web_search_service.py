"""Web search session service - creation, retrieval, TTL, normalization and ownership."""

from __future__ import annotations

import hashlib
import html
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlparse

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("backend.web_search")

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid",
})

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200e\u200f\u202a-\u202e]")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class SearchSessionService:
    """Manages search sessions with user isolation and TTL."""

    def __init__(self, db: AsyncIOMotorDatabase, ttl_minutes: int = 30):
        self._db = db
        self._ttl_minutes = ttl_minutes

    def _generate_search_id(self) -> str:
        """Generate unpredictable search session ID."""
        ts = datetime.now(UTC).strftime("%Y%m%d")
        rand = secrets.token_hex(6)
        return f"srch_{ts}_{rand}"

    def _generate_result_id(self, search_id: str, canonical_url: str) -> str:
        """Generate deterministic result ID within a session."""
        h = hashlib.sha256(f"{search_id}{canonical_url}".encode()).hexdigest()[:12]
        return f"res_{h}"

    async def create_session(
        self,
        user_id: str,
        query: dict[str, Any],
        results: list[dict[str, Any]],
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a new search session. Returns the session document."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=self._ttl_minutes)
        search_id = self._generate_search_id()

        session = {
            "search_id": search_id,
            "user_id": user_id,
            "query": query,
            "results": results,
            "warnings": warnings or [],
            "result_count": len(results),
            "created_at": now,
            "expires_at": expires_at,
        }
        await self._db["search_sessions"].insert_one(session)
        session["_id"] = str(session.get("_id", ""))
        return session

    async def get_session(
        self, search_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Get a search session by ID, enforcing user ownership.

        Returns None if session doesn't exist, expired, or belongs to another user.
        """
        now = datetime.now(UTC)
        return await self._db["search_sessions"].find_one(
            {
                "search_id": search_id,
                "user_id": user_id,
                "expires_at": {"$gt": now},
            }
        )

    async def update_imported_status(
        self,
        search_id: str,
        user_id: str,
        result_id: str,
        article_url_hash: str,
    ) -> bool:
        """Mark a result as imported in the session. Returns True if updated."""
        result = await self._db["search_sessions"].update_one(
            {
                "search_id": search_id,
                "user_id": user_id,
                "results.result_id": result_id,
            },
            {
                "$set": {
                    "results.$.is_imported": True,
                    "results.$.article_url_hash": article_url_hash,
                }
            },
        )
        return result.modified_count > 0

    # ── Result Normalization ──────────────────────────────

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags, unescape entities, normalize whitespace, remove control chars."""
        if not text:
            return ""
        no_tags = _TAG_RE.sub("", text)
        unescaped = html.unescape(no_tags)
        clean = _CTRL_RE.sub("", unescaped)
        return _WS_RE.sub(" ", clean).strip()

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract display domain from URL (without www. prefix)."""
        try:
            host = urlparse(url).hostname or ""
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        """Remove fragment and tracking params, normalize scheme/host."""
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
        netloc = host
        if port is not None:
            netloc = f"{host}:{port}"
        path = parsed.path or "/"
        pairs = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        ]
        pairs.sort()
        query = "&".join(f"{k}={v}" for k, v in pairs) if pairs else ""
        result = f"{scheme}://{netloc}{path}"
        if query:
            result += f"?{query}"
        return result[:2048]

    @staticmethod
    def _compute_url_hash(canonical_url: str) -> str:
        """MD5 hash of canonical URL (matches articles.url_hash format)."""
        return hashlib.md5(canonical_url.encode("utf-8"), usedforsecurity=False).hexdigest()

    def normalize_results(
        self,
        raw_results: list[dict],
        search_id: str,
        allowed_categories: list[str],
    ) -> list[dict[str, Any]]:
        """Normalize SearXNG results: clean HTML, filter non-HTTP(S), deduplicate, truncate."""
        seen: dict[str, dict[str, Any]] = {}

        for raw in raw_results:
            title = self._strip_html(str(raw.get("title", "")))
            if not title:
                continue
            title = title[:500]

            url = str(raw.get("url", "")).strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                continue

            canonical = self._canonicalize_url(url)
            url_hash = self._compute_url_hash(canonical)

            # Deduplicate by canonical URL, merge engines
            if url_hash in seen:
                existing = seen[url_hash]
                existing_engines = set(existing.get("engines", []))
                raw_engines = raw.get("engines") or ([raw["engine"]] if raw.get("engine") else [])
                existing_engines.update(raw_engines)
                existing["engines"] = sorted(existing_engines)
                continue

            snippet = self._strip_html(str(raw.get("content", "")))[:2000]
            domain = self._extract_domain(canonical)

            published = raw.get("publishedDate") or raw.get("published_date")
            published_str = str(published) if published else None

            raw_engines = raw.get("engines") or ([raw["engine"]] if raw.get("engine") else [])
            engines = sorted({str(e) for e in raw_engines}) if raw_engines else []

            category = str(raw.get("category", "") or "general")
            if category not in allowed_categories:
                category = "general"

            score = raw.get("score")
            try:
                searxng_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                searxng_score = None

            result_id = self._generate_result_id(search_id, canonical)

            seen[url_hash] = {
                "result_id": result_id,
                "title": title,
                "url": url,
                "canonical_url": canonical,
                "canonical_url_hash": url_hash,
                "display_domain": domain,
                "snippet": snippet,
                "published_at": published_str,
                "engines": engines,
                "category": category,
                "searxng_score": searxng_score,
                "is_imported": False,
                "article_url_hash": None,
            }

        return list(seen.values())

    def build_warnings(self, raw_response: dict) -> list[dict[str, Any]]:
        """Extract warnings from SearXNG response."""
        warnings: list[dict[str, Any]] = []
        unresponsive = raw_response.get("unresponsive_engines") or []
        if unresponsive:
            count = len(unresponsive)
            warnings.append({
                "code": "ENGINE_UNAVAILABLE",
                "message": "部分搜索引擎暂时不可用，结果可能不完整",
                "count": count,
            })
        return warnings

    async def mark_imported_results(self, results: list[dict]) -> list[dict]:
        """Batch query articles collection and mark is_imported."""
        if not results:
            return results
        hashes = [r["canonical_url_hash"] for r in results if r.get("canonical_url_hash")]
        if not hashes:
            return results
        existing = await self._db["articles"].find(
            {"url_hash": {"$in": hashes}},
            {"url_hash": 1, "_id": 0},
        ).to_list(length=None)
        existing_hashes = {doc["url_hash"] for doc in existing}
        for r in results:
            if r.get("canonical_url_hash") in existing_hashes:
                r["is_imported"] = True
                r["article_url_hash"] = r["canonical_url_hash"]
        return results
