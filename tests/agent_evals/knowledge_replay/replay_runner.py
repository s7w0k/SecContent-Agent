"""阶段7 离线回放评测器（10.2）。

对历史高分文章固定模型与 prompt，**新旧链路分别生成** PR，盲评：
  - 产品相关性（PR 中出现的产品声明是否落在 allowed_product_claims）
  - 事实准确性（事实句被 KNOWLEDGE_SOURCE 引用覆盖的比例）
  - 传播质量（引用块保留/可读性、无依据高风险事实数）
并自动检查：
  - 跨产品术语（competitor/竞品类声明）
  - 无依据数字（number/version 类声明缺来源）

设计：
- 新旧链路通过可注入的 `build_context` 回调区分（默认旧=无检索切片，新=带检索+展开）；
- 生成通过可注入的 `generate` 回调（固定模型/prompt 由调用方传入，满足"固定模型和 prompt 版本"）；
- 全部指标确定性计算，不依赖 LLM-as-judge，可快速离线运行；
- 报告写入 `reports/knowledge-replay-{mode}.json`。

使用方式：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.knowledge_replay.replay_runner --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 添加 backend 源码到 path
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "services" / "backend"
sys.path.insert(0, str(BACKEND_SRC))
sys.path.insert(0, str(REPO_ROOT))

# 阶段7 退出门槛（10.6/9.5 经验值）
GATES = {
    "product_relevance": 0.95,
    "fact_source_rate": 0.95,
    "unsupported_high_risk": 0.0,  # 无依据高风险事实（数字/版本/部署）条数
}

# 数据集路径：复用阶段0 评测集（含历史文章字段）
DATASET_PATH = Path(__file__).parent.parent / "knowledge_retrieval" / "dataset.v1.jsonl"

# 生成上下文回调签名：build_context(case, mode) -> str（知识上下文）
BuildContext = Callable[[dict[str, Any], str], Any]
# 生成回调签名：generate(case, context, mode) -> dict（含 content / 其他元数据）
Generate = Callable[[dict[str, Any], Any, str], dict[str, Any]]


@dataclass
class ReplayCaseResult:
    """单条用例的旧/新链路对比结果。"""

    case_id: str
    mode: str  # legacy / new
    expected_products: list[str] = field(default_factory=list)
    allowed_claims: list[str] = field(default_factory=list)
    # 产品相关性
    claimed_products: list[str] = field(default_factory=list)
    product_ok: bool = False
    # 事实准确性
    fact_clauses: int = 0
    cited_clauses: int = 0
    fact_source_rate: float = 0.0
    # 传播质量
    citation_blocks: int = 0
    unsupported_high_risk: int = 0
    # 跨产品术语
    competitor_terms: list[str] = field(default_factory=list)
    # 生成文本（脱敏：仅存无需 prompt 的正文片段）
    char_count: int = 0


def _extract_products(content: str) -> list[str]:
    """从生成文本中提取可识别的产品声明（简单启发式，供确定性盲评）。"""
    found: list[str] = []
    # 识别 KNOWLEDGE_SOURCE doc_id 前缀与上下文中的产品 ID 形态
    for token in ("agent-identity-security", "agent-security", "ai-bom"):
        if token in content:
            found.append(token)
    return sorted(set(found))


def _count_citation_blocks(content: str) -> int:
    return content.count("[KNOWLEDGE_SOURCE")


def _competitor_terms(content: str) -> list[str]:
    """跨产品术语自动检查：竞品/领先/超越类声明。"""
    from agent.fact_citation import classify_fact

    terms: list[str] = []
    for clause in content.split("。"):
        if "competitor" in classify_fact(clause):
            terms.append(clause.strip())
    return terms


def _build_metrics(result: ReplayCaseResult) -> dict[str, Any]:
    """从单条结果计算可复核指标。"""
    return {
        "case_id": result.case_id,
        "mode": result.mode,
        "product_ok": result.product_ok,
        "fact_source_rate": round(result.fact_source_rate, 4),
        "citation_blocks": result.citation_blocks,
        "unsupported_high_risk": result.unsupported_high_risk,
        "competitor_terms": result.competitor_terms,
        "char_count": result.char_count,
    }


class ReplayRunner:
    """离线回放 runner：固定模型/prompt，新旧链路分别生成并对比。"""

    def __init__(
        self,
        *,
        build_context: BuildContext | None = None,
        generate: Generate | None = None,
        dataset: list[dict[str, Any]] | None = None,
    ):
        self._build_context = build_context or self._default_build_context
        self._generate = generate or self._default_generate
        self._dataset = dataset if dataset is not None else load_dataset()

    # ── 主流程 ──────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        """对每条用例，新旧链路分别生成并计算指标。"""
        results: list[dict[str, Any]] = []
        for case in self._dataset:
            for mode in ("legacy", "new"):
                ctx = self._build_context(case, mode)
                gen = self._generate(case, ctx, mode)
                if asyncio.iscoroutine(gen):
                    gen = await gen
                content = gen.get("content", "")
                r = self._evaluate(case, mode, content)
                results.append(_build_metrics(r))
        return self._aggregate(results)

    # ── 评估 ────────────────────────────────────────────────

    def _evaluate(
        self, case: dict[str, Any], mode: str, content: str
    ) -> ReplayCaseResult:
        from agent.fact_citation import audit_fact_citations

        expected = list(case.get("expected_product_ids", []))
        allowed = list(case.get("allowed_product_claims", []))

        claimed = _extract_products(content)
        # 产品相关性：声明的产品必须都在期望产品内（不出现禁止/无关产品）
        product_ok = all(p in expected for p in claimed) if expected else (claimed == [])

        # 事实准确性：引用块覆盖的高风险事实比例
        audit = audit_fact_citations(content)
        fact_clauses = audit.fact_clauses
        cited_clauses = audit.cited_clauses
        fact_source_rate = cited_clauses / fact_clauses if fact_clauses else 1.0

        # 传播质量：无依据高风险事实数
        unsupported_high = sum(
            1 for i in audit.issues if i.category == "missing_citation" and i.severity == "high"
        )

        return ReplayCaseResult(
            case_id=str(case.get("case_id", "")),
            mode=mode,
            expected_products=expected,
            allowed_claims=allowed,
            claimed_products=claimed,
            product_ok=product_ok,
            fact_clauses=fact_clauses,
            cited_clauses=cited_clauses,
            fact_source_rate=fact_source_rate,
            citation_blocks=_count_citation_blocks(content),
            unsupported_high_risk=unsupported_high,
            competitor_terms=_competitor_terms(content),
            char_count=len(content),
        )

    # ── 聚合 ────────────────────────────────────────────────

    def _aggregate(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        by_mode: dict[str, list[dict[str, Any]]] = {"legacy": [], "new": []}
        for r in results:
            by_mode[r["mode"]].append(r)

        def _agg(mode: str) -> dict[str, Any]:
            rows = by_mode[mode]
            if not rows:
                return {"cases": 0, "product_relevance": 0.0, "fact_source_rate": 0.0,
                        "unsupported_high_risk": 0, "avg_citation_blocks": 0.0,
                        "competitor_terms": 0, "avg_chars": 0}
            prod_ok = sum(1 for r in rows if r["product_ok"])
            fact_rates = [r["fact_source_rate"] for r in rows]
            unsupported = sum(r["unsupported_high_risk"] for r in rows)
            return {
                "cases": len(rows),
                "product_relevance": round(prod_ok / len(rows), 4),
                "fact_source_rate": round(
                    sum(fact_rates) / len(rows) if fact_rates else 0.0, 4
                ),
                "unsupported_high_risk": unsupported,
                "avg_citation_blocks": round(
                    sum(r["citation_blocks"] for r in rows) / len(rows), 2
                ),
                "competitor_terms": sum(len(r["competitor_terms"]) for r in rows),
                "avg_chars": round(sum(r["char_count"] for r in rows) / len(rows), 1),
            }

        legacy_agg = _agg("legacy")
        new_agg = _agg("new")

        gates = {
            "product_relevance_new": {
                "pass": new_agg["product_relevance"] >= GATES["product_relevance"],
                "value": new_agg["product_relevance"],
                "threshold": f"≥{GATES['product_relevance']}",
            },
            "fact_source_rate_new": {
                "pass": new_agg["fact_source_rate"] >= GATES["fact_source_rate"],
                "value": new_agg["fact_source_rate"],
                "threshold": f"≥{GATES['fact_source_rate']}",
            },
            "unsupported_high_risk_new": {
                "pass": new_agg["unsupported_high_risk"] == GATES["unsupported_high_risk"],
                "value": new_agg["unsupported_high_risk"],
                "threshold": "0",
            },
        }

        return {
            "stage": "phase7_replay",
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
            "total_cases": len(self._dataset),
            "legacy": legacy_agg,
            "new": new_agg,
            "gates": gates,
            "results": results,
        }

    # ── 默认实现（可被注入替换） ────────────────────────────

    @staticmethod
    def _default_build_context(case: dict[str, Any], mode: str) -> str:
        """默认构建上下文：legacy 只给文章本身，new 追加 expected 产品提示。

        真实使用时应替换为 KnowledgeSliceResolver 新旧链路（见离线回放说明）。
        """
        article = case.get("article", {})
        text = f"{article.get('title', '')}\n{article.get('summary_cn', '')}"
        if mode == "new":
            text += f"\n[产品知识] {', '.join(case.get('expected_product_ids', []))}"
        return text

    @staticmethod
    def _default_generate(
        case: dict[str, Any], context: str, mode: str
    ) -> dict[str, Any]:
        """默认生成：直接把上下文作为候选正文（确定性，供 runner 流程自测）。"""
        return {"content": context}


def load_dataset() -> list[dict[str, Any]]:
    """加载阶段0 评测集（含 article 字段）。"""
    items: list[dict[str, Any]] = []
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


# ── 真实 LLM 生成接入（阶段7 10.2："固定模型和 prompt 版本"） ──────────


def build_llm_generate(
    *,
    llm: Any | None = None,
    max_drafts: int | None = 1,
    temperature: float = 0.1,
) -> Generate:
    """构建真实 LLM 生成回调（异步）。

    使用 `DraftGenerator` + `ChatOpenAI`（默认从 settings 读取 DEEPSEEK 配置），
    对每条用例生成 PR 草稿并拼接各草稿 `content_md` 作为生成文本。

    Args:
        llm: 可注入的 `BaseChatModel`；None 时用 settings 构造 ChatOpenAI
        max_drafts: 每用例最多生成几篇草稿（评测建议 1 以控制成本与可对账性）
        temperature: 生成温度（固定，保证可复现）
    """
    from agent.draft_generator import DraftGenerator
    from agent.knowledge import ProductKnowledge
    from config import get_settings

    if llm is None:
        from langchain_openai import ChatOpenAI

        settings = get_settings()
        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=temperature,
            timeout=settings.DEEPSEEK_TIMEOUT,
            max_tokens=settings.DEEPSEEK_MAX_TOKENS,
        )

    generator = DraftGenerator(
        llm=llm,
        knowledge=ProductKnowledge(),
        temperature=temperature,
        max_output_tokens=2048,
    )

    async def _generate(case: dict[str, Any], context: str, mode: str) -> dict[str, Any]:
        article = case.get("article", {})
        # 知识上下文作为 knowledge_slice 注入 prompt（固定 prompt 版本）
        result = await generator.generate(
            article,
            knowledge_slice=context or None,
            max_drafts=max_drafts,
        )
        if not result.get("ok"):
            return {"content": "", "error": result.get("error")}
        pieces = [
            d.get("content_md", "")
            for d in result.get("drafts", [])
            if d.get("content_md")
        ]
        return {"content": "\n\n".join(pieces), "drafts": len(pieces)}

    return _generate


def run_all(
    *, write_report: bool = False, use_llm: bool = False, max_drafts: int | None = 1
) -> dict[str, Any]:
    """运行离线回放并聚合报告。"""
    runner = ReplayRunner(
        generate=build_llm_generate(max_drafts=max_drafts) if use_llm else None
    )
    report = asyncio.run(runner.run())
    if write_report:
        out = REPO_ROOT / "reports"
        out.mkdir(parents=True, exist_ok=True)
        (out / "knowledge-replay.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def print_report(report: dict[str, Any]) -> None:
    print(f"\n{'=' * 64}")
    print("  阶段7 知识检索离线回放报告")
    print(f"{'=' * 64}")
    for mode in ("legacy", "new"):
        agg = report[mode]
        print(f"  [{mode}] 用例: {agg['cases']}")
        print(f"    产品相关性: {agg['product_relevance'] * 100:.1f}%")
        print(f"    事实来源率: {agg['fact_source_rate'] * 100:.1f}%")
        print(f"    无依据高风险事实: {agg['unsupported_high_risk']}")
        print(f"    平均引用块: {agg['avg_citation_blocks']}")
        print(f"    跨产品术语: {agg['competitor_terms']}")
        print(f"    平均字符: {agg['avg_chars']}")
        print("-" * 64)
    print("  门禁:")
    for name, g in report["gates"].items():
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"    [{mark}] {name}: {g['value']} (要求 {g['threshold']})")
    print(f"{'=' * 64}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="阶段7 知识检索离线回放")
    parser.add_argument("--report", action="store_true", help="写入 reports/knowledge-replay.json")
    parser.add_argument("--llm", action="store_true", help="使用真实 LLM 生成（DraftGenerator + ChatOpenAI）")
    parser.add_argument("--max-drafts", type=int, default=1, help="每用例生成草稿数（默认 1）")
    args = parser.parse_args()
    _rep = run_all(write_report=args.report, use_llm=args.llm, max_drafts=args.max_drafts)
    print_report(_rep)
