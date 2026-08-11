"""Eval Harness 契约 -- 阶段2 §3（WBS 2.1/2.2）。

定义真实 Agent 评测所需的 EvalCase / EvalResult / PairedEvalResult 结构，
字段对齐「03-阶段2-真实Agent评测体系.md §3」：
  - EvalCase: 输入 fixture、租户 fixture、工具边界、期望结果、预算、故障注入、评分 rubric
  - EvalResult: run manifest、输出、工具/证据 trace、终态、三层评分、Token/成本、时延、归因
  - PairedEvalResult: legacy vs candidate 同一输入的配对结果（paired comparison 的最小单元）

安全约束（对齐统一事件契约）：
  - 结果只保留工具名、参数/结果 hash 与 error code，不保存 prompt 正文与私有推理；
  - tenant_fixture 中的身份用于权限/隔离评分，不进入模型参数。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ═══════════════════════════════════════════════════════════════
# 评测分层（阶段2 §1）
# ═══════════════════════════════════════════════════════════════


class EvalLevel:
    """评测分层（L0 Contract ~ L5 Online）。"""

    L0_CONTRACT = "L0_contract"
    L1_TRACE = "L1_trace"
    L2_TASK_OUTCOME = "L2_task_outcome"
    L3_ADVERSARIAL = "L3_adversarial"
    L4_RELIABILITY = "L4_reliability"
    L5_ONLINE = "L5_online"


# ═══════════════════════════════════════════════════════════════
# EvalCase
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class EvalCase:
    """单条评测用例（不可变）。

    Attributes:
        case_id: 用例 ID（数据集内唯一）
        dataset_version: 所属数据集版本（如 "real_v1"）
        category: 场景类别（阶段2 §2.1 的 13 类）
        split: 数据切分（train/validation/holdout/safety_holdout）
        level: 评测分层（L0-L5，默认 L2）
        input_fixture: 输入快照 {question, knowledge, article, history, ...}
        tenant_fixture: 租户/权限 fixture {user_id, tenant_id, allowed_product_ids, allowed_article_hashes}
        allowed_tools: 允许使用的工具名列表
        forbidden_tools: 禁止使用的工具名列表
        expected_outcome: 期望结果描述（人工可读）
        required_facts: 必需事实（答案中必须包含的关键事实）
        required_evidence: 必需证据（必须引用/出现在 evidence trace 中的来源 ID）
        forbidden_claims: 禁止声明（答案中不得出现）
        expected_terminal_status: 期望终态（LoopStatus 值）
        max_steps: 最大步骤数
        max_tokens: 最大 Token 预算
        max_cost: 最大成本（USD）
        max_latency_ms: 最大时延（毫秒）
        fault_injection: 故障注入配置 {llm_error, tool_error, ...}
        scoring_rubric: 评分 rubric（确定性评分权重，默认按项目配置）
    """

    case_id: str
    dataset_version: str
    category: str
    split: str = "validation"
    level: str = EvalLevel.L2_TASK_OUTCOME
    input_fixture: dict[str, Any] = field(default_factory=dict)
    tenant_fixture: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    required_facts: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    forbidden_claims: list[str] = field(default_factory=list)
    expected_terminal_status: str = "completed"
    max_steps: int = 5
    max_tokens: int = 28000
    max_cost: float = 0.0
    max_latency_ms: float = 30_000.0
    fault_injection: dict[str, Any] = field(default_factory=dict)
    scoring_rubric: dict[str, Any] = field(default_factory=dict)

    # ── 便捷访问 ──────────────────────────────────────────

    @property
    def question(self) -> str:
        return str(self.input_fixture.get("question", ""))

    @property
    def user_id(self) -> str:
        return str(self.tenant_fixture.get("user_id", "eval-user"))

    @property
    def tenant_id(self) -> str:
        return str(self.tenant_fixture.get("tenant_id", ""))

    @property
    def allowed_product_ids(self) -> frozenset[str]:
        raw = self.tenant_fixture.get("allowed_product_ids", [])
        return frozenset(str(p) for p in raw)

    @property
    def allowed_article_hashes(self) -> frozenset[str]:
        raw = self.tenant_fixture.get("allowed_article_hashes", [])
        return frozenset(str(h) for h in raw)

    @property
    def tool_script(self) -> list[list[str]]:
        """模拟器工具调用剧本：每轮应调用的工具名序列（用于 mock LLM 决策）。"""
        raw = self.input_fixture.get("tool_script", [])
        return [[str(t) for t in group] for group in raw]

    def payload_hash(self) -> str:
        """输入+租户 fixture 的稳定 hash（用于数据集去重与 group split）。"""
        payload = json.dumps(
            {
                "input": self.input_fixture,
                "tenant": self.tenant_fixture,
                "tools": sorted(self.allowed_tools),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        """从 JSONL 字典构造（字段缺失使用默认值，前向兼容）。

        dataset_version 缺失时补空串，由 load_dataset 依据文件名回填版本号。
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("dataset_version", "")
        return cls(**kwargs)


