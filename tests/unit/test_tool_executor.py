"""ToolExecutor 单元测试 -- 阶段1 1.2 节（同轮多工具并发）。"""

from __future__ import annotations

import asyncio
import time

import pytest


class SimpleTool:
    """可配置延迟/异常/Schema 的最小工具。"""

    def __init__(self, name, result="ok", delay=0.0, error=None, args_schema=None):
        self.name = name
        self.result = result
        self.delay = delay
        self.error = error
        self.args_schema = args_schema
        self.calls = 0
        self.started = asyncio.Event()

    async def ainvoke(self, args):
        self.calls += 1
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


class _Policy:
    """带 depends_on 的测试策略对象（鸭子类型 ToolPolicy）。"""

    def __init__(
        self,
        name,
        timeout_seconds=5,
        requires_product_allowlist=False,
        requires_article_allowlist=False,
        depends_on=(),
    ):
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.requires_product_allowlist = requires_product_allowlist
        self.requires_article_allowlist = requires_article_allowlist
        self.depends_on = depends_on


class _RunContext:
    def __init__(self, allowed_products=frozenset(), allowed_articles=frozenset()):
        self.allowed_products = allowed_products
        self.allowed_articles = allowed_articles

    def is_product_allowed(self, product_id):
        return product_id in self.allowed_products

    def is_article_allowed(self, url_hash):
        return url_hash in self.allowed_articles


def _make_executor(tools, policies=None, **kw):
    from agent.tool_executor import ToolExecutor

    return ToolExecutor(
        tools_by_name={t.name: t for t in tools},
        tool_policies=policies or {},
        max_parallel_tools=kw.get("max_parallel_tools", 3),
        run_context=kw.get("run_context"),
        tenant_id=kw.get("tenant_id", ""),
    )


def _tool_call(name, call_id, args=None):
    return {"name": name, "id": call_id, "args": args or {}, "type": "tool_call"}


class TestConcurrentExecution:
    @pytest.mark.asyncio
    async def test_two_tools_run_concurrently(self):
        ta = SimpleTool("tool_a", delay=0.2)
        tb = SimpleTool("tool_b", delay=0.2)
        executor = _make_executor([ta, tb])

        started = time.perf_counter()
        results = await executor.execute_many(
            [_tool_call("tool_a", "c1"), _tool_call("tool_b", "c2")]
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 0.35  # 真实并发：总时长接近单工具而非叠加
        assert [r.tool_call_id for r in results] == ["c1", "c2"]  # 按原顺序回填
        assert all(r.ok for r in results)
        assert ta.calls == 1 and tb.calls == 1

    @pytest.mark.asyncio
    async def test_one_failure_does_not_cancel_sibling(self):
        ta = SimpleTool("tool_a", error=ValueError("boom"))
        tb = SimpleTool("tool_b", result="fine")
        executor = _make_executor([ta, tb])

        results = await executor.execute_many(
            [_tool_call("tool_a", "c1"), _tool_call("tool_b", "c2")]
        )
        by_id = {r.tool_call_id: r for r in results}
        assert not by_id["c1"].ok
        assert by_id["c1"].error_code == "ValueError"
        assert by_id["c2"].ok
        assert tb.calls == 1


class TestOrderAndBudget:
    @pytest.mark.asyncio
    async def test_budget_exhausted_not_executed(self):
        ta = SimpleTool("tool_a")
        tb = SimpleTool("tool_b")
        executor = _make_executor([ta, tb])

        results = await executor.execute_many(
            [_tool_call("tool_a", "c1"), _tool_call("tool_b", "c2")],
            remaining_tool_budget=1,
        )
        by_id = {r.tool_call_id: r for r in results}
        assert by_id["c1"].ok
        assert not by_id["c2"].ok
        assert by_id["c2"].error_code == "budget_exhausted"
        assert tb.calls == 0

    @pytest.mark.asyncio
    async def test_unknown_tool_blocked(self):
        executor = _make_executor([])
        results = await executor.execute_many([_tool_call("nope", "c1")])
        assert len(results) == 1
        assert not results[0].ok
        assert results[0].error_code == "tool_not_found"


class TestPolicy:
    @pytest.mark.asyncio
    async def test_product_allowlist_denied(self):
        ta = SimpleTool("search")
        policy = {
            "search": _Policy(
                "search",
                requires_product_allowlist=True,
            )
        }
        executor = _make_executor(
            [ta], policies=policy, run_context=_RunContext(allowed_products=frozenset())
        )
        results = await executor.execute_many([_tool_call("search", "c1", {"product_id": "p1"})])
        assert not results[0].ok
        assert results[0].error_code == "policy_denied:denied_policy"
        assert ta.calls == 0

    @pytest.mark.asyncio
    async def test_timeout(self):
        ta = SimpleTool("slow", delay=10)
        policy = {"slow": _Policy("slow", timeout_seconds=1)}
        executor = _make_executor([ta], policies=policy)
        results = await executor.execute_many([_tool_call("slow", "c1")])
        assert not results[0].ok
        assert results[0].error_code == "timeout"


class TestDependencyGroups:
    @pytest.mark.asyncio
    async def test_dependent_tools_serialized(self):
        ta = SimpleTool("tool_a", result="A")
        tb = SimpleTool("tool_b", result="B")
        policy = {
            "tool_a": _Policy("tool_a"),
            "tool_b": _Policy("tool_b", depends_on=("tool_a",)),
        }
        executor = _make_executor([ta, tb], policies=policy)

        results = await executor.execute_many(
            [_tool_call("tool_b", "c_b"), _tool_call("tool_a", "c_a")]
        )
        by_id = {r.tool_call_id: r for r in results}
        assert by_id["c_a"].ok and by_id["c_b"].ok
        assert ta.calls == 1 and tb.calls == 1

    def test_dependency_groups_topology(self):
        ta = SimpleTool("tool_a")
        tb = SimpleTool("tool_b")
        tc = SimpleTool("tool_c")
        policy = {
            "tool_a": _Policy("tool_a"),
            "tool_b": _Policy("tool_b", depends_on=("tool_a",)),
            "tool_c": _Policy("tool_c"),
        }
        executor = _make_executor([ta, tb, tc], policies=policy)
        specs = executor._dependency_groups(
            [
                executor._validate(name="tool_c", call_id="c_c", args={}, args_hash="h"),
                executor._validate(name="tool_b", call_id="c_b", args={}, args_hash="h"),
                executor._validate(name="tool_a", call_id="c_a", args={}, args_hash="h"),
            ]
        )
        # 第一层包含无依赖的 tool_a / tool_c；tool_b 依赖 tool_a 在第二层
        layer1 = {s.tool_name for s in specs[0]}
        assert "tool_a" in layer1 and "tool_c" in layer1
        assert "tool_b" not in layer1
        assert specs[1][0].tool_name == "tool_b"
