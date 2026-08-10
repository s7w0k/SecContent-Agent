"""生成 baseline-manifest.json — 阶段 0 环境与版本冻结。

用法（在仓库根目录）:
    python scripts/generate_baseline_manifest.py

产出:
    baseline-manifest.json —— 冻结 Python/Node/依赖锁、镜像 digest、
    Mongo/Redis/ARQ、模型与 prompt 版本、价格表版本、feature flag 默认值。

同一代码提交 + 同一脚本可重复生成相同 manifest（除 git revision 与镜像 digest 外，
digest 随镜像重建而变化，属预期）。spec: Agent生产上线优化实施计划-20260810/01。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"
OUT_PATH = REPO_ROOT / "baseline-manifest.json"

sys.path.insert(0, str(BACKEND_SRC))


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_revision() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def _docker_digests() -> dict:
    """读取本地 docker images 的 digest（镜像 digest 随重建变化，属预期）。"""
    try:
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.Digest}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception:
        return {}
    digests: dict = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            digests[parts[0]] = parts[1]
    return digests


def _feature_flags() -> dict:
    """从 .env.example 提取 feature flag 默认值。"""
    env_example = REPO_ROOT / ".env.example"
    flags: dict = {}
    if not env_example.exists():
        return flags
    for line in env_example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        if not re.search(r"ENABLED|PERCENT|ROLLOUT|SHADOW|FLAG|MODE", key):
            continue
        flags[key] = value.strip()
    return flags


def _legacy_flags(flags: dict) -> dict:
    """legacy 与各 Agent feature flag（记录默认值，不含密钥类配置）。"""
    legacy = {}
    for key, value in flags.items():
        if key in (
            "CHAT_AGENT_ENABLED",
            "CHAT_AGENT_SHADOW_ENABLED",
            "CHAT_AGENT_ROLLOUT_PERCENT",
            "CHAT_ASK_AGENT_ENABLED",
            "CHAT_REVISE_AGENT_ENABLED",
            "KNOWLEDGE_SKILLS_ENABLED",
            "KNOWLEDGE_SKILLS_SHADOW_ENABLED",
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT",
            "MULTI_AGENT_ENABLED",
            "MULTI_AGENT_SHADOW_ENABLED",
            "MULTI_AGENT_ROLLOUT_PERCENT",
            "AUTONOMOUS_AGENT_ENABLED",
            "AUTONOMOUS_AGENT_SHADOW_ENABLED",
            "AUTONOMOUS_AGENT_ROLLOUT_PERCENT",
            "A2A_ENABLED",
            "A2A_INTERNAL_PEER_ENABLED",
            "WEB_SEARCH_ENABLED",
        ):
            legacy[key] = value
    return legacy


def build_manifest() -> dict:
    """采集并组装 manifest。"""
    # 代码内版本常量
    try:
        from agent.a2a.models import PROTOCOL_VERSION
        from agent.llm_wrapper import PROMPT_VERSIONS
        from agent.planner import PLANNER_VERSION
        from agent.pricing_catalog import PRICING_CATALOG, PRICING_CATALOG_VERSION
    except Exception:
        PROTOCOL_VERSION = PLANNER_VERSION = "unknown"  # noqa: N806
        PROMPT_VERSIONS = {}  # noqa: N806
        PRICING_CATALOG_VERSION = "unknown"  # noqa: N806
        PRICING_CATALOG = ()  # noqa: N806

    from agent.reporter import DEFAULT_TEMPERATURE

    flags = _feature_flags()

    docker_digests = _docker_digests()
    expected_images = {
        "pr-agent-demo-v2-backend:latest": "backend",
        "pr-agent-demo-v2-backend-worker:latest": "backend-worker",
        "pr-agent-demo-v2-mcp-wewe:latest": "mcp-wewe",
        "pr-agent-demo-v2-mcp-crawl:latest": "mcp-crawl",
    }
    images = {
        name: docker_digests.get(image, "not-built") for image, name in expected_images.items()
    }

    manifest = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_revision": _git_revision(),
        "runtime": {
            "python": "3.12",
            "node": "22",
        },
        "dependencies": {
            "python_requirements": {
                "backend": {
                    "file_sha256": _file_sha256(BACKEND_SRC / "requirements.txt"),
                    "locked": False,  # requirements.txt 为范围约束（>=），无锁文件
                },
                "mcp_crawl": {
                    "file_sha256": _file_sha256(
                        REPO_ROOT / "services" / "mcp_crawl" / "requirements.txt"
                    ),
                    "locked": False,
                },
                "mcp_wewe": {
                    "file_sha256": _file_sha256(
                        REPO_ROOT / "services" / "mcp_wewe" / "requirements.txt"
                    ),
                    "locked": False,
                },
            },
            "node": {
                "package_version": "0.1.0",
                "package_json_sha256": _file_sha256(REPO_ROOT / "frontend" / "package.json"),
                "package_lock_sha256": _file_sha256(REPO_ROOT / "frontend" / "package-lock.json"),
                "locked": True,  # 有 package-lock.json
            },
        },
        "docker_images": images,
        "infrastructure": {
            "mongodb": {"image": "mongo:7", "database": "pr_agent"},
            "redis": {"image": "redis:7-alpine", "db": 1},
            "arq": {
                "max_jobs": flags.get("ARQ_MAX_JOBS", "3"),
                "job_timeout_seconds": flags.get("ARQ_JOB_TIMEOUT", "600"),
                "max_retries": flags.get("ARQ_MAX_RETRIES", "3"),
            },
            "searxng": {"image": "searxng/searxng:latest"},
        },
        "models": {
            "default": {
                "provider": "deepseek",
                "model_id": "deepseek-chat",
                "endpoint": "https://api.deepseek.com",
                "timeout_seconds": 60.0,
                "max_tokens": 8192,
                "temperature": DEFAULT_TEMPERATURE,  # reporter 默认
            },
            "autonomous": {
                "model_id": flags.get("AUTONOMOUS_MODEL", "deepseek-chat"),
                "fallback_model": flags.get("AUTONOMOUS_ROUTER_FALLBACK_MODEL", ""),
                "max_input_tokens": flags.get("AUTONOMOUS_MAX_INPUT_TOKENS", "24000"),
                "max_output_tokens": flags.get("AUTONOMOUS_MAX_OUTPUT_TOKENS", "4000"),
                "max_steps": flags.get("AUTONOMOUS_MAX_STEPS", "20"),
            },
            "chat_agent": {
                "max_input_tokens": flags.get("CHAT_AGENT_MAX_INPUT_TOKENS", "24000"),
                "max_output_tokens": flags.get("CHAT_AGENT_MAX_OUTPUT_TOKENS", "4000"),
                "max_rounds": flags.get("CHAT_AGENT_MAX_ROUNDS", "5"),
                "max_tool_calls": flags.get("CHAT_AGENT_MAX_TOOL_CALLS", "8"),
            },
        },
        "prompts_and_knowledge": {
            "prompt_versions": PROMPT_VERSIONS,
            "planner_version": PLANNER_VERSION,
            "a2a_protocol_version": PROTOCOL_VERSION,
            "knowledge_base_dir": "/app/docs",
            "skills_enabled_by_default": flags.get("KNOWLEDGE_SKILLS_ENABLED", "false"),
        },
        "pricing": {
            "version": PRICING_CATALOG_VERSION,
            "currency": "USD",
            "entries": [
                {
                    "provider": e.provider,
                    "model_id": e.model_id,
                    "effective_from": e.effective_from,
                    "input_price_per_million": e.input_price_per_million,
                    "cached_input_price_per_million": e.cached_input_price_per_million,
                    "output_price_per_million": e.output_price_per_million,
                    "currency": e.currency,
                    "source": e.source,
                }
                for e in PRICING_CATALOG
            ],
        },
        "feature_flags": _legacy_flags(flags),
    }
    return manifest


def main() -> None:
    manifest = build_manifest()
    OUT_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"baseline-manifest.json written: {OUT_PATH}")


if __name__ == "__main__":
    main()
