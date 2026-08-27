"""统一 Runtime Provider 装配（Phase 11 / PR-15，G-02/G-03/G-11）。

- 唯一 Factory：`build_knowledge_runtime(settings, llm, db)`
- FastAPI `main.py` 与 ARQ `worker.py` 必须调用同一个 Factory
- Strict Startup：`KNOWLEDGE_BACKEND=wiki` 时，若 Active Wiki / Manifest /
  Registry Snapshot / Tree Hash 任一缺失或不一致，必须 fail-fast（G-11），
  禁止 `wiki unavailable → silently legacy`（G-03）。
- Mode 语义：
    - legacy            → 旧链路，不接 Wiki
    - wiki              → Wiki-only，strict
    - shadow            → 旧结果同时后台跑 Wiki（对比期）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("backend.agent.wiki.runtime_factory")


class KnowledgeRuntimeError(Exception):
    """Knowledge Runtime 装配失败。wiki 模式下必须 fail-fast，不得静默降级。"""


@dataclass
class KnowledgeRuntime:
    """装配完成的 Wiki Knowledge Runtime 视图。"""

    provider: Any
    store: Any | None
    index: Any | None
    active_version: str
    source_snapshot_id: str
    mode: str


_DEFAULT_REGISTRY_NAME = "source-registry.json"


def resolve_wiki_root(settings: Any) -> Path:
    """解析 Wiki Root：优先 WIKI_ROOT_DIR，否则 KNOWLEDGE_BASE_DIR/_wiki。"""
    configured = getattr(settings, "WIKI_ROOT_DIR", "") or ""
    if configured:
        return Path(configured)
    base = getattr(settings, "KNOWLEDGE_BASE_DIR", "/app/docs")
    return Path(base) / "_wiki"


def _meta_dir(wiki_root: Path) -> Path:
    return wiki_root / "_meta"


def _registry_path(wiki_root: Path) -> Path:
    return _meta_dir(wiki_root) / _DEFAULT_REGISTRY_NAME


def _verify_wiki_artifact(
    *,
    mode: str,
    wiki_root: Path,
    manifest_store: Any,
    index_store: Any,
    registry: Any,
) -> tuple[str, str]:
    """校验 Active Wiki 产物；任一缺失/不一致即抛 KnowledgeRuntimeError。

    wiki / shadow 都要求存在可读取的 Active Wiki Artifact（G-11），
    禁止 `wiki unavailable → silently legacy`（G-03）。
    返回 (active_version, source_snapshot_id)。
    """
    active = manifest_store.active_version()
    if not active:
        raise KnowledgeRuntimeError(
            f"[{mode}] Active Wiki Version 不存在，禁止静默降级 (wiki_root={wiki_root})"
        )

    m = manifest_store.load(active)
    if m is None:
        raise KnowledgeRuntimeError(f"[{mode}] Active Manifest 不存在: {active}")
    errors = m.validate_self()
    if errors:
        raise KnowledgeRuntimeError(f"[{mode}] Active Manifest 自校验失败: {errors}")
    if m.schema_version < 1 or m.schema_version > 2:
        raise KnowledgeRuntimeError(f"[{mode}] Schema 版本不支持: {m.schema_version}")
    if not m.page_tree_hash:
        raise KnowledgeRuntimeError(f"[{mode}] Manifest 缺少 page_tree_hash")

    # Tree Hash 校验：Disk Index 与 Manifest 一致（G-11）
    index_manifest = index_store.load()
    if index_manifest is not None and index_manifest.wiki_version != m.page_tree_hash:
        raise KnowledgeRuntimeError(
            f"[{mode}] Tree Hash 不一致: index={index_manifest.wiki_version} "
            f"manifest={m.page_tree_hash}"
        )

    snapshot = registry.active_snapshot() if registry is not None else None
    snapshot_id = ""
    if snapshot is not None:
        if not snapshot.sources:
            raise KnowledgeRuntimeError(f"[{mode}] Registry Snapshot 为空")
        snapshot_id = snapshot.snapshot_id
    else:
        raise KnowledgeRuntimeError(f"[{mode}] Registry Snapshot 缺失")

    return active, snapshot_id


def validate_production_gates(
    settings: Any,
    *,
    mode: str,
    wiki_root: Path,
    manifest_store: Any,
    index_store: Any,
    registry: Any,
) -> dict[str, bool]:
    """生产 Backend 强制检查（Phase 23，PR-21）：wiki 模式必须全过。

    对应 Phase 23 启动断言：
      assert active_wiki_exists
      assert manifest_valid
      assert registry_match
      assert schema_supported
      assert source_grounding_enabled
    任一断言失败即抛 `KnowledgeRuntimeError`（fail-fast，禁止静默降级）。
    """
    if mode != "wiki":
        # 仅 wiki（切默认生产 Backend）时严格校验 grounding 门禁；
        # legacy / shadow 不在此处设置生产门槛。
        return {"enforced": False}

    active = manifest_store.active_version()
    m = manifest_store.load(active) if active else None

    snapshot_id = ""
    registry_match = False
    if registry is not None:
        snapshot = registry.active_snapshot()
        if snapshot is not None and snapshot.sources:
            snapshot_id = snapshot.snapshot_id
            registry_match = bool(snapshot_id)

    schema_supported = m is not None and (1 <= m.schema_version <= 2)
    source_grounding_enabled = bool(getattr(settings, "WIKI_REQUIRE_SOURCE_GROUNDING", True))

    gates: dict[str, bool] = {
        "active_wiki_exists": bool(active),
        "manifest_valid": m is not None and not m.validate_self(),
        "registry_match": registry_match,
        "schema_supported": schema_supported,
        "source_grounding_enabled": source_grounding_enabled,
    }
    failed = [name for name, ok in gates.items() if not ok]
    if failed:
        raise KnowledgeRuntimeError(f"[wiki] 生产门禁未通过: {failed} (wiki_root={wiki_root})")
    gates["enforced"] = True
    return gates


def build_knowledge_runtime(
    settings: Any,
    llm: Any | None = None,
    db: Any | None = None,
) -> KnowledgeRuntime:
    """构建统一的 Knowledge Provider 及其依赖。

    - legacy：直接返回旧链路 Provider，不触碰 Wiki
    - wiki：strict 装配，Active Wiki 缺失即抛错
    - shadow：双跑对比，Wiki 缺失时令（区别于 silent fallback）同样抛错
    """
    from agent.wiki.index import WikiIndex, WikiIndexStore
    from agent.wiki.manifest import ManifestStore
    from agent.wiki.provider import (
        LegacyKnowledgeProvider,
        build_knowledge_provider,
    )
    from agent.wiki.source_registry import SourceRegistry
    from agent.wiki.store import WikiStore

    # GOAL B/§22：去掉隐藏的 legacy default。settings 缺 KNOWLEDGE_BACKEND 字段 = 配置 bug，
    # ≠ 自动退 Legacy 的理由；必须显式抛错，禁止再静默默认到 legacy。
    try:
        mode = settings.KNOWLEDGE_BACKEND
    except AttributeError as exc:
        raise KnowledgeRuntimeError(
            "KNOWLEDGE_BACKEND missing from settings — must be explicitly configured"
        ) from exc

    if mode == "legacy":
        return KnowledgeRuntime(
            provider=LegacyKnowledgeProvider(),
            store=None,
            index=None,
            active_version="",
            source_snapshot_id="",
            mode="legacy",
        )
    if mode not in {"wiki", "shadow"}:
        raise KnowledgeRuntimeError(f"未知 KNOWLEDGE_BACKEND: {mode!r}")

    wiki_root = resolve_wiki_root(settings)
    meta = _meta_dir(wiki_root)
    manifest_store = ManifestStore(meta / "versions")
    index_store = WikiIndexStore(meta)
    source_root = Path(getattr(settings, "KNOWLEDGE_BASE_DIR", "/app/docs"))
    registry = SourceRegistry(source_root, _registry_path(wiki_root))

    active, snapshot_id = _verify_wiki_artifact(
        mode=mode,
        wiki_root=wiki_root,
        manifest_store=manifest_store,
        index_store=index_store,
        registry=registry,
    )
    if mode == "wiki":
        # Phase 23（PR-21）：切默认生产 Backend 时强制全部生产门禁。
        # 若 WIKI_REQUIRE_SOURCE_GROUNDING 被关闭，wiki 严格模式必须 fail-fast。
        validate_production_gates(
            settings,
            mode=mode,
            wiki_root=wiki_root,
            manifest_store=manifest_store,
            index_store=index_store,
            registry=registry,
        )

    store = WikiStore(wiki_root)
    index = WikiIndex(index_store.load())
    provider = build_knowledge_provider(
        mode=mode,
        store=store,
        index=index,
        source_registry=registry,
        source_root=str(source_root),
        llm=llm,
        navigator_llm_enabled=bool(getattr(settings, "WIKI_NAVIGATOR_LLM_ENABLED", False)),
        confidence_threshold=float(getattr(settings, "WIKI_EVIDENCE_CONFIDENCE_THRESHOLD", 0.8)),
        relevance_threshold=float(getattr(settings, "WIKI_EVIDENCE_RELEVANCE_THRESHOLD", 0.5)),
        min_coverage={
            "score": float(getattr(settings, "WIKI_MIN_COVERAGE_SCORE", 0.70)),
            "draft": float(getattr(settings, "WIKI_MIN_COVERAGE_DRAFT", 0.80)),
            "chat": float(getattr(settings, "WIKI_MIN_COVERAGE_CHAT", 0.60)),
        },
    )

    return KnowledgeRuntime(
        provider=provider,
        store=store,
        index=index,
        active_version=active,
        source_snapshot_id=snapshot_id,
        mode=mode,
    )
