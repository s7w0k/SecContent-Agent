"""
海外安全新闻爬虫 — 从 site_crawl 提取并解耦数据库依赖。

混合策略:
  - The Hacker News: curl_cffi 直接 HTML 解析（国内可达）
  - BleepingComputer / SecurityWeek / Help Net Security: Tavily API site: 搜索

使用方式:
    crawler = NewsCrawler(tavily_api_key="tvly-xxx")
    articles = await crawler.crawl(days=1)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("mcp-crawl.crawler")


# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass
class NewsArticle:
    """标准化新闻文章数据结构"""

    title: str
    url: str
    source: str  # "The Hacker News" | "BleepingComputer" | ...
    source_type: str = "overseas_news"
    summary: str = ""
    published_at: Optional[datetime] = None
    content_md: str = ""

    @property
    def url_hash(self) -> str:
        return hashlib.md5(self.url.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "source_type": self.source_type,
            "summary": self.summary,
            "published_at": self.published_at.strftime("%Y-%m-%d") if self.published_at else "",
            "content_md": self.content_md,
            "url_hash": self.url_hash,
        }


# ═══════════════════════════════════════════════════════════
# 爬虫
# ═══════════════════════════════════════════════════════════

class NewsCrawler:
    """
    海外安全新闻混合爬虫。

    站点配置:
      - The Hacker News: curl_cffi 浏览器指纹模拟
      - 其余 3 站: Tavily API site:domain 搜索
    """

    SITES = {
        "The Hacker News": {
            "domain": "thehackernews.com",
            "method": "curl_cffi",
            "homepage": "https://thehackernews.com/",
        },
        "BleepingComputer": {
            "domain": "bleepingcomputer.com",
            "method": "tavily",
            "homepage": "https://www.bleepingcomputer.com/",
        },
        "SecurityWeek": {
            "domain": "securityweek.com",
            "method": "tavily",
            "homepage": "https://www.securityweek.com/",
        },
        "Help Net Security": {
            "domain": "helpnetsecurity.com",
            "method": "tavily",
            "homepage": "https://www.helpnetsecurity.com/",
        },
    }

    def __init__(self, tavily_api_key: str):
        if not tavily_api_key:
            raise ValueError("TAVILY_API_KEY is required")
        try:
            from tavily import TavilyClient

            self.tavily = TavilyClient(api_key=tavily_api_key)
        except ImportError:
            raise ImportError("tavily-python is required. pip install tavily-python")

    async def crawl(self, days: int = 1) -> list[NewsArticle]:
        """爬取所有站点最近 N 天的文章（同步阻塞方法用 asyncio.to_thread 包装时调用者负责）"""
        cutoff = datetime.now() - timedelta(days=days)
        all_articles: list[NewsArticle] = []

        for site_name, cfg in self.SITES.items():
            logger.info("Crawling: %s (%s)", site_name, cfg["method"])
            try:
                if cfg["method"] == "curl_cffi":
                    articles = self._crawl_curl_cffi(site_name, cfg)
                else:
                    articles = self._crawl_tavily(site_name, cfg, days)

                logger.info("  %s: %d articles", site_name, len(articles))
                all_articles.extend(articles)
            except Exception as e:
                logger.error("  %s: %s", site_name, e)

        # URL 去重 + 时间过滤
        seen: set[str] = set()
        filtered: list[NewsArticle] = []
        for art in all_articles:
            key = art.url_hash
            if key in seen:
                continue
            seen.add(key)
            if art.published_at and art.published_at < cutoff:
                continue
            filtered.append(art)

        filtered.sort(key=lambda a: a.published_at or datetime.min, reverse=True)
        logger.info("Total after dedup + date filter (%dd): %d", days, len(filtered))
        return filtered

    async def fetch_fulltext(self, url: str) -> str:
        """抓取单篇文章全文并转为 Markdown。"""
        try:
            from curl_cffi import requests
            from bs4 import BeautifulSoup
        except ImportError:
            return ""

        try:
            resp = requests.get(url, impersonate="chrome124", timeout=15)
            if resp.status_code != 200:
                return ""
        except Exception:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除无用标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # 定位正文容器
        article = soup.select_one("article") or soup.select_one(
            "div.article-content, div.post-content, div.entry-content, div.body-post"
        )

        if not article:
            # 回退到 body
            article = soup.body or soup

        # 提取标题
        title_el = soup.select_one("h1") or soup.select_one("title")
        title = title_el.get_text(strip=True) if title_el else ""

        markdown = f"# {title}\n\n{article.get_text(separator='\n', strip=True)}"
        return markdown

    # ── curl_cffi: 直接解析 The Hacker News ──

    def _crawl_curl_cffi(self, source: str, cfg: dict) -> list[NewsArticle]:
        try:
            from curl_cffi import requests
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("curl_cffi or beautifulsoup4 not installed")
            return []

        try:
            resp = requests.get(
                f"https://{cfg['domain']}",
                impersonate="chrome124",
                timeout=20,
            )
        except Exception as e:
            logger.error("  HTTP error: %s", e)
            return []

        if resp.status_code != 200:
            logger.warning("  HTTP %d", resp.status_code)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        articles: list[NewsArticle] = []

        for item in soup.select("div.body-post, div.Story, div.clear.home-right"):
            link = item.select_one("a.story-link, h2.home-title a")
            if not link:
                continue

            title = link.get_text(strip=True)
            href = link.get("href", "")
            if href.startswith("/"):
                href = f"https://{cfg['domain']}{href}"
            if cfg["domain"] not in href:
                continue

            date_el = item.select_one("div.item-label, span.h-datetime, div.date")
            pub_date = self._parse_date(date_el.get_text() if date_el else "")

            desc_el = item.select_one("div.home-desc, div.Story-body, p")
            summary = desc_el.get_text(strip=True)[:400] if desc_el else ""

            articles.append(
                NewsArticle(
                    title=title,
                    url=href,
                    source=source,
                    published_at=pub_date or datetime.now(),
                    summary=summary,
                )
            )

        return articles

    # ── Tavily: site:domain 搜索 ──

    def _crawl_tavily(self, source: str, cfg: dict, days: int = 1) -> list[NewsArticle]:
        domain = cfg["domain"]
        time_range = "day" if days <= 1 else "week" if days <= 7 else "month"

        try:
            response = self.tavily.search(
                query=f"site:{domain} security",
                search_depth="advanced",
                max_results=30,
                include_domains=[domain],
                time_range=time_range,
            )
        except Exception as e:
            logger.error("  Tavily error: %s", e)
            return []

        articles: list[NewsArticle] = []
        for item in response.get("results", []):
            url = item.get("url", "")
            if domain not in url:
                continue

            title = item.get("title", "")
            summary = (item.get("content") or "")[:400]
            pub_date = self._parse_date(item.get("published_date", ""))

            if not pub_date:
                pub_date = self._fetch_date_from_url(url)

            articles.append(
                NewsArticle(
                    title=title,
                    url=url,
                    source=source,
                    published_at=pub_date or datetime.now(),
                    summary=summary,
                )
            )

        return articles

    # ── 日期工具 ──

    @staticmethod
    def _parse_date(text: str) -> Optional[datetime]:
        """解析多种日期格式"""
        if not text:
            return None
        text = str(text).strip()

        # ISO 8601
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(text, fmt)
                return dt.replace(tzinfo=None)
            except ValueError:
                continue

        # RFC 2822
        for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"]:
            try:
                dt = datetime.strptime(text, fmt)
                return dt.replace(tzinfo=None)
            except ValueError:
                continue

        # "Jun 23, 2026"
        for match in re.finditer(r"(\w+)\s+(\d{1,2}),?\s*(\d{4})", text):
            try:
                return datetime.strptime(
                    f"{match.group(1)} {match.group(2)} {match.group(3)}",
                    "%b %d %Y",
                )
            except ValueError:
                pass

        return None

    def _fetch_date_from_url(self, url: str) -> Optional[datetime]:
        """从文章页面 meta/time 标签提取发表时间"""
        try:
            from curl_cffi import requests
            from bs4 import BeautifulSoup
        except ImportError:
            return None

        try:
            resp = requests.get(url, impersonate="chrome124", timeout=10)
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        for prop in [
            "article:published_time", "date", "pubdate",
            "DC.date.issued", "og:article:published_time",
        ]:
            meta = soup.select_one(f'meta[property="{prop}"], meta[name="{prop}"]')
            if meta and meta.get("content"):
                dt = self._parse_date(meta["content"])
                if dt:
                    return dt

        for el in soup.select("time[datetime]"):
            dt = self._parse_date(el.get("datetime", ""))
            if dt:
                return dt

        for sel in [
            "span.date", "div.date", "time.date", "span.post-date",
            "span.published", "span.entry-date", "time.entry-date",
        ]:
            el = soup.select_one(sel)
            if el:
                dt = self._parse_date(el.get_text())
                if dt:
                    return dt

        return None