# ═══════════════════════════════════════════════════════════════
# EvalResult
# ═══════════════════════════════════════════════════════════════


@dataclass
class EvalResult:
    """单次运行结果（可变，运行后填充）。

    Attributes:
        backend: "legacy" 或 "candidate"
        run_manifest: 本次运行的冻结清单（版本/模型/后端/dataset hash/时间戳）
        actual_output: 最终输出文本
        tool_trace: 工具执行 trace [{tool_name, args_hash, result_hash, error_code, duration_ms, ok}]
        evidence_trace: 证据来源 ID 列表
        terminal_status: 终态（LoopStatus 值）
        deterministic_scores: 确定性评分 {check_name: {pass, value, reason}}
        judge_scores: judge 评分 {dimension: {score, max}}
        human_scores: 人工评分（盲评后回填）{dimension: {score, max}}
        token_and_cost: {input_tokens, output_tokens, cached_input_tokens, cost_usd, usage_estimated, retries, llm_calls}
        latency_ms: 端到端时延（毫秒）
        failure_attribution: 失败归因（error code / 分类）
        llm_events: 步级事件摘要（仅 hash/名称，不落正文）
    """

    backend: str
    run_manifest: dict[str, Any] = field(default_factory=dict)
    actual_output: str = ""
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    evidence_trace: list[str] = field(default_factory=list)
    terminal_status: str = ""
    deterministic_scores: dict[str, Any] = field(default_factory=dict)
    judge_scores: dict[str, Any] = field(default_factory=dict)
    human_scores: dict[str, Any] = field(default_factory=dict)
    token_and_cost: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    failure_attribution: str = ""
    llm_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tools_used(self) -> list[str]:
        """实际使用的工具名（去重保序）。"""
        seen: list[str] = []
        for step in self.tool_trace:
            name = step.get("tool_name", "")
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def succeeded(self) -> bool:
        return self.terminal_status == "completed"

    def to_legacy_dict(self) -> dict[str, Any]:
        """转为可序列化字典（脱敏，不落正文）。"""
        return {
            "backend": self.backend,
            "run_manifest": self.run_manifest,
            "actual_output": self.actual_output,
            "tool_trace": self.tool_trace,
            "evidence_trace": self.evidence_trace,
            "terminal_status": self.terminal_status,
            "deterministic_scores": self.deterministic_scores,
            "judge_scores": self.judge_scores,
            "human_scores": self.human_scores,
            "token_and_cost": self.token_and_cost,
            "latency_ms": self.latency_ms,
            "failure_attribution": self.failure_attribution,
        }


# ═══════════════════════════════════════════════════════════════
# PairedEvalResult
# ═══════════════════════════════════════════════════════════════


@dataclass
class PairedEvalResult:
    """legacy vs candidate 配对结果（阶段2 §4：paired comparison 单元）。"""

    case_id: str
    category: str
    legacy: EvalResult
    candidate: EvalResult
    repetitions: int = 1
    position_bias: dict[str, Any] = field(default_factory=dict)

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "repetitions": self.repetitions,
            "legacy": self.legacy.to_legacy_dict(),
            "candidate": self.candidate.to_legacy_dict(),
        }


def make_run_manifest(
    *,
    backend: str,
    dataset_version: str,
    model_name: str,
    llm_backend: str,
    case_id: str,
    runner_version: str = "v1",
) -> dict[str, Any]:
    """构造每 run 冻结清单（对齐 RunManifest 语义：每 run 冻结、可追溯）。"""
    return {
        "schema_version": "2.0",
        "runner_version": runner_version,
        "created_at": datetime.now(UTC).isoformat(),
        "backend": backend,
        "dataset_version": dataset_version,
        "case_id": case_id,
        "model_name": model_name,
        "llm_backend": llm_backend,
    }
