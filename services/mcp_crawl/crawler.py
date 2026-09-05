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
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger("mcp-crawl.crawler")


def _proxy_config() -> dict[str, str] | None:
    """Return curl_cffi proxy settings without ever logging proxy credentials."""
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )
    return {"https": proxy, "http": proxy} if proxy else None


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
        return hashlib.md5(self.url.encode(), usedforsecurity=False).hexdigest()

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
    "/deals/",
    "/offer/",
    "/sales/",
    "/shop/",
    "/store/",
    "/webinar",
    "/podcast",
    "/video/",
    "/videos/",
    "/sponsor",
    "/advertise",
    "/free-",
    "discount",
    "coupon",
    "/how-to-",
    "/deal-",
    "-deal",
    "giveaway",
]
# 非新闻标题关键词（含其一则跳过）
_NON_NEWS_TITLE_WORDS = [
    "deal",
    "sale",
    "discount",
    "coupon",
    "giveaway",
    "just $",
    "only $",
    "% off",
    "save ",
    "webinar",
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
        # ── 海外安全新闻（需网络可达，部分站点可能需要代理） ──
        "The Hacker News": {
            "domain": "thehackernews.com",
            "feed": "https://thehackernews.com/feeds/posts/default",
            "fallback": "https://thehackernews.com/rss.xml",
            "rsshub": "/thehackernews",
        },
        "BleepingComputer": {
            "domain": "bleepingcomputer.com",
            "feed": "https://www.bleepingcomputer.com/feed/",
            "fallback": "https://feeds.feedburner.com/BleepingComputer",
        },
        "SecurityWeek": {
            "domain": "securityweek.com",
            "feed": "https://www.securityweek.com/feed/",
            "fallback": "https://feeds.feedburner.com/securityweek",
        },
        "Help Net Security": {
            "domain": "helpnetsecurity.com",
            "feed": "https://www.helpnetsecurity.com/rss.xml",
            "fallback": "https://www.helpnetsecurity.com/feed/",
        },
        "Dark Reading": {
            "domain": "darkreading.com",
            "feed": "https://www.darkreading.com/rss.xml",
            "fallback": "https://www.darkreading.com/rss/simple.xml",
        },
        # ── 国内安全新闻（无需代理，RSS 直接可达） ──
        "FreeBuf": {
            "domain": "freebuf.com",
            "feed": "https://www.freebuf.com/feed",
            "fallback": "https://rss.feedspot.com/folder/Lu7DjgV4nOQ=/",
        },
    }

    def __init__(self, tavily_api_key: str = ""):
        # RSS-only mode, no API key needed
        pass

    async def crawl(self, days: int = 1) -> list[NewsArticle]:
        """爬取所有站点最近 N 天的文章"""
        cutoff = datetime.now() - timedelta(days=days)
        all_articles: list[NewsArticle] = []
        self._last_errors: dict[str, str] = {}
        self._per_site: dict[str, int] = {}
        self._per_site_detail: dict[str, dict] = {}

        for site_name, cfg in self.SITES.items():
            logger.info("Crawling: %s (RSS)", site_name)
            try:
                articles, detail = self._crawl_rss(site_name, cfg, cutoff)
                self._per_site[site_name] = len(articles)
                self._per_site_detail[site_name] = detail
                logger.info("  %s: %d articles", site_name, len(articles))
                all_articles.extend(articles)
            except Exception as e:
                msg = str(e)
                logger.error("  %s: %s", site_name, msg)
                self._last_errors[site_name] = msg
                self._per_site[site_name] = 0

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

    async def fetch_fulltext_batch(
        self,
        articles: list[NewsArticle],
        *,
        max_concurrent: int = 5,
        per_domain_concurrent: int = 2,
        delay_range: tuple[float, float] = (1.0, 3.0),
        max_retries: int = 3,
    ) -> dict[str, str]:
        """异步批量抓取文章全文（含反风控策略）

        Args:
            articles: 文章列表
            max_concurrent: 全局最大并发数
            per_domain_concurrent: 同域名最大并发数
            delay_range: 请求前随机延迟范围（秒）
            max_retries: 失败重试次数

        Returns:
            {url_hash: content_md} 成功抓取的全文映射
        """
        import asyncio
        import random
        from urllib.parse import urlparse

        if not articles:
            return {}

        # 按域名分组
        domain_groups: dict[str, list[NewsArticle]] = {}
        for art in articles:
            domain = urlparse(art.url).netloc
            domain_groups.setdefault(domain, []).append(art)

        global_sem = asyncio.Semaphore(max_concurrent)
        domain_sems: dict[str, asyncio.Semaphore] = {
            d: asyncio.Semaphore(per_domain_concurrent) for d in domain_groups
        }

        results: dict[str, str] = {}
        success_count = 0
        fail_count = 0

        async def _fetch_one(art: NewsArticle):
            nonlocal success_count, fail_count
            domain = urlparse(art.url).netloc
            # 随机延迟
            await asyncio.sleep(random.uniform(*delay_range))

            async with global_sem, domain_sems[domain]:
                for attempt in range(max_retries + 1):
                    try:
                        content = await self.fetch_fulltext(art.url)
                        if content and len(content) >= 100:
                            results[art.url_hash] = content
                            success_count += 1
                            logger.info(
                                "[fulltext] OK: %s (%d chars)", art.title[:40], len(content)
                            )
                            return
                    except Exception as e:
                        logger.warning(
                            "[fulltext] attempt %d failed: %s - %s", attempt + 1, art.url[:50], e
                        )

                    if attempt < max_retries:
                        backoff = 2**attempt + random.uniform(0, 1)
                        logger.info("[fulltext] retry in %.1fs: %s", backoff, art.url[:50])
                        await asyncio.sleep(backoff)

                fail_count += 1
                logger.warning("[fulltext] GIVE UP: %s", art.url[:60])

        tasks = [_fetch_one(art) for art in articles]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "[fulltext] Batch done: %d/%d success, %d failed",
            success_count,
            len(articles),
            fail_count,
        )
        return results

    async def fetch_fulltext(self, url: str) -> str:
        """抓取单篇文章全文并转为 Markdown。"""
        try:
            from bs4 import BeautifulSoup
            from curl_cffi import requests
        except ImportError:
            return ""

        try:
            # 用线程池执行同步 HTTP 请求，避免阻塞事件循环
            import asyncio as _aio

            resp = await _aio.to_thread(
                requests.get,
                url,
                impersonate="chrome124",
                timeout=15,
                proxies=_proxy_config(),
            )
            if resp.status_code != 200:
                return ""
        except Exception:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # 移除无用标签
        for tag in soup(
            ["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]
        ):
            tag.decompose()

        # 定位正文容器：优先专用选择器，article 标签可能匹配到广告区域需过滤
        selectors = [
            "div.post-body",
            "div#articlebody",
            "div.article-body",
            "div.article-content",
            "div.post-content",
            "div.entry-content",
            "div.body-post",
            "div.story-body",
            "div.article-text",
            "div.content-body",
            "main article",
            "article",  # article 标签放最后，可能匹配到广告
        ]
        article = None
        for sel in selectors:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if len(text) >= 500:  # 正文至少 500 字符，过滤广告/侧边栏
                    article = el
                    break
            if article:
                break
        if not article:
            article = soup.body or soup

        # 提取标题
        title_el = soup.select_one("h1") or soup.select_one("title")
        title = title_el.get_text(strip=True) if title_el else ""

        markdown = f"# {title}\n\n{article.get_text(separator='\n', strip=True)}"
        return markdown

    # ── RSS: feedparser 解析标准 RSS/Atom ──

    def _crawl_rss(
        self, source: str, cfg: dict, cutoff: datetime
    ) -> tuple[list[NewsArticle], dict]:
        """通过 RSS/Atom feed 爬取文章，严格按 cutoff 时间过滤。"""
        from email.utils import parsedate_to_datetime

        try:
            import feedparser as _fp
        except ImportError:
            logger.error("  feedparser not installed!")
            return [], {}

        # 用 curl_cffi 浏览器指纹模拟获取 RSS 内容，绕过 Cloudflare 403
        # 如果主 feed 失败，尝试 Google News fallback
        feed = None
        feed_urls = [cfg["feed"]]
        if cfg.get("fallback"):
            feed_urls.append(cfg["fallback"])

        # 代理支持：从环境变量读取，但日志不输出代理地址和凭据
        proxies = _proxy_config()
        proxy = proxies["https"] if proxies else None
        if proxies:
            logger.info("  Using configured proxy")

        try:
            from curl_cffi import requests as cffi_requests

            for i, feed_url in enumerate(feed_urls):
                tag = "primary" if i == 0 else "fallback"
                try:
                    resp = cffi_requests.get(
                        feed_url,
                        impersonate="chrome124",
                        timeout=15,
                        proxies=proxies,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Cache-Control": "no-cache",
                        },
                    )
                    if resp.status_code == 200:
                        parsed = _fp.parse(resp.content)
                        if parsed.entries:
                            feed = parsed
                            logger.info(
                                "  RSS: %s (%s) -> %d entries",
                                source,
                                tag,
                                len(parsed.entries),
                            )
                            break
                        else:
                            logger.warning("  RSS: %s (%s) -> 0 entries", source, tag)
                    else:
                        logger.warning(
                            "  RSS: %s (%s) -> HTTP %d",
                            source,
                            tag,
                            resp.status_code,
                        )
                except Exception as e:
                    logger.warning("  RSS: %s (%s) -> %s", source, tag, e)

            if feed is None:
                return [], {}

        except ImportError:
            logger.warning("  curl_cffi not installed, falling back to httpx")
            import httpx

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            }
            try:
                with httpx.Client(
                    timeout=15, headers=headers, follow_redirects=True, proxy=proxy
                ) as http_client:
                    for i, feed_url in enumerate(feed_urls):
                        tag = "primary" if i == 0 else "fallback"
                        try:
                            resp = http_client.get(feed_url)
                            if resp.status_code == 200:
                                parsed = _fp.parse(resp.content)
                                if parsed.entries:
                                    feed = parsed
                                    logger.info(
                                        "  RSS: %s (%s) -> %d entries",
                                        source,
                                        tag,
                                        len(parsed.entries),
                                    )
                                    break
                                else:
                                    logger.warning(
                                        "  RSS: %s (%s) -> 0 entries",
                                        source,
                                        tag,
                                    )
                            else:
                                logger.warning(
                                    "  RSS: %s (%s) -> HTTP %d",
                                    source,
                                    tag,
                                    resp.status_code,
                                )
                        except Exception as e:
                            logger.warning("  RSS: %s (%s) -> %s", source, tag, e)

                if feed is None:
                    return [], {}
            except Exception as e:
                logger.warning("  RSS fetch error (%s): %s", source, e)
                return [], {}

        # ── RSSHub fallback：通过自建 RSSHub 实例获取（可绕过 Cloudflare JS 挑战） ──
        if feed is None and cfg.get("rsshub"):
            rsshub_url = os.environ.get("RSSHUB_URL", "")
            if rsshub_url:
                rsshub_feed_url = f"{rsshub_url.rstrip('/')}{cfg['rsshub']}"
                tag = "rsshub"
                try:
                    import httpx

                    with httpx.Client(timeout=30, follow_redirects=True) as http_client:
                        resp = http_client.get(rsshub_feed_url)
                        if resp.status_code == 200:
                            parsed = _fp.parse(resp.content)
                            if parsed.entries:
                                feed = parsed
                                logger.info(
                                    "  RSS: %s (%s) -> %d entries",
                                    source,
                                    tag,
                                    len(parsed.entries),
                                )
                            else:
                                logger.warning("  RSS: %s (%s) -> 0 entries", source, tag)
                        else:
                            logger.warning(
                                "  RSS: %s (%s) -> HTTP %d",
                                source,
                                tag,
                                resp.status_code,
                            )
                except Exception as e:
                    logger.warning("  RSS: %s (%s) -> %s", source, tag, e)

        if feed is None:
            return [], {}

        if feed.bozo and not feed.entries:
            logger.warning("  RSS: %s (bozo=%s)", source, feed.bozo_exception)
            return [], {}

        if not feed.entries:
            logger.warning("  %s: feed returned 0 entries", source)

        articles: list[NewsArticle] = []
        total_entries = len(feed.entries)
        skipped_no_date = 0
        skipped_old = 0
        skipped_non_news = 0
        for entry in feed.entries:
            url = entry.get("link", "") or entry.get("guid", "") or entry.get("id", "")
            if not url:
                continue
            title = entry.get("title", "")
            if _is_non_news(url, title):
                skipped_non_news += 1
                continue
            summary = (entry.get("summary", "") or entry.get("description", ""))[:500]

            # 解析日期 — 多字段 + 多格式尝试
            pub_date = None
            for field in ("published_parsed", "updated_parsed"):
                v = entry.get(field)
                if v and hasattr(v, "tm_year"):
                    from calendar import timegm

                    pub_date = datetime(1970, 1, 1) + timedelta(seconds=timegm(v))
                    break
            if pub_date is None:
                for field in ("published", "updated", "pubDate", "dc:date", "created"):
                    raw = entry.get(field, "")
                    if raw:
                        with contextlib.suppress(Exception):
                            pub_date = parsedate_to_datetime(str(raw))
                        if pub_date:
                            break
            # 最后尝试 datetime.strptime 常见 RSS 格式
            if pub_date is None:
                for field in ("published", "updated", "pubDate"):
                    raw = entry.get(field, "")
                    if raw:
                        for fmt in (
                            "%a, %d %b %Y %H:%M:%S %z",
                            "%a, %d %b %Y %H:%M:%S %Z",
                            "%Y-%m-%dT%H:%M:%S%z",
                            "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d",
                        ):
                            with contextlib.suppress(Exception):
                                pub_date = datetime.strptime(str(raw), fmt)
                            if pub_date:
                                break
                        if pub_date:
                            break

            # 严格时间过滤：无日期或超出范围跳过
            if pub_date is None:
                skipped_no_date += 1
                continue
            if pub_date < cutoff:
                skipped_old += 1
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

        detail = {
            "feed_entries": total_entries,
            "skipped_no_date": skipped_no_date,
            "skipped_old": skipped_old,
            "skipped_non_news": skipped_non_news,
            "accepted": len(articles),
        }
        return articles, detail

    # ── 日期工具 ──

    @staticmethod
    def _parse_date(text: str) -> datetime | None:
        """解析多种日期格式（保留供外部使用）"""
        if not text:
            return None
        text = str(text).strip()

        # ISO 8601
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
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
