"""Eval/Replay Harness — 阶段4 §1.4（WBS 4.1）。

- EvalSnapshot：保存 manifest / trace / 输出 / 评分 / 报告 快照（可对账）；
- MatrixRunner：模型 / prompt / skill / 代码版本矩阵比较；
- MinimalRepro：失败样本最小复现包（case + manifest + trace + 评分）。

安全约束：快照与复现包只保留脱敏结果（hash/指纹/状态），不落 prompt 正文
与私有推理链。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.evals import EvalCase, EvalResult, EvalRunner, PairedEvalResult

SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class EvalSnapshot:
    """Eval 结果快照：一条命令产出、可复现、可对账。"""

    def __init__(self, *, output_dir: Path | None = None, runner_version: str = "v1"):
        self.output_dir = output_dir or Path("reports")
        self.runner_version = runner_version

    def save(
        self,
        *,
        level: str,
        report: dict[str, Any],
        pairs: list[PairedEvalResult] | None = None,
        dataset_version: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """保存快照到 reports/eval-snapshot-{level}-{ts}.json，返回路径。"""
        ts = _utc_now().strftime("%Y%m%d-%H%M%S")
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "runner_version": self.runner_version,
            "created_at": _utc_now().isoformat(),
            "level": level,
            "dataset_version": dataset_version,
            "report": report,
            "extra": extra or {},
        }
        if pairs is not None:
            payload["pairs"] = [p.to_legacy_dict() for p in pairs]
        payload["snapshot_hash"] = _sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"eval-snapshot-{level}-{ts}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def verify(payload: dict[str, Any]) -> bool:
        """校验快照完整性（snapshot_hash 自洽）。"""
        if "snapshot_hash" not in payload:
            return False
        expected = payload.pop("snapshot_hash")
        actual = _sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))
        return actual == expected

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════
# 矩阵比较
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MatrixCell:
    """矩阵中的一个比较单元（模型 / prompt / skill / 代码版本）。"""

    label: str
    model_name: str = "deepseek-chat"
    llm_backend: str = "mock"
    prompt_ref: str = ""
    skill_hash: str = ""
    code_revision: str = ""
    n_runs: int = 1


@dataclass
class MatrixReport:
    """矩阵比较报告。"""

    cells: list[dict[str, Any]] = field(default_factory=list)

    def best_by(self, metric: str) -> dict[str, Any] | None:
        """按指标取最优 cell（metric 越小越好）。"""
        valid = [c for c in self.cells if c.get(metric) is not None]
        if not valid:
            return None
        return min(valid, key=lambda c: c[metric])


class MatrixRunner:
    """对同一用例集按矩阵逐 cell 运行 EvalRunner 并聚合对比。"""

    def __init__(self, *, db: Any = None, judge: Any = None):
        self.db = db
        self.judge = judge

    async def run(
        self,
        cases: list[EvalCase],
        matrix: list[MatrixCell],
    ) -> MatrixReport:
        report = MatrixReport()
        for cell in matrix:
            runner = EvalRunner(
                llm_backend=cell.llm_backend,
                model_name=cell.model_name,
                n_runs=max(1, cell.n_runs),
                db=self.db,
            )
            pairs = await runner.run_pairs(cases)
            agg = self._aggregate(pairs, cell)
            report.cells.append(agg)
        return report

    @staticmethod
    def _aggregate(pairs: list[PairedEvalResult], cell: MatrixCell) -> dict[str, Any]:
        ok = 0
        total = 0
        tokens: list[float] = []
        costs: list[float] = []
        latencies: list[float] = []
        for pair in pairs:
            candidate = pair.candidate
            total += 1
            if candidate.succeeded:
                ok += 1
            tc = candidate.token_and_cost or {}
            tokens.append(float(tc.get("input_tokens", 0) + tc.get("output_tokens", 0)))
            costs.append(float(tc.get("cost_usd", 0.0)))
            latencies.append(float(candidate.latency_ms))
        return {
            "label": cell.label,
            "model_name": cell.model_name,
            "llm_backend": cell.llm_backend,
            "prompt_ref": cell.prompt_ref,
            "skill_hash": cell.skill_hash,
            "code_revision": cell.code_revision,
            "cases": total,
            "success_rate": round(ok / total, 4) if total else 0.0,
            "avg_tokens": round(_mean(tokens), 2) if tokens else 0.0,
            "avg_cost_usd": round(_mean(costs), 6) if costs else 0.0,
            "p95_latency_ms": round(_percentile(latencies, 95), 2) if latencies else 0.0,
        }


# ═══════════════════════════════════════════════════════════════
# 最小复现包
# ═══════════════════════════════════════════════════════════════


class MinimalRepro:
    """失败样本最小复现包：case + manifest + trace + 评分 + 运行说明。"""

    def __init__(self, *, output_dir: Path | None = None):
        self.output_dir = output_dir or Path("reports") / "repro"

    def export(
        self,
        *,
        case: EvalCase,
        result: EvalResult,
        output_dir: Path | None = None,
    ) -> Path:
        target = output_dir or self.output_dir
        target.mkdir(parents=True, exist_ok=True)
        package: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case.case_id,
            "category": case.category,
            "dataset_version": case.dataset_version,
            "backend": result.backend,
            "terminal_status": result.terminal_status,
            "run_manifest": result.run_manifest,
            "tool_trace": result.tool_trace,
            "evidence_trace": result.evidence_trace,
            "deterministic_scores": result.deterministic_scores,
            "token_and_cost": result.token_and_cost,
            "latency_ms": result.latency_ms,
            "failure_attribution": result.failure_attribution,
            "case_fixture": {
                "allowed_tools": case.allowed_tools,
                "forbidden_tools": case.forbidden_tools,
                "required_facts": case.required_facts,
                "required_evidence": case.required_evidence,
                "expected_terminal_status": case.expected_terminal_status,
                "max_steps": case.max_steps,
                "max_tokens": case.max_tokens,
            },
            "repro_hash": "",
        }
        package["repro_hash"] = _sha256(
            json.dumps(package, sort_keys=True, ensure_ascii=False, default=str)
        )
        path = target / f"repro-{case.case_id}-{result.backend}.json"
        path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return path


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, round((p / 100.0) * (len(sorted_v) - 1))))
    return sorted_v[idx]
