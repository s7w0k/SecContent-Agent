"""真实线上用户 query 语料上的检索排序指标评测（Recall@K / Precision@K / MRR / NDCG@K / HitRate）。

与 `ranking_eval.py`（文档级合成真值）不同，本评测器在**真实用户 query 语料**
（`dataset.query.jsonl`，100 条短句，含口语化/缩写/中英混排/无命中/竞品隔离）上评测：

- 真值 = **产品级 → 文档级映射**：期望产品（`expected_product_ids`）下所有
  实际可检索的 core 文档（overview/market-brief/sales-brief/custom）构成相关文档集合。
  - 单产品 query → 该产品下全部相关文档（多相关文档）
  - 多产品 query → 多产品文档并集
  - 无命中 query（expected=[]）→ 相关集合为空，IR 指标无意义，
    单独统计"误召回率"（检索器不得把任何产品文档排在前面）
- 检索：`DocumentRetriever.retrieve_ranked()`（纯相关性排序）。
- 聚合：IR 指标仅在"有相关文档"的样本上宏平均；无命中样本单列误召回。

用法：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.knowledge_retrieval.ranking_eval_query --report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "services" / "backend"
sys.path.insert(0, str(BACKEND_SRC))
sys.path.insert(0, str(REPO_ROOT))

from tests.agent_evals.knowledge_retrieval.generate_query_dataset import (  # noqa: E402
    OUTPUT as QUERY_OUTPUT,
)
from tests.agent_evals.knowledge_retrieval.ranking_metrics import (  # noqa: E402
    aggregate,
    per_query_metrics,
)

KB_ROOT = Path(__file__).resolve().parents[3] / "agent-security-briefs"
PUBLISHED = ["agent-identity-security", "agent-security", "ai-bom"]
KS = (1, 3, 5)
RANKED_DOC_TYPES = ("overview", "market-brief", "sales-brief", "custom")
logger = logging.getLogger(__name__)


def _load_index() -> Any:
    from agent.knowledge_index import KnowledgeIndexBuilder, KnowledgeIndexer

    index_file = KB_ROOT / "_index" / "kb-index.json"
    indexer = KnowledgeIndexer(index_file)
    if indexer.load() is None or not indexer.manifest or not indexer.manifest.docs:
        builder = KnowledgeIndexBuilder(KB_ROOT)
        manifest = builder.build_manifest()
        builder.write(manifest)
        indexer = KnowledgeIndexer(index_file)
        if indexer.load() is None:
            raise RuntimeError("知识索引构建失败")
    return indexer


def _build_retriever(embedding_weight: float = 0.0, use_rerank: bool = False) -> Any:
    from agent.document_retriever import DocumentRetriever

    reranker = _build_reranker() if use_rerank else None

    if embedding_weight > 0:
        from tests.agent_evals.knowledge_retrieval.embedding_provider import (
            build_embedding_store,
        )

        store = build_embedding_store(_load_index())
        if store is not None:
            return DocumentRetriever(
                indexer=_load_index(),
                embedding_store=store,
                hybrid_weights={"exact": 1.0, "embedding": float(embedding_weight)},
                reranker=reranker,
            )
        logger.warning("embedding 不可用，回退纯关键词排序")

    return DocumentRetriever(indexer=_load_index(), reranker=reranker)


def _build_reranker() -> Any:
    from tests.agent_evals.knowledge_retrieval.embedding_provider import (
        build_reranker,
    )

    return build_reranker(_load_index())


def _product_doc_ids(indexer: Any) -> dict[str, list[str]]:
    """已发布产品 → 该产品下可检索 core 文档 doc_id 列表。"""
    mapping: dict[str, list[str]] = {p: [] for p in PUBLISHED}
    for doc in indexer.manifest.docs or []:
        if doc.product_id not in PUBLISHED:
            continue
        if not doc.published:
            continue
        if doc.doc_type not in RANKED_DOC_TYPES:
            continue
        mapping[doc.product_id].append(doc.doc_id)
    return mapping


def _load_query_cases() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with QUERY_OUTPUT.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _rank_for_query(retriever: Any, query: str) -> list[str]:
    from agent.document_retriever import RetrievalRequest

    request = RetrievalRequest(
        purpose="chat",
        query=query,
        product_ids=list(PUBLISHED),
        max_optional_docs=50,
        include_shared=False,
    )
    # 只保留 core 可检索文档参与排序：raw/shared 正文庞大、得分虚高，
    # 会抢占前排把真正相关的 core 文档挤出 Top5；且相关集合只含这些 core 类型。
    return [d.doc_id for d in retriever.retrieve_ranked(request) if d.doc_type in RANKED_DOC_TYPES]


def run_all(
    *,
    write_report: bool = False,
    embedding_weight: float = 0.0,
    use_rerank: bool = False,
) -> dict[str, Any]:
    retriever = _build_retriever(embedding_weight, use_rerank)
    indexer = _load_index()
    product_docs = _product_doc_ids(indexer)
    cases = _load_query_cases()

    rows: list[dict[str, Any]] = []
    ranked_samples = []  # 有相关文档的指标样本
    no_hit_rows = []  # 无命中样本（单独统计误召回）

    for case in cases:
        query = case.get("query", "")
        expected = list(case.get("expected_product_ids", []))
        ranked = _rank_for_query(retriever, query)
        relevant: set[str] = set()
        for pid in expected:
            relevant.update(product_docs.get(pid, []))

        if not relevant:
            # 无命中：检索器不应把任何产品文档排在前面
            hit_any = any(d in ranked for d in set().union(*(product_docs.values())))
            no_hit_rows.append(
                {
                    "case_id": case["case_id"],
                    "query": query,
                    "expected": expected,
                    "ranked_docs": ranked[:5],
                    "false_recall": hit_any,
                }
            )
            continue

        metrics = per_query_metrics(ranked, relevant, KS)
        rows.append(
            {
                "case_id": case["case_id"],
                "query": query,
                "expected": expected,
                "relevant_doc_ids": sorted(relevant),
                "rank": (ranked.index(sorted(relevant)[0]) + 1),
                "ranked_docs": ranked[:5],
                "metrics": metrics,
            }
        )
        ranked_samples.append(metrics)

    agg = aggregate(ranked_samples, KS)
    total_with_rel = len(ranked_samples)
    in_top5 = sum(1 for r in rows if 0 < r["rank"] <= 5) / total_with_rel if total_with_rel else 0.0
    false_recall_rate = (
        (sum(1 for r in no_hit_rows if r["false_recall"]) / len(no_hit_rows))
        if no_hit_rows
        else 0.0
    )

    gates = {
        "recall@3": {
            "pass": agg["recall"]["@3"] >= 0.5,
            "value": agg["recall"]["@3"],
            "threshold": "≥50%",
        },
        "mrr": {"pass": agg["mrr"] >= 0.5, "value": agg["mrr"], "threshold": "≥0.5"},
        "ndcg@3": {
            "pass": agg["ndcg"]["@3"] >= 0.5,
            "value": agg["ndcg"]["@3"],
            "threshold": "≥50%",
        },
        "hit@3": {
            "pass": agg["hit_rate"]["@3"] >= 0.6,
            "value": agg["hit_rate"]["@3"],
            "threshold": "≥60%",
        },
    }

    report = {
        "stage": "phase8",
        "schema_version": "1.0",
        "dataset_version": "query-doc-ranking",
        "embedding_weight": embedding_weight,
        "use_rerank": use_rerank,
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": "agent-security-briefs",
        "total": len(cases),
        "with_relevant": total_with_rel,
        "no_hit": len(no_hit_rows),
        "ks": list(KS),
        "metrics": agg,
        "target_in_top5": in_top5,
        # 无命中误召回为信息字段（非门禁）：retrieve_ranked 是纯相关性排序器，
        # 不负责无命中拒绝（该判定在 product_routing 层，由 baseline-query 评测覆盖）。
        "false_recall_rate": false_recall_rate,
        "gates": gates,
        "results": rows,
        "no_hit_results": no_hit_rows,
    }

    if write_report:
        out = REPO_ROOT / "reports" / "knowledge-retrieval-ranking-query.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report


def print_report(report: dict[str, Any]) -> None:
    m = report["metrics"]
    print(f"\n{'=' * 68}")
    print("  真实线上用户 Query 检索排序指标评测 (Recall@K/Precision@K/MRR/NDCG@K/HitRate)")
    print(f"{'=' * 68}")
    print(
        f"  语料: {report['corpus']}  总样本: {report['total']}  "
        f"有相关文档: {report['with_relevant']}  无命中: {report['no_hit']}"
    )
    print(
        f"  目标文档进入 Top5: {report['target_in_top5'] * 100:.1f}%  "
        f"无命中误召回率: {report['false_recall_rate'] * 100:.1f}%"
    )
    print("-" * 56)
    for k in report["ks"]:
        print(f"  Recall@{k}:    {m['recall'][f'@{k}'] * 100:6.1f}%")
        print(f"  Precision@{k}: {m['precision'][f'@{k}'] * 100:6.1f}%")
        print(f"  NDCG@{k}:      {m['ndcg'][f'@{k}'] * 100:6.1f}%")
        print(f"  HitRate@{k}:   {m['hit_rate'][f'@{k}'] * 100:6.1f}%")
    print(f"  MRR:           {m['mrr']:.4f}")
    print("-" * 56)
    print("  门禁:")
    for name, g in report["gates"].items():
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"    [{mark}] {name}: {g['value'] * 100:.1f}% (要求 {g['threshold']})")
    print("=" * 68)
    print("  有相关文档但目标未在 Top5 的用例:")
    for r in report["results"]:
        if not (0 < r["rank"] <= 5):
            print(
                f"    {r['case_id']} | {r['query'][:30]} | expected={r['expected']} | "
                f"top5={r['ranked_docs']}"
            )
    print("  无命中但误召回的用例:")
    for r in report.get("no_hit_results", []):
        if r["false_recall"]:
            print(f"    {r['case_id']} | {r['query'][:30]} | top5={r['ranked_docs']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实用户 query 检索排序指标评测")
    parser.add_argument(
        "--report", action="store_true", help="写入 reports/knowledge-retrieval-ranking-query.json"
    )
    parser.add_argument(
        "--embed",
        type=float,
        default=0.0,
        help="embedding 混合权重（>0 启用 DASHSCOPE embedding 召回，如 0.3）",
    )
    parser.add_argument("--rerank", action="store_true", help="启用 DEEPSEEK LLM 文档重排")
    args = parser.parse_args()
    _report = run_all(
        write_report=args.report,
        embedding_weight=args.embed,
        use_rerank=args.rerank,
    )
    print_report(_report)
