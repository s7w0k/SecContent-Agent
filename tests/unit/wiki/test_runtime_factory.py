"""Phase 11 / PR-15：统一 Runtime Provider 装配测试（G-02/G-03/G-11）。

覆盖：
  - legacy：返回 LegacyKnowledgeProvider，不触碰 Wiki，不校验
  - wiki：strict 装配，返回 WikiKnowledgeProvider + active_version + snapshot
  - Active Wiki 缺失 → fail-fast 抛 KnowledgeRuntimeError（禁止 silently legacy）
  - Manifest / Registry Snapshot 缺失 → fail-fast
  - resolve_wiki_root 缺省取 KNOWLEDGE_BASE_DIR/_wiki
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.wiki.provider import LegacyKnowledgeProvider, WikiKnowledgeProvider
from agent.wiki.runtime_factory import (
    KnowledgeRuntimeError,
    build_knowledge_runtime,
    resolve_wiki_root,
    validate_production_gates,
)

from tests.unit.wiki.helpers import make_page, make_source_file


class _Settings:
    """模拟 Settings 的最小桩。"""

    def __init__(
        self,
        *,
        backend: str,
        wiki_root: str = "",
        base_dir: str = "/app/docs",
    ):
        self.KNOWLEDGE_BACKEND = backend
        self.WIKI_ROOT_DIR = wiki_root
        self.KNOWLEDGE_BASE_DIR = base_dir


def _seed_wiki(tmp_path: Path, source_root: Path, wiki_root: Path) -> str:
    """写一份小 Wiki：注册表 + publisher 发布，返回 active version_id。"""
    from agent.wiki.contracts import SourceRef
    from agent.wiki.linter import WikiLinter
    from agent.wiki.publisher import WikiPublisher
    from agent.wiki.source_registry import SourceRegistry
    from agent.wiki.store import WikiStore

    # 1) source
    make_source_file(source_root, "1-产品/overview.md", "# 产品\n支持身份认证")
    reg_path = wiki_root / "_meta" / "source-registry.json"
    registry = SourceRegistry(source_root, reg_path)
    pending = registry.snapshot_pending()
    registry.commit_snapshot(pending)  # 固化 active snapshot

    # 2) wiki page + publish
    store = WikiStore(wiki_root)
    sr = SourceRef(
        source_id=next(iter(pending.sources)),
        relative_path="1-产品/overview.md",
        content_hash=pending.sources[next(iter(pending.sources))].sha256,
    )
    store.write_page(
        make_page(
            "capability.agent_auth",
            page_type="capability",
            product_id="agent_identity",
            source_refs=[sr],
        )
    )
    meta_dir = wiki_root / "_meta"
    publisher = WikiPublisher(
        store=store,
        linter=WikiLinter(store),
        meta_dir=meta_dir,
        schema_version=2,
        source_snapshot=pending,
    )
    res = publisher.publish()
    assert res.ok, res.errors
    return res.version_id


def test_legacy_mode_returns_legacy_provider(tmp_path: Path) -> None:
    # 即使没有任何 wiki 产物，legacy 也不校验
    settings = _Settings(backend="legacy", wiki_root=str(tmp_path / "nope"))
    rt = build_knowledge_runtime(settings)
    assert rt.mode == "legacy"
    assert rt.active_version == ""
    assert isinstance(rt.provider, LegacyKnowledgeProvider)


def test_wiki_mode_returns_wiki_provider(tmp_path: Path) -> None:
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    version_id = _seed_wiki(tmp_path, source_root, wiki_root)

    settings = _Settings(backend="wiki", wiki_root=str(wiki_root), base_dir=str(source_root))
    rt = build_knowledge_runtime(settings)
    assert rt.mode == "wiki"
    assert isinstance(rt.provider, WikiKnowledgeProvider)
    assert rt.active_version == version_id
    assert rt.source_snapshot_id
    assert rt.store is not None
    assert rt.index is not None and rt.index.wiki_version


def test_wiki_mode_missing_active_fails_fast(tmp_path: Path) -> None:
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    settings = _Settings(backend="wiki", wiki_root=str(wiki_root), base_dir=str(source_root))
    with pytest.raises(KnowledgeRuntimeError):
        build_knowledge_runtime(settings)


def test_shadow_mode_requires_active_wiki(tmp_path: Path) -> None:
    # shadow 也需要 Active Wiki；缺失时不得静默降级（G-03）
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    settings = _Settings(backend="shadow", wiki_root=str(wiki_root), base_dir=str(source_root))
    with pytest.raises(KnowledgeRuntimeError):
        build_knowledge_runtime(settings)


def test_wiki_mode_missing_registry_fails_fast(tmp_path: Path) -> None:
    from agent.wiki.contracts import SourceRef
    from agent.wiki.linter import WikiLinter
    from agent.wiki.publisher import WikiPublisher
    from agent.wiki.source_registry import SourceRegistry
    from agent.wiki.store import WikiStore

    # 有 active manifest + index，随后替换 registry 文件为空快照
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    make_source_file(source_root, "1-产品/overview.md", "# 产品\n支持身份认证")
    reg_path = wiki_root / "_meta" / "source-registry.json"

    registry = SourceRegistry(source_root, reg_path)
    pending = registry.snapshot_pending()

    store = WikiStore(wiki_root)
    store.write_page(
        make_page(
            "capability.agent_auth",
            page_type="capability",
            source_refs=[
                SourceRef(
                    source_id=next(iter(pending.sources)),
                    relative_path="1-产品/overview.md",
                    content_hash=pending.sources[next(iter(pending.sources))].sha256,
                )
            ],
        )
    )
    meta = wiki_root / "_meta"
    publisher = WikiPublisher(
        store=store,
        linter=WikiLinter(store),
        meta_dir=meta,
        schema_version=2,
        source_snapshot=pending,
    )
    res = publisher.publish()
    assert res.ok, res.errors

    # 清空 registry 快照 → 即使有 active manifest/index，也因 Registry Snapshot 为空 fail-fast
    from agent.wiki.source_registry import SourceSnapshot

    SourceRegistry(source_root, reg_path).commit_snapshot(SourceSnapshot.build({}))

    settings = _Settings(backend="wiki", wiki_root=str(wiki_root), base_dir=str(source_root))
    with pytest.raises(KnowledgeRuntimeError) as exc:
        build_knowledge_runtime(settings)
    assert "Registry" in str(exc.value)


def test_resolve_wiki_root_default() -> None:
    settings = _Settings(backend="legacy", base_dir="/app/docs")
    assert resolve_wiki_root(settings) == Path("/app/docs/_wiki")
    settings2 = _Settings(backend="legacy", wiki_root="/custom/wiki")
    assert resolve_wiki_root(settings2) == Path("/custom/wiki")


# ── Phase 23（PR-21）：生产 Backend 门禁 ─────────────────────────


def _gates_settings(*, backend: str, grounding: bool = True) -> _Settings:
    s = _Settings(backend=backend, wiki_root="/tmp/w", base_dir="/tmp/docs")
    s.WIKI_REQUIRE_SOURCE_GROUNDING = grounding
    return s


def _gates_parts(tmp_path: Path):
    source_root = tmp_path / "docs"
    wiki_root = source_root / "_wiki"
    version_id = _seed_wiki(tmp_path, source_root, wiki_root)
    from agent.wiki.index import WikiIndexStore
    from agent.wiki.manifest import ManifestStore
    from agent.wiki.source_registry import SourceRegistry

    return {
        "wiki_root": wiki_root,
        "manifest_store": ManifestStore(wiki_root / "_meta" / "versions"),
        "index_store": WikiIndexStore(wiki_root / "_meta"),
        "registry": SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json"),
        "version_id": version_id,
    }


def test_production_gates_pass_in_wiki_mode(tmp_path: Path) -> None:
    parts = _gates_parts(tmp_path)
    gates = validate_production_gates(
        _gates_settings(backend="wiki"),
        mode="wiki",
        wiki_root=parts["wiki_root"],
        manifest_store=parts["manifest_store"],
        index_store=parts["index_store"],
        registry=parts["registry"],
    )
    assert gates["enforced"] is True
    for name, ok in gates.items():
        if name == "enforced":
            continue
        assert ok, name


def test_production_gates_fail_when_grounding_disabled(tmp_path: Path) -> None:
    parts = _gates_parts(tmp_path)
    with pytest.raises(KnowledgeRuntimeError) as exc:
        validate_production_gates(
            _gates_settings(backend="wiki", grounding=False),
            mode="wiki",
            wiki_root=parts["wiki_root"],
            manifest_store=parts["manifest_store"],
            index_store=parts["index_store"],
            registry=parts["registry"],
        )
    assert "source_grounding_enabled" in str(exc.value) or "门禁" in str(exc.value)


def test_production_gates_fail_when_active_missing(tmp_path: Path) -> None:
    from agent.wiki.index import WikiIndexStore
    from agent.wiki.manifest import ManifestStore

    wiki_root = tmp_path / "empty" / "_wiki"
    with pytest.raises(KnowledgeRuntimeError):
        validate_production_gates(
            _gates_settings(backend="wiki"),
            mode="wiki",
            wiki_root=wiki_root,
            manifest_store=ManifestStore(wiki_root / "_meta" / "versions"),
            index_store=WikiIndexStore(wiki_root / "_meta"),
            registry=None,
        )


def test_production_gates_not_enforced_for_legacy(tmp_path: Path) -> None:
    gates = validate_production_gates(
        _gates_settings(backend="legacy"),
        mode="legacy",
        wiki_root=tmp_path / "w",
        manifest_store=None,
        index_store=None,
        registry=None,
    )
    assert gates == {"enforced": False}
