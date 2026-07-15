"""C4 独立爬虫部署包与最小 Secret 验证。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pytest
from crawler import NewsArticle, NewsCrawler

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = ROOT / "deploy" / "crawler"


def test_standalone_compose_is_hardened_and_core_independent():
    compose = (DEPLOY_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    build = (DEPLOY_DIR / "docker-compose.build.yml").read_text(encoding="utf-8")

    for required in (
        "MCP_CRAWL_API_KEY",
        "read_only: true",
        "no-new-privileges:true",
        "cap_drop:",
        "tmpfs:",
        "healthcheck:",
        "restart: unless-stopped",
        "host.docker.internal:host-gateway",
    ):
        assert required in compose
    for forbidden in ("MONGODB", "REDIS", "JWT_SECRET", "DEEPSEEK_API_KEY"):
        assert forbidden not in compose
    assert "../../services/mcp_crawl" in build
    dockerignore = (ROOT / "services" / "mcp_crawl" / ".dockerignore").read_text(encoding="utf-8")
    assert ".env.*" in dockerignore
    assert "__pycache__/" in dockerignore


def test_crawler_environment_template_is_minimal_and_proxy_compatible():
    template = (DEPLOY_DIR / ".env.crawler.example").read_text(encoding="utf-8")

    assert "MCP_CRAWL_API_KEY=" in template
    assert "host.docker.internal:7890" in template
    assert "HTTP_PROXY=" in template
    assert "HTTPS_PROXY=" in template
    for forbidden in ("MONGODB", "REDIS", "JWT_SECRET", "DEEPSEEK_API_KEY"):
        assert forbidden not in template


def test_operations_entrypoints_cover_build_upgrade_and_rollback():
    shell = (DEPLOY_DIR / "manage.sh").read_text(encoding="utf-8")
    powershell = (DEPLOY_DIR / "manage.ps1").read_text(encoding="utf-8")

    for action in ("build", "up", "upgrade", "rollback", "down", "logs", "status", "config"):
        assert action in shell
        assert action in powershell
    assert "MCP_CRAWL_IMAGE_TAG" in shell
    assert "MCP_CRAWL_IMAGE_TAG" in powershell


@pytest.mark.asyncio
async def test_news_crawl_does_not_require_deepseek_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    crawler = NewsCrawler()
    article = NewsArticle(
        title="Agent security news",
        url="https://example.com/news",
        source="Example",
        published_at=datetime.now(),
    )
    monkeypatch.setattr(crawler, "_crawl_rss", lambda *_args: ([article], {"status": "ok"}))

    result = await crawler.crawl(days=1)

    assert len(result) == 1
    assert result[0].url == article.url


@pytest.mark.asyncio
async def test_fulltext_uses_configured_proxy_without_logging_secret(
    monkeypatch,
    caplog,
):
    from curl_cffi import requests

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-secret@host.docker.internal:7890")
    captured: dict = {}

    class Response:
        status_code = 200
        text = "<html><h1>Title</h1><article>" + ("security content " * 50) + "</article></html>"

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    with caplog.at_level(logging.INFO, logger="mcp-crawl.crawler"):
        content = await NewsCrawler().fetch_fulltext("https://example.com/news")

    assert content.startswith("# Title")
    assert captured["proxies"] == {
        "http": "http://proxy-user:proxy-secret@host.docker.internal:7890",
        "https": "http://proxy-user:proxy-secret@host.docker.internal:7890",
    }
    assert "proxy-secret" not in caplog.text
