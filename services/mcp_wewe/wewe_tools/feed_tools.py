"""
RSS 分析工具

从 WeWe RSS 获取文章并通过 LLM 进行摘要分析。
"""

from datetime import datetime, timedelta

from dateutil.tz import tzlocal

from .config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, get_rss_url

# ---------------------------------------------------------------------------
#  RSS 获取与筛选
# ---------------------------------------------------------------------------


def fetch_yesterday_articles(rss_url: str = "") -> dict:
    """
    获取昨日发布的所有文章。

    Args:
        rss_url: WeWe RSS 全量 Feed 地址（可选，默认从配置读取）

    Returns:
        {"ok": true, "total_rss": int, "yesterday": int, "articles": [...]}
    """
    import feedparser

    url = rss_url or get_rss_url()

    # 计算昨日时间范围
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    tz = tzlocal()
    start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=tz)
    end = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=tz)

    fp = feedparser.parse(url)

    yest_entries = []
    for e in fp.entries:
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if not pub:
            continue
        dt = datetime(*pub[:6], tzinfo=tz)
        if start <= dt <= end:
            yest_entries.append(
                {
                    "title": e.get("title", "无标题"),
                    "link": e.get("link", ""),
                    "published": dt.isoformat(),
                }
            )

    return {
        "ok": True,
        "total_rss": len(fp.entries),
        "yesterday": len(yest_entries),
        "articles": yest_entries,
    }


# ---------------------------------------------------------------------------
#  全文抓取
# ---------------------------------------------------------------------------


def fetch_article_fulltext(link: str) -> dict:
    """
    抓取微信公众号文章全文（纯文本）。

    Returns:
        {"ok": true, "text": "...", "length": int}
        {"ok": false, "error": "..."}
    """
    import requests
    from bs4 import BeautifulSoup

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    try:
        r = requests.get(link, headers={"User-Agent": ua}, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        content = soup.find("div", class_="rich_media_content")
        if content:
            for img in content.find_all("img"):
                if img.get("data-src") and not img.get("src"):
                    img["src"] = img["data-src"]
            text = content.get_text("\n", strip=True)
            return {"ok": True, "text": text, "length": len(text)}
        return {"ok": False, "error": "未找到正文区域"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# ---------------------------------------------------------------------------
#  LLM 摘要
# ---------------------------------------------------------------------------


def analyze_articles_with_llm(
    articles: list,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> dict:
    """
    对文章列表进行 AI 摘要分析（逐篇）。

    Args:
        articles: [{"title": str, "link": str}, ...]
        api_key:  DeepSeek API Key
        base_url: DeepSeek API Base URL
        model:    模型名

    Returns:
        {"ok": true, "results": [{"title", "link", "summary"}], "errors": int}
    """
    from openai import OpenAI

    key = api_key or DEEPSEEK_API_KEY
    url = base_url or DEEPSEEK_BASE_URL
    mdl = model or DEEPSEEK_MODEL

    client = OpenAI(api_key=key, base_url=url)

    results = []
    errors = 0

    for art in articles:
        title = art.get("title", "无标题")
        link = art.get("link", "")

        # 先抓全文
        fulltext = fetch_article_fulltext(link)
        if not fulltext["ok"] or fulltext["length"] < 200:
            errors += 1
            continue

        text = fulltext["text"][:8000]

        prompt = f"""你是一个资讯分析师。以下是微信公众号的一篇文章正文，请：

1. 用 2-3 句话概括核心内容
2. 提炼 3 个关键信息点（bullet）
3. 判断这篇文章对「互联网/技术从业者」是否有价值，简述理由（1 句）

正文：
{text}"""

        try:
            resp = client.chat.completions.create(
                model=mdl,
                messages=[
                    {"role": "system", "content": "你是一个专业的资讯摘要助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
            )
            summary = resp.choices[0].message.content
        except Exception as ex:
            errors += 1
            summary = f"[分析失败: {ex}]"

        results.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
            }
        )

    return {
        "ok": True,
        "results": results,
        "errors": errors,
    }
