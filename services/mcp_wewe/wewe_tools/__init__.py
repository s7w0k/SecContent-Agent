"""
WeWe RSS 工具包

提供账号管理、RSS 分析等功能的 Python 工具集，
可被 MCP Server 或独立脚本调用。
"""

from .account_tools import (
    check_accounts,
    create_login_qrcode,
    poll_login_result,
    save_account,
    delete_account,
    has_usable_account,
)

from .feed_tools import (
    fetch_yesterday_articles,
    fetch_article_fulltext,
    analyze_articles_with_llm,
)

__all__ = [
    # 账号工具
    "check_accounts",
    "create_login_qrcode",
    "poll_login_result",
    "save_account",
    "delete_account",
    "has_usable_account",
    # RSS 分析工具
    "fetch_yesterday_articles",
    "fetch_article_fulltext",
    "analyze_articles_with_llm",
]
