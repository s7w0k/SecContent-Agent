"""Phase1 / PR-08：KnowledgeBuildTransaction 与双快照 Registry 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.wiki.maintainer import WikiMaintainer
from agent.wiki.publisher import PublicationResult
from agent.wiki.source_registry import SourceRegistry, SourceSnapshot
from agent.wiki.transaction import (
    KnowledgeBuildTransaction,
    TransactionStore,
    compute_build_id,
)
from helpers import make_source_file, sha256_hex


class _FakePublisher:
    """可控 publisher：ok=True/False 由参数决定，且可记录是否被调用。"""

    def __init__(self, ok: bool, wiki_version: str = "wiki-v1"):
        self.ok = ok
        self.wiki_version = wiki_version
        self.calls = 0

    def publish(self) -> PublicationResult:
        self.calls += 1
        if self.ok:
            return PublicationResult(ok=True, wiki_version=self.wiki_version, pages_published=1)
        return PublicationResult(ok=False, errors=["lint gate rejected"])


# ── compute_build_id 幂等 ───────────────────────────────────────


def test_build_id_idempotent():
    kw = {
        "parent_wiki_version": "wiki-prev",
        "source_snapshot_hash": "sha256:abc",
        "compiler_version": "deterministic-1",
        "schema_version": 1,
    }
    assert compute_build_id(**kw) == compute_build_id(**kw)


def test_build_id_changes_with_inputs():
    a = compute_build_id(
        parent_wiki_version="p", source_snapshot_hash="h", compiler_version="c", schema_version=1
    )
    b = compute_build_id(
        parent_wiki_version="p", source_snapshot_hash="h2", compiler_version="c", schema_version=1
    )
    assert a != b


# ── KnowledgeBuildTransaction 状态机 ────────────────────────────


def _snapshot() -> SourceSnapshot:
    return SourceSnapshot.build({})


def test_begin_creates_discovered():
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p")
    assert tx.state == "DISCOVERED"
    assert tx.build_id == tx.transaction_id
    assert tx.parent_wiki_version == "p"


def test_happy_path_transitions():
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p")
    for state in [
        "COMPILING",
        "COMPILED",
        "VALIDATING",
        "READY_TO_PUBLISH",
        "PUBLISHING",
        "PUBLISHED",
        "COMMITTED",
    ]:
        tx.transition(state)  # type: ignore[arg-type]
    assert tx.state == "COMMITTED"


def test_invalid_transition_raises():
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot())
    with pytest.raises(ValueError):
        tx.transition("COMMITTED")  # DISCOVERED 不能直接 COMMITTED


def test_recovery_action_mapping():
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot())
    assert tx.recovery_action() == "RESTART_COMPILE"
    for state, expect in {
        "COMPILING": "CLEANUP_STAGING_AND_COMPILE",
        "PUBLISHED": "COMMIT_REGISTRY",
        "COMMITTED": "NOOP",
    }.items():
        tx.state = state  # type: ignore[assignment]
        assert tx.recovery_action() == expect


def test_failed_records_reason():
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot())
    tx.transition("FAILED", reason="boom")
    assert tx.failure_reason == "boom"


# ── TransactionStore 持久化 ─────────────────────────────────────


def test_store_roundtrip(tmp_path: Path):
    st = TransactionStore(tmp_path / "tx")
    tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p")
    tx.transition("COMPILING")
    st.save(tx)
    loaded = st.load(tx.transaction_id)
    assert loaded is not None
    assert loaded.state == "COMPILING"


def test_list_unfinished_filters_terminal(tmp_path: Path):
    st = TransactionStore(tmp_path / "tx")
    done = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p")
    done.state = "COMMITTED"  # type: ignore[assignment]
    pending_tx = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p2")
    st.save(done)
    st.save(pending_tx)
    unfinished = st.list_unfinished()
    assert [t.transaction_id for t in unfinished] == [pending_tx.transaction_id]


def test_recovery_plan_lists_actions(tmp_path: Path):
    st = TransactionStore(tmp_path / "tx")
    hanging = KnowledgeBuildTransaction.begin(snapshot=_snapshot(), parent_wiki_version="p")
    hanging.state = "PUBLISHING"  # type: ignore[assignment]
    hanging.updated_at = "x"
    st.save(hanging)
    plan = st.recovery_plan()
    assert any(x["transaction_id"] == hanging.transaction_id for x in plan)
    assert plan[0]["action"] == "CHECK_ACTIVE_POINTER"


# ── 双快照 SourceRegistry（G-01）───────────────────────────────


def test_snapshot_pending_captures_disk_new_source(source_root, wiki_root):
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# 产品")
    pending = reg.snapshot_pending()
    assert "1-产品/overview.md" in {e.relative_path for e in pending.sources.values()}
    # 不写盘：active 仍为空
    assert reg.get_by_path("1-产品/overview.md") is None


def test_commit_snapshot_persists_active(source_root, wiki_root):
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# 产品")
    pending = reg.snapshot_pending()
    reg.commit_snapshot(pending)
    assert reg.get_by_path("1-产品/overview.md") is not None


def test_failed_publish_does_not_commit_registry(source_root, wiki_root, store):
    """G-01 核心：Publish 失败后 active Registry 不变，下一轮仍能检测变更。"""
    rel = "1-产品/overview.md"
    make_source_file(source_root, rel, "# v1")
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    reg.sync()  # active 记录 v1

    make_source_file(source_root, rel, "# v2")
    maintainer = WikiMaintainer(store, reg, publisher=_FakePublisher(ok=False))

    import asyncio

    result = asyncio.run(maintainer.run_transaction(transaction_dir=wiki_root / "_meta" / "tx"))
    assert result["published"] is False
    assert result["state"] == "FAILED"
    # active registry 未被污染，仍指向 v1
    entry = reg.get_by_path(rel)
    assert entry is not None
    assert entry.sha256 == sha256_hex("# v1")
    # 下一轮仍检测到 v2 为 changed
    next_pending = reg.snapshot_pending()
    diff = reg.snapshot_diff(next_pending)
    assert rel in diff.changed


def test_successful_publish_commits_registry(source_root, wiki_root, store):
    rel = "1-产品/overview.md"
    make_source_file(source_root, rel, "# v1")
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    reg.sync()
    make_source_file(source_root, rel, "# v2")
    maintainer = WikiMaintainer(store, reg, publisher=_FakePublisher(ok=True))

    import asyncio

    result = asyncio.run(maintainer.run_transaction(transaction_dir=wiki_root / "_meta" / "tx"))
    assert result["published"] is True
    assert result["state"] == "COMMITTED"
    assert reg.get_by_path(rel).sha256 == sha256_hex("# v2")


def test_run_transaction_idempotent_replay(source_root, wiki_root, store):
    rel = "1-产品/overview.md"
    make_source_file(source_root, rel, "# v1")
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    publisher = _FakePublisher(ok=True, wiki_version="wiki-v1")
    maintainer = WikiMaintainer(store, reg, publisher=publisher)
    tx_dir = wiki_root / "_meta" / "tx"

    import asyncio

    first = asyncio.run(maintainer.run_transaction(transaction_dir=tx_dir))
    assert first["state"] == "COMMITTED"
    assert publisher.calls == 1
    # 同一 build_id 重放 → 直接命中 COMMITTED，不重复发布
    second = asyncio.run(maintainer.run_transaction(transaction_dir=tx_dir))
    assert second["replayed"] is True
    assert publisher.calls == 1
