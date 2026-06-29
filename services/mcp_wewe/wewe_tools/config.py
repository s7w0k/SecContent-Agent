"""
配置管理 — 所有模块通过此文件获取统一的配置。
优先级：函数参数 > 环境变量 > 默认值
"""

import os

# 默认值使用 copy 版中的实际配置
_DEFAULT_URL = "http://49.232.145.182:4001"
_DEFAULT_AUTH = "123567"


def get_wewe_url() -> str:
    return os.getenv("WEWE_RSS_URL", _DEFAULT_URL)


def get_auth_code() -> str:
    return os.getenv("WEWE_AUTH_CODE", _DEFAULT_AUTH)


def get_rss_url() -> str:
    base = get_wewe_url().rstrip("/")
    return os.getenv("WEWE_RSS_FEED_URL", f"{base}/feeds/all.rss")


# DeepSeek 配置（可被环境变量覆盖）
DEEPSEEK_API_KEY = os.getenv(
    "DEEPSEEK_API_KEY",
    "sk-REDACTED",
)
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com/v1",
)
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
