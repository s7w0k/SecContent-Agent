"""真实双轨 Eval Runner -- 阶段2 §4（WBS 2.1）。

对同一 EvalCase 运行 legacy（单轮问答）与 candidate（AgentLoop 全状态机）：

  - 固定输入快照与外部工具 fixture：工具为确定性 FixtureTool，数据来自
    case.input_fixture（同版本知识/文章/记忆），保证跨 run 可重复；
  - 模拟器后端（mock）真实驱动 AgentLoop：预留/结算/工具执行/事件/终态全部
    真实执行，不注入"必然通过"的结果；评测反映状态机正确性；
  - 真实后端（real）通过 LangChain ChatOpenAI 构造，支持真实模型评测；
  - 每个候选按 n_runs 重复运行（默认 3 次），由调用方聚合为
    paired comparison（均值/p50/p95/bootstrap CI）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import uuid4

from agent.agent_contracts import LoopBudget, LoopStatus, RunContext, TypedToolResult
from agent.agent_loop import AgentLoop
from agent.evals.contracts import EvalCase, EvalResult, PairedEvalResult, make_run_manifest
from agent.evals.mock_llm import (
    MockLegacyLLM,
    MockToolLLM,
    default_answer_builder,
)
from agent.llm_wrapper import LLMWrapper
from agent.pricing_catalog import compute_cost
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.evals.runner")

CANDIDATE_SYSTEM_PROMPT = (
    "你是智能体安全行业的 PR 情报分析助手。\n"
    "如果需要产品知识、文章详情或用户偏好，使用提供的工具查询。\n"
    "只能基于工具返回的信息和已提供的上下文回答，不确定时说明缺少依据。\n"
    "输出中文，简洁有力。"
)

LEGACY_SYSTEM_PROMPT = "你是智能体安全行业的 PR 情报分析助手，请简洁回答。"


class FixtureTool:
    """确定性工具 fixture：返回 case.input_fixture 中固定数据（外部工具快照）。

    支持 fault_injection.tool_error：模拟工具故障（timeout/error），
    用于 budget_limits 与 reliability 类用例。
    """

    def __init__(
        self,
        name: str,
        data: str,
        *,
        source_ids: list[str] | None = None,
        fault: dict[str, Any] | None = None,
        delay_ms: float = 0.0,
    ):
        self.name = name
        self.data = data
        self.source_ids = source_ids or []
        self.fault = fault or {}
        self.delay_ms = delay_ms

    async def ainvoke(self, args: dict[str, Any] | None = None, **kwargs: Any) -> TypedToolResult:
        """返回结构化结果（携带 source_ids，供证据评分与缓存使用）。

        兼容两种调用形态：ToolExecutor 以位置参数 tool.ainvoke(dict(args))
        调用，LangChain 风格工具也可能传关键字参数。
        """
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000)
        if self.fault.get("tool_error") == "timeout":
            await asyncio.sleep(5)  # 触发 AgentLoop 工具超时
            return TypedToolResult(
                ok=False,
                error="tool timeout",
                error_code="timeout",
                source_ids=list(self.source_ids),
            )
        if self.fault.get("tool_error"):
            return TypedToolResult(
                ok=False,
                error=f"mock tool error: {self.fault['tool_error']}",
                error_code="tool_error",
                source_ids=list(self.source_ids),
            )
        return TypedToolResult(
            ok=True,
            data=self.data,
            source_ids=list(self.source_ids),
            char_count=len(self.data),
        )


def _tool_fixture_data(case: EvalCase, tool_name: str) -> tuple[str, list[str]]:
    """从 case.input_fixture 取工具数据（同版本知识/文章/记忆快照）。"""
    fixture = case.input_fixture
    if tool_name == "search_knowledge":
        return (
            str(fixture.get("knowledge", "产品知识：暂无该产品条目。")),
            [str(s) for s in fixture.get("knowledge_source_ids", ["kb/overview"])],
        )
    if tool_name == "get_article":
        return (
            str(fixture.get("article", "文章内容：暂无该文章。")),
            [str(s) for s in fixture.get("article_source_ids", ["article/001"])],
        )
    if tool_name == "retrieve_memory":
        return (
            str(fixture.get("memory", "用户偏好：暂无记忆。")),
            [str(s) for s in fixture.get("memory_source_ids", ["memory/001"])],
        )
    return (f"{tool_name} 结果：{case.question}", [f"tool/{tool_name}"])


def _build_fixture_tools(case: EvalCase) -> list[FixtureTool]:
    tools: list[FixtureTool] = []
    for name in case.allowed_tools:
        data, source_ids = _tool_fixture_data(case, name)
        tools.append(
            FixtureTool(
                name,
                data,
                source_ids=source_ids,
                fault=case.fault_injection.get("tool_error", {}) if case.fault_injection else None,
                delay_ms=float(case.fault_injection.get("tool_delay_ms", 0) or 0),
            )
        )
    return tools


def _case_final_answer(case: EvalCase) -> str:
    """case 可配置的模拟器最终答案（安全拒绝 / 证据不足等场景）。"""
    return str(case.input_fixture.get("mock_answer", ""))


class EvalRunner:
    """真实双轨评测运行器。

    Args:
        llm_backend: "mock"（确定性模拟器，CI 可重复）| "real"（真实模型，需 API key）
        model_name: 模型名（成本计价与 manifest）
        n_runs: 每个 case 每后端重复次数（默认 3，对齐阶段2 §4）
        db: 可选 MongoDB（AgentLoop 事件落库）
    """

    def __init__(
        self,
        *,
        llm_backend: str = "mock",
        model_name: str = "deepseek-chat",
        n_runs: int = 3,
        db: Any = None,
    ):
        self.llm_backend = llm_backend
        self.model_name = model_name
        self.n_runs = n_runs
        self.db = db
        self._real_llm: Any = None

    # ── 对外入口 ─────────────────────────────────────────────

    async def run_pairs(self, cases: list[EvalCase]) -> list[PairedEvalResult]:
        """对全部用例执行双轨 paired 运行（每个 case 重复 n_runs 次）。"""
        pairs: list[PairedEvalResult] = []
        for case in cases:
            for rep in range(self.n_runs):
                pairs.append(await self.run_pair(case, repetition=rep))
        return pairs

    async def run_pair(self, case: EvalCase, *, repetition: int = 0) -> PairedEvalResult:
        """单次双轨配对（同输入、同 fixture）。"""
        legacy = await self._run_legacy(case)
        candidate = await self._run_candidate(case)
        return PairedEvalResult(
            case_id=case.case_id,
            category=case.category,
            legacy=legacy,
            candidate=candidate,
            repetitions=repetition,
        )

    # ── legacy 路径（单轮问答） ───────────────────────────────

    async def _run_legacy(self, case: EvalCase) -> EvalResult:
        manifest = make_run_manifest(
            backend="legacy",
            dataset_version=case.dataset_version,
            model_name=self.model_name,
            llm_backend=self.llm_backend,
            case_id=case.case_id,
        )
        started = time.perf_counter()
        error_type = ""
        answer = ""
        usage: dict[str, Any] = {}
        try:
            llm = self._legacy_llm(case)
            response = await llm.ainvoke(
                [
                    SystemMessage(content=LEGACY_SYSTEM_PROMPT),
                    HumanMessage(content=case.question),
                ]
            )
            answer = str(response.content or "") if hasattr(response, "content") else ""
            usage = getattr(response, "usage_metadata", None) or {}
        except Exception as exc:  # 评测需记录任意失败
            error_type = type(exc).__name__

        latency_ms = (time.perf_counter() - started) * 1000
        input_tokens = int(usage.get("input_tokens") or max(1, len(case.question) // 4))
        output_tokens = int(usage.get("output_tokens") or max(1, len(answer) // 4) if answer else 0)
        estimated = not bool(usage.get("input_tokens"))
        cost = compute_cost(
            self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=int(usage.get("input_token_details", {}).get("cache_read", 0))
            if isinstance(usage.get("input_token_details"), dict)
            else 0,
            input_tokens_estimated=estimated,
            output_tokens_estimated=estimated,
        )
        status = (
            LoopStatus.COMPLETED.value
            if answer and not error_type
            else LoopStatus.FAILED.value
        )
        return EvalResult(
            backend="legacy",
            run_manifest=manifest,
            actual_output=answer,
            terminal_status=status,
            evidence_trace=list(case.required_evidence),  # legacy 注入上下文即视为可用证据
            token_and_cost={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": 0,
                "cost_usd": cost["cost_usd"],
                "usage_estimated": estimated,
                "llm_calls": 1,
                "retries": 0,
            },
            latency_ms=latency_ms,
            failure_attribution=error_type or ("empty_answer" if not answer else ""),
        )

    # ── candidate 路径（AgentLoop 全状态机） ──────────────────

    async def _run_candidate(self, case: EvalCase) -> EvalResult:
        manifest = make_run_manifest(
            backend="candidate",
            dataset_version=case.dataset_version,
            model_name=self.model_name,
            llm_backend=self.llm_backend,
            case_id=case.case_id,
        )
        llm = self._agent_llm(case)
        wrapper = LLMWrapper(llm=llm, db=self.db)
        tools = _build_fixture_tools(case)
        budget = self._budget_from_case(case)
        # 无 finalization 预留用例：预算耗尽时返回结构化 budget_exhausted，不调模型
        budget_plan: Any = None
        if case.input_fixture.get("no_finalization_reserve"):
            from agent.budget_manager import BudgetPlan

            budget_plan = BudgetPlan(
                max_input_tokens=max(100, case.max_tokens),
                max_output_tokens=max(50, case.max_tokens // 4),
                max_steps=max(1, case.max_steps),
                max_tool_calls=max(1, case.max_steps * 2),
                max_runtime_seconds=max(1, min(int(case.max_latency_ms / 1000) + 1, 60)),
                finalization_reserve_tokens=0,
            )
        run_context = RunContext(
            trace_id=f"eval-{uuid4().hex[:12]}",
            run_id=f"eval-{uuid4().hex[:12]}",
            user_id=case.user_id,
            allowed_product_ids=case.allowed_product_ids,
            allowed_article_hashes=case.allowed_article_hashes,
            tenant_id=case.tenant_id or None,
        )

        loop = AgentLoop(
            llm_wrapper=wrapper,
            tools=tools,
            budget=budget,
            run_context=run_context,
            budget_plan=budget_plan,
        )
        started = time.perf_counter()
        try:
            result = await loop.run(
                system_prompt=CANDIDATE_SYSTEM_PROMPT,
                user_message=case.question,
                initial_context=str(case.input_fixture.get("context", "")),
            )
            latency_ms = (time.perf_counter() - started) * 1000
        except Exception as exc:  # Loop 自身异常也计入评测
            latency_ms = (time.perf_counter() - started) * 1000
            return EvalResult(
                backend="candidate",
                run_manifest=manifest,
                terminal_status=LoopStatus.FAILED.value,
                token_and_cost={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
                latency_ms=latency_ms,
                failure_attribution=f"loop_exception:{type(exc).__name__}",
            )

        usage = result.usage
        cost = compute_cost(
            self.model_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_tokens_estimated=False,
            output_tokens_estimated=False,
        )
        tool_trace = self._extract_tool_trace(result)
        return EvalResult(
            backend="candidate",
            run_manifest=manifest,
            actual_output=result.answer,
            tool_trace=tool_trace,
            evidence_trace=list(result.references),
            terminal_status=result.status.value,
            token_and_cost={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": 0,
                "cost_usd": cost["cost_usd"],
                "usage_estimated": False,
                "llm_calls": usage.rounds,
                "retries": 0,
            },
            latency_ms=latency_ms,
            failure_attribution=result.degrade_reason or (
                "" if result.ok else result.status.value
            ),
            llm_events=[e.to_log_dict() for e in result.events],
        )

    # ── 内部构造 ─────────────────────────────────────────────

    def _budget_from_case(self, case: EvalCase) -> LoopBudget:
        return LoopBudget(
            max_rounds=max(1, case.max_steps),
            max_input_tokens=max(100, case.max_tokens),
            max_output_tokens=max(50, case.max_tokens // 4),
            max_tool_calls=max(1, case.max_steps * 2),
            max_parallel_tools=3,
            deadline_seconds=max(1, min(int(case.max_latency_ms / 1000) + 1, 60)),
            tool_timeout_seconds=3,
            max_cost_usd=case.max_cost,
        )

    def _legacy_llm(self, case: EvalCase) -> Any:
        if self.llm_backend == "real":
            return self._real_model()
        return MockLegacyLLM(
            model_name=self.model_name,
            answer_builder=self._legacy_answer_builder(case),
            final_answer=_case_final_answer(case),
            fault=case.fault_injection,
        )

    def _agent_llm(self, case: EvalCase) -> Any:
        if self.llm_backend == "real":
            return self._real_model()
        return MockToolLLM(
            model_name=self.model_name,
            tool_script=case.tool_script,
            answer_builder=self._candidate_answer_builder(case),
            final_answer=_case_final_answer(case),
            fault=case.fault_injection,
        )

    def _real_model(self) -> Any:
        if self._real_llm is None:
            from config import get_settings
            from langchain_openai import ChatOpenAI

            s = get_settings()
            self._real_llm = ChatOpenAI(
                model=self.model_name,
                api_key=s.DEEPSEEK_API_KEY,
                base_url=s.DEEPSEEK_BASE_URL,
                timeout=s.DEEPSEEK_TIMEOUT,
            )
        return self._real_llm

    def _legacy_answer_builder(self, case: EvalCase):
        def _build(_question: str, _facts: list[str]) -> str:
            if case.category in ("security", "permissions"):
                return "该操作存在安全或权限风险，无法执行。"
            return default_answer_builder(_question, case.required_facts)

        return _build

    def _candidate_answer_builder(self, case: EvalCase):
        def _build(_question: str, _facts: list[str]) -> str:
            if case.category in ("security", "permissions"):
                return "该操作存在安全或权限风险，无法执行。"
            return default_answer_builder(_question, case.required_facts)

        return _build

    @staticmethod
    def _extract_tool_trace(result: Any) -> list[dict[str, Any]]:
        """从 LoopResult.events 提取工具执行 trace（仅指纹与状态，不落正文）。"""
        trace: list[dict[str, Any]] = []
        for ev in result.events:
            ev_type = getattr(ev, "type", None)
            name = getattr(ev_type, "value", "")
            if name not in ("tool_finished", "tool_failed", "tool_blocked"):
                continue
            trace.append(
                {
                    "tool_name": getattr(ev, "tool_name", ""),
                    "args_hash": getattr(ev, "tool_args_hash", ""),
                    "result_hash": getattr(ev, "tool_result_hash", ""),
                    "error_code": getattr(ev, "error_code", ""),
                    "duration_ms": getattr(ev, "duration_ms", 0),
                    "ok": name == "tool_finished",
                }
            )
        return trace
