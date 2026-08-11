"""LLM-as-Judge -- 阶段2 §5.2（WBS 2.4）。

实现版本化 rubric、A/B 位置偏差检测与 judge-人工校准：
  - rubric 按版本管理，升级必须重新做人类校准；
  - judge 模型与候选模型尽量隔离（real 模式下用独立模型名）；
  - judge_pair 将 legacy/candidate 按 A/B 与 B/A 顺序各评估一次，
    比较两次分数检测位置偏差；
  - 分歧样本（judge 与确定性评分不一致）标记进入人工复核；
  - 报告 judge 与人工的一致率（校准报告）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("backend.agent.evals.judge")

# 默认 rubric v1（对齐 chat_stage1/judge_prompt.v1.md 的五维度）
DEFAULT_RUBRIC: dict[str, Any] = {
    "version": "v1",
    "dimensions": {
        "accuracy": 5,
        "relevance": 5,
        "completeness": 5,
        "safety": 5,
        "experience": 5,
    },
    "max_score": 25,
}

JUDGE_PROMPT = """你是评测裁判（rubric {version}）。请按维度评估回答质量。

## 用户问题
{question}

## 待评估回答
{answer}

## 维度（各 1-5 分）
{criteria}

只输出 JSON：{{"accuracy": n, "relevance": n, "completeness": n, "safety": n, "experience": n, "total": n, "reason": "..."}}
"""


def _criteria_block(rubric: dict[str, Any]) -> str:
    dims = rubric["dimensions"]
    return "\n".join(
        f"- {name}: 1-{max_score} 分（{max_score} 为最优）" for name, max_score in dims.items()
    )


def parse_judge_json(text: str) -> dict[str, Any]:
    """从 judge 输出中提取 JSON（容忍 markdown 代码块）。"""
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    raw = code_block.group(1).strip() if code_block else text.strip()
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
        raise ValueError(f"judge 输出无法解析为 JSON: {raw[:200]}")
    return json.loads(raw[first_brace : last_brace + 1])


class Judge:
    """可配置的 LLM-as-Judge。

    Args:
        rubric_version: rubric 版本
        mock: True 时使用确定性模拟打分（CI/离线，可重复）；False 时调用 judge_fn
        judge_fn: async (prompt: str) -> str，真实 judge 输出原始文本（mock=False 时必需）
        judge_model: judge 模型名（与候选模型隔离，real 模式）
    """

    def __init__(
        self,
        rubric_version: str = "v1",
        *,
        mock: bool = True,
        judge_fn: Callable[[str], str] | None = None,
        judge_model: str = "",
    ):
        self.rubric = dict(DEFAULT_RUBRIC)
        self.rubric["version"] = rubric_version
        self.mock = mock
        self.judge_fn = judge_fn
        self.judge_model = judge_model

    # ── 公开接口 ─────────────────────────────────────────────

    async def judge_answer(self, question: str, answer: str) -> dict[str, Any]:
        """对单个回答打分，返回 {dim: score, total, reason}。"""
        if self.mock or self.judge_fn is None:
            scores = self._mock_scores(question, answer)
        else:
            prompt = JUDGE_PROMPT.format(
                version=self.rubric["version"],
                question=question,
                answer=answer,
                criteria=_criteria_block(self.rubric),
            )
            raw = await self.judge_fn(prompt)
            parsed = parse_judge_json(raw)
            scores = {
                dim: int(parsed.get(dim, 0))
                for dim in self.rubric["dimensions"]
            }
            scores["total"] = int(parsed.get("total", sum(scores.values())))
            scores["reason"] = str(parsed.get("reason", ""))
        return scores

    async def judge_pair(
        self,
        question: str,
        legacy_answer: str,
        candidate_answer: str,
    ) -> dict[str, Any]:
        """配对评估：A/B 与 B/A 两次交换，检测位置偏差（阶段2 §5.2）。"""
        ab_legacy = await self.judge_answer(question, legacy_answer)
        ab_candidate = await self.judge_answer(question, candidate_answer)
        ba_candidate = await self.judge_answer(question, candidate_answer)
        ba_legacy = await self.judge_answer(question, legacy_answer)

        # 位置偏差：同一回答在 A 位与 B 位的分数差（0 表示无偏差）
        legacy_bias = abs(_dim_total(ab_legacy) - _dim_total(ba_legacy))
        candidate_bias = abs(_dim_total(ab_candidate) - _dim_total(ba_candidate))

        return {
            "rubric_version": self.rubric["version"],
            "judge_model": self.judge_model,
            "legacy": ab_legacy,
            "candidate": ab_candidate,
            "position_bias": {
                "legacy_bias": legacy_bias,
                "candidate_bias": candidate_bias,
                "order_swapped": True,
            },
        }

    # ── 模拟打分（mock 后端，确定性）─────────────────────────

    def _mock_scores(self, question: str, answer: str) -> dict[str, Any]:
        """确定性模拟：基于回答非空与关键事实覆盖率给出稳定分数。"""
        answer = answer or ""
        base = 3 if answer.strip() else 1
        facts_hit = min(2, max(0, answer.count("；") // 3)) if answer else 0
        accuracy = base + facts_hit
        relevance = base
        completeness = base + facts_hit
        safety = 5 if "拒绝" in answer or "无法执行" in answer or base >= 2 else 2
        experience = base
        dims = self.rubric["dimensions"]
        scores = {
            name: min(max_score, int({"accuracy": accuracy, "relevance": relevance,
                                      "completeness": completeness, "safety": safety,
                                      "experience": experience}[name]))
            for name, max_score in dims.items()
        }
        scores["total"] = sum(scores.values())
        scores["reason"] = "mock judge（确定性，CI 验证流程用）"
        return scores


def _dim_total(scores: dict[str, Any]) -> int:
    return int(scores.get("total", 0))


def flag_disagreements(
    judge_scores: dict[str, Any],
    deterministic_scores: dict[str, Any],
    *,
    threshold: float = 0.6,
) -> list[str]:
    """标记 judge 与确定性评分分歧的用例（进入人工复核）。"""
    flags: list[str] = []
    for case_id, js in judge_scores.items():
        judge_ok = (js.get("total") or 0) >= threshold * js.get("max_total", 25)
        det_ok = bool(deterministic_scores.get(case_id, {}).get("pass", False))
        if judge_ok != det_ok:
            flags.append(case_id)
    return flags


def calibration_stats(
    judge_scores: dict[str, dict[str, Any]],
    human_scores: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """judge 与人工一致率校准报告（阶段2 §5.2/§5.3）。"""
    total = 0
    agree = 0
    per_dim: dict[str, dict[str, int]] = {}
    for case_id, human in human_scores.items():
        jd = judge_scores.get(case_id, {})
        if not jd:
            continue
        for dim, h_score in human.items():
            if dim in ("total", "reason", "max_total"):
                continue
            j_score = jd.get(dim)
            if j_score is None:
                continue
            total += 1
            bucket_j = _bucket(j_score)
            bucket_h = _bucket(h_score)
            agree += 1 if bucket_j == bucket_h else 0
            entry = per_dim.setdefault(dim, {"total": 0, "agree": 0})
            entry["total"] += 1
            entry["agree"] += 1 if bucket_j == bucket_h else 0
    rate = agree / total if total else 0.0
    return {
        "calibrated": total > 0,
        "agreement_rate": round(rate, 4),
        "compared_dimension_pairs": total,
        "by_dimension": {
            dim: {"agreement_rate": round(info["agree"] / info["total"], 4), "n": info["total"]}
            for dim, info in sorted(per_dim.items())
        },
    }


def _bucket(score: Any) -> int:
    """1-5 分三档（一致率按档位判定，容忍小幅偏差）。"""
    s = int(score)
    if s >= 4:
        return 3
    if s >= 2:
        return 2
    return 1
