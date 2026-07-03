"""
WeWe RSS 工具包

提供账号管理、RSS 分析等功能的 Python 工具集，
可被 MCP Server 或独立脚本调用。
"""

from .account_tools import (
    check_accounts,
    create_login_qrcode,
    delete_account,
    has_usable_account,
    poll_login_result,
    save_account,
)
from .feed_tools import (
    analyze_articles_with_llm,
    fetch_article_fulltext,
    fetch_yesterday_articles,
)

__all__ = [
    "analyze_articles_with_llm",
    # 账号工具
    "check_accounts",
    "create_login_qrcode",
    "delete_account",
    "fetch_article_fulltext",
    # RSS 分析工具
    "fetch_yesterday_articles",
    "has_usable_account",
    "poll_login_result",
    "save_account",
]
