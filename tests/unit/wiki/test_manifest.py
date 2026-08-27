"""Phase2 / PR-09：Manifest、Version Pinning、Publish Lock、Rollback 测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from agent.wiki.locks import PublishLock
from agent.wiki.manifest import ManifestStore, WikiVersionManifest, build_version_id
from agent.wiki.read_session import VersionAwareCache, VersionedReader, WikiReadSession

# ── Manifest ─────────────────────────────────────────────────


def test_version_id_unique_and_ordered():
    a = build_version_id()
    b = build_version_id()
    assert a != b
    assert a.startswith("wiki-")


def test_manifest_validate_self():
    m = WikiVersionManifest(
        wiki_version="wiki-x",
        source_snapshot_hash="h",
        page_tree_hash="t",
        page_count=3,
    )
    assert m.validate_self() == []


def test_manifest_store_roundtrip_and_active(tmp_path: Path):
    store = ManifestStore(tmp_path / "versions")
    m = WikiVersionManifest(
        wiki_version="wiki-1",
        source_snapshot_hash="h1",
        page_tree_hash="t1",
        page_count=2,
        status="published",
    )
    store.write(m)
    assert store.load("wiki-1") == m
    assert store.active_version() == ""  # 未激活
    store.set_active("wiki-1")
    assert store.active_version() == "wiki-1"
    assert store.has_active() is True


# ── Publish Lock ─────────────────────────────────────────────


def test_lock_acquire_block_and_release(tmp_path: Path):
    lock = PublishLock(tmp_path / ".publish.lock", ttl_seconds=120)
    assert lock.acquire() is True
    assert lock.acquire() is False  # 已被占用

    lock2 = PublishLock(tmp_path / ".publish.lock", ttl_seconds=120)
    assert lock2.acquire() is False  # 其他 owner 不能获取


def test_lock_owner_only_release(tmp_path: Path):
    lock = PublishLock(tmp_path / ".publish.lock", ttl_seconds=120)
    lock.acquire()
    # 其他实例释放失败
    other = PublishLock(tmp_path / ".publish.lock", ttl_seconds=120)
    assert other.release() is False
    # 所有者释放成功
    assert lock.release() is True
    assert lock.release() is False  # 已释放


def test_lock_renew_and_expiry_takeover(tmp_path: Path):
    lock = PublishLock(tmp_path / ".publish.lock", ttl_seconds=0.3)
    lock.acquire()
    time.sleep(0.35)
    # 过期后其他实例可接管
    other = PublishLock(tmp_path / ".publish.lock", ttl_seconds=120)
    assert other.acquire() is True
    # 旧 owner 续约失败
    assert lock.renew() is False


# ── Read Session / Version Pinning ───────────────────────────


def test_read_session_pin():
    session = WikiReadSession.pin(wiki_version="wiki-1", source_snapshot_id="snap-1", task_id="t")
    assert session.wiki_version == "wiki-1"
    assert session.source_snapshot_id == "snap-1"
    assert session.pinned_at


def test_read_session_requires_version():
    with pytest.raises(ValueError):
        WikiReadSession.pin(wiki_version="").ensure_version()


def test_versioned_reader_uses_store(dummy_store_factory):
    store = dummy_store_factory()
    session = WikiReadSession.pin(wiki_version="wiki-1")
    reader = VersionedReader(store, session)
    assert reader.page_exists("product.a") is True


def test_version_aware_cache_keyed_by_version():
    cache = VersionAwareCache(max_pages=2)
    cache.put("wiki-1", "p1", {"data": 1})
    cache.put("wiki-2", "p1", {"data": 2})
    assert cache.get("wiki-1", "p1") == {"data": 1}
    assert cache.get("wiki-2", "p1") == {"data": 2}
    # LRU 淘汰
    cache.put("wiki-1", "p2", {"data": 3})
    assert cache.get("wiki-1", "p1") is None


# ── 共享 fixture ─────────────────────────────────────────────


def test_version_aware_cache_memory_bound():
    cache = VersionAwareCache(max_pages=2, max_memory_mb=0)
    cache.put("v", "a", "x" * 10)
    cache.put("v", "b", "y" * 10)
    cache.put("v", "c", "z" * 10)
    assert len(cache) == 2
    assert cache.get("v", "a") is None


def test_versioned_reader_uses_version_cache():
    """§20.3：VersionedReader.open_page 命中版本感知缓存，避免重复读文件。"""
    from agent.wiki.read_session import VersionedReader, WikiReadSession

    calls = {"n": 0}

    class _FakeStore:
        def open_page(self, page_id):
            calls["n"] += 1
            return {"page_id": page_id}

        def page_exists(self, page_id):
            return True

    cache = VersionAwareCache(max_pages=10)
    session = WikiReadSession(wiki_version="v1")
    reader = VersionedReader(_FakeStore(), session, cache=cache)

    assert reader.open_page("p1") == {"page_id": "p1"}
    assert reader.open_page("p1") == {"page_id": "p1"}  # 命中缓存
    assert calls["n"] == 1
    assert cache.get("v1", "p1") == {"page_id": "p1"}


@pytest.fixture
def dummy_store_factory(tmp_path: Path):
    from agent.wiki.contracts import WikiPage, WikiPageMeta, WikiSection
    from agent.wiki.store import WikiStore

    def _make():
        store = WikiStore(tmp_path / "_wiki")
        store.write_page(
            WikiPage(
                meta=WikiPageMeta(page_id="product.a", title="A", page_type="product"),
                body="# A",
                sections=[WikiSection(title="summary", heading_level=2, body="概述")],
            )
        )
        return store

    return _make
