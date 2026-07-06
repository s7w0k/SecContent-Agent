"""
海外安全新闻爬虫 — 基于 RSS/Atom Feed 的标准新闻抓取。

所有站点通过 RSS feed 获取（feedparser），严格按时间过滤，
无日期文章直接丢弃。无需任何 API Key。

覆盖:
  - The Hacker News (FeedBurner RSS)
  - BleepingComputer (WordPress RSS)
  - SecurityWeek (WordPress RSS)
  - Help Net Security (WordPress RSS)
  - Dark Reading (RSS XML)

使用方式:
    crawler = NewsCrawler()
    articles = await crawler.crawl(days=1)
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    published_at: datetime | None = None
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

# 非新闻内容 URL/域名 黑名单
_NON_NEWS_DOMAINS = [
    "deals.bleepingcomputer.com",
]
_NON_NEWS_PATTERNS = [
    "/deals/", "/offer/", "/sales/", "/shop/", "/store/",
    "/webinar", "/podcast", "/video/", "/videos/",
    "/sponsor", "/advertise", "/free-", "discount", "coupon",
    "/how-to-", "/deal-", "-deal", "giveaway",
]
# 非新闻标题关键词（含其一则跳过）
_NON_NEWS_TITLE_WORDS = [
    "deal", "sale", "discount", "coupon", "giveaway",
    "just $", "only $", "% off", "save ", "webinar",
]


def _is_non_news(url: str, title: str = "") -> bool:
    """判断是否为非新闻内容（促销、广告、教程等）"""
    url_lower = url.lower()
    # 域名黑名单
    if any(d in url_lower for d in _NON_NEWS_DOMAINS):
        return True
    # URL 路径模式
    if any(p.lower() in url_lower for p in _NON_NEWS_PATTERNS):
        return True
    # 标题关键词
    if title:
        title_lower = title.lower()
        if any(w in title_lower for w in _NON_NEWS_TITLE_WORDS):
            return True
    return False


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
            "feed": "https://feeds.feedburner.com/TheHackersNews",
        },
        "BleepingComputer": {
            "domain": "bleepingcomputer.com",
            # 使用 news 分类 RSS 排除 deals/tutorials
            "feed": "https://www.bleepingcomputer.com/feed/?post_type=post&category_name=news",
        },
        "SecurityWeek": {
            "domain": "securityweek.com",
            "feed": "https://www.securityweek.com/feed/",
        },
        "Help Net Security": {
            "domain": "helpnetsecurity.com",
            "feed": "https://www.helpnetsecurity.com/feed/",
        },
        "Dark Reading": {
            "domain": "darkreading.com",
            "feed": "https://www.darkreading.com/rss.xml",
        },
    }

    def __init__(self, tavily_api_key: str = ""):
        # RSS-only mode, no API key needed
        pass

    async def crawl(self, days: int = 1) -> list[NewsArticle]:
        """爬取所有站点最近 N 天的文章（同步阻塞方法用 asyncio.to_thread 包装时调用者负责）"""
        cutoff = datetime.now() - timedelta(days=days)
        all_articles: list[NewsArticle] = []

        for site_name, cfg in self.SITES.items():
            logger.info("Crawling: %s (RSS)", site_name)
            try:
                articles = self._crawl_rss(site_name, cfg, cutoff)

                logger.info("  %s: %d articles", site_name, len(articles))
                all_articles.extend(articles)
            except Exception as e:
                logger.error("  %s: %s", site_name, e)

        # URL 去重 + 时间过滤 + 非新闻内容过滤
        seen: set[str] = set()
        filtered: list[NewsArticle] = []
        for art in all_articles:
            key = art.url_hash
            if key in seen:
                continue
            seen.add(key)
            if art.published_at and art.published_at < cutoff:
                continue
            if _is_non_news(art.url, art.title):
                logger.debug("  Skipping non-news: %s", art.url)
                continue
            filtered.append(art)

        filtered.sort(key=lambda a: a.published_at or datetime.min, reverse=True)
        logger.info("Total after dedup + date filter (%dd): %d", days, len(filtered))
        return filtered

    async def fetch_fulltext(self, url: str) -> str:
        """抓取单篇文章全文并转为 Markdown。"""
        try:
            from bs4 import BeautifulSoup
            from curl_cffi import requests
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

    # ── RSS: feedparser 解析标准 RSS/Atom ──

    def _crawl_rss(self, source: str, cfg: dict, cutoff: datetime) -> list[NewsArticle]:
        """通过 RSS/Atom feed 爬取文章，严格按 cutoff 时间过滤。"""
        from email.utils import parsedate_to_datetime

        import feedparser as _fp

        try:
            feed = _fp.parse(cfg["feed"])
            if feed.bozo and not feed.entries:
                logger.warning("  RSS parse warning: %s", feed.bozo_exception)
                return []
        except Exception as e:
            logger.error("  RSS fetch error: %s", e)
            return []

        articles: list[NewsArticle] = []
        for entry in feed.entries:
            url = entry.get("link", "")
            if not url or cfg["domain"] not in url:
                continue
            title = entry.get("title", "")
            if _is_non_news(url, title):
                continue
            summary = (entry.get("summary", "") or entry.get("description", ""))[:500]

            # 解析日期
            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                from calendar import timegm
                pub_date = datetime(1970, 1, 1) + timedelta(seconds=timegm(entry.published_parsed))
            elif entry.get("published"):
                with contextlib.suppress(Exception):
                    pub_date = parsedate_to_datetime(entry["published"])

            # 严格时间过滤：无日期或超出范围跳过
            if pub_date is None:
                continue
            if pub_date < cutoff:
                continue

            articles.append(
                NewsArticle(
                    title=title,
                    url=url,
                    source=source,
                    published_at=pub_date,
                    summary=summary,
                )
            )

        return articles

    # ── 日期工具 ──

    @staticmethod
    def _parse_date(text: str) -> datetime | None:
        """解析多种日期格式（保留供外部使用）"""
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
