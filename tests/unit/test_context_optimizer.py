"""Token 优化单元测试 -- 阶段1 3.2 / 3.4 节（工具结果缓存与压缩）。"""

from __future__ import annotations

import time

from agent.context_optimizer import ContextCompressor, ToolResultCache


class TestToolResultCache:
    def test_hit_and_miss(self):
        cache = ToolResultCache()
        assert cache.get(user_id="u1", tool_name="t", args={"a": 1}) is None
        cache.set(
            user_id="u1",
            tool_name="t",
            args={"a": 1},
            content="data",
            source_ids=["s1"],
            result_hash="h1",
        )
        hit = cache.get(user_id="u1", tool_name="t", args={"a": 1})
        assert hit is not None
        assert hit.content == "data"
        assert hit.source_ids == ("s1",)

    def test_cross_user_isolation(self):
        cache = ToolResultCache()
        cache.set(user_id="u1", tool_name="t", args={}, content="secret")
        assert cache.get(user_id="u2", tool_name="t", args={}) is None

    def test_tenant_isolation(self):
        cache = ToolResultCache()
        cache.set(tenant_id="t1", user_id="u1", tool_name="t", args={}, content="a")
        assert cache.get(tenant_id="t2", user_id="u1", tool_name="t", args={}) is None
        assert cache.get(tenant_id="t1", user_id="u1", tool_name="t", args={}) is not None

    def test_invalidate(self):
        cache = ToolResultCache()
        cache.set(user_id="u1", tool_name="t", args={}, content="a")
        assert cache.invalidate(user_id="u1", tool_name="t", args={})
        assert cache.get(user_id="u1", tool_name="t", args={}) is None

    def test_invalidate_all_per_user(self):
        cache = ToolResultCache()
        cache.set(user_id="u1", tool_name="t1", args={}, content="a")
        cache.set(user_id="u1", tool_name="t2", args={}, content="b")
        cache.set(user_id="u2", tool_name="t1", args={}, content="c")
        removed = cache.invalidate_all(user_id="u1")
        assert removed == 2
        assert cache.size() == 1

    def test_ttl_expiry(self):
        cache = ToolResultCache(ttl_seconds=1)
        cache.set(user_id="u1", tool_name="t", args={}, content="a")
        key = next(iter(cache._entries))
        cache._entries[key] = type(cache._entries[key])(
            content="a",
            source_ids=(),
            result_hash="",
            created_at=time.monotonic() - 10,  # 强制过期
        )
        assert cache.get(user_id="u1", tool_name="t", args={}) is None

    def test_max_entries_eviction(self):
        cache = ToolResultCache(max_entries=2)
        cache.set(user_id="u1", tool_name="t", args={"n": 1}, content="a")
        cache.set(user_id="u1", tool_name="t", args={"n": 2}, content="b")
        cache.set(user_id="u1", tool_name="t", args={"n": 3}, content="c")
        assert cache.size() == 2


class TestContextCompressor:
    def test_dedup_same_hash(self):
        compressor = ContextCompressor()
        first = compressor.compress_tool_result(
            content="long content here", result_hash="h1", source_ids=["s1"]
        )
        assert first.content == "long content here"
        second = compressor.compress_tool_result(
            content="long content here", result_hash="h1", source_ids=["s1"]
        )
        assert "已获取" in second.content
        assert second.truncated

    def test_truncate_long_result(self):
        compressor = ContextCompressor(max_tool_result_chars=64)
        summary = compressor.compress_tool_result(
            content="a" * 100, result_hash="h2", source_ids=["s1"]
        )
        assert summary.truncated
        assert summary.original_chars == 100
        assert len(summary.content) < 100
        assert "truncated" in summary.content

    def test_summarize_history(self):
        compressor = ContextCompressor()
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        text = compressor.summarize_history(history, keep_recent=2)
        assert "用户: q1" in text
        assert "助手: a1" in text

    def test_summarize_history_truncate(self):
        compressor = ContextCompressor(max_history_chars=128)
        history = [{"role": "user", "content": "x" * 200}]
        text = compressor.summarize_history(history)
        assert len(text) < 200
        assert "历史已压缩" in text
