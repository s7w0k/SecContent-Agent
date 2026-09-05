"""文档检索排序评测器（Recall@K / Precision@K / MRR / NDCG@K / HitRate）。

在真实知识索引上，用真实 `DocumentRetriever` 对每个 query 执行检索排序，
以**文档级 ground-truth** 衡量信息检索排序质量。

评测集构造（文档级真值）：
  - 遍历已发布产品下的核心文档（overview/market-brief/sales-brief/architecture-brief）
  - 对每个文档用其 title + keywords 生成一条 query
  - ground-truth = 该文档的 doc_id（单相关文档，Recall/Precision/MRR/NDCG 有明确语义）
  - 全库检索（不缩小到单产品），衡量检索器能否把目标文档排到前面（跨产品隔离）

用法：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.knowledge_retrieval.ranking_eval --report
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "services" / "backend"
sys.path.insert(0, str(BACKEND_SRC))
sys.path.insert(0, str(ROOT))

from tests.agent_evals.knowledge_retrieval.ranking_metrics import (  # noqa: E402
    aggregate,
    per_query_metrics,
)

# 真实知识库根目录（仓库根下）
KB_ROOT = Path(__file__).resolve().parents[3] / "agent-security-briefs"

# 已发布产品：用于构造检索候选（不缩小到单产品，测试跨产品隔离）
PUBLISHED = ["agent-identity-security", "agent-security", "ai-bom"]

# 评测 K 值
KS = (1, 3, 5)

# 纳入文档级真值的 doc_type。
# 仅收录实际可检索的类型（chat purpose 的 allowed types：overview/market-brief/sales-brief/custom）。
# architecture-brief / tasks 不在任何 purpose 的 allowed types 内，属检索策略的注入边界，不参与相关性排序评测。
RANKED_DOC_TYPES = ("overview", "market-brief", "sales-brief", "custom")


def _load_index() -> Any:
    """加载（必要时构建）真实知识索引，返回 KnowledgeIndexer。"""
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


def _build_retriever() -> Any:
    from agent.document_retriever import DocumentRetriever

    indexer = _load_index()
    return DocumentRetriever(indexer=indexer)


def _keyword_query(doc: Any) -> str:
    """由文档 title + keywords 构造检索 query（贴近真实检索词）。"""
    parts = [doc.title]
    parts.extend(k for k in (doc.keywords or [])[:3])
    return " ".join(parts)


def _build_ranking_queries(indexer: Any) -> list[dict[str, Any]]:
    """构造文档级真值评测集：query → 相关 doc_id。"""
    queries: list[dict[str, Any]] = []
    for doc in indexer.manifest.docs or []:
        if doc.product_id not in PUBLISHED:
            continue
        if not doc.published:
            continue
        if doc.doc_type not in RANKED_DOC_TYPES:
            continue
        queries.append(
            {
                "query": _keyword_query(doc),
                "relevant_doc_id": doc.doc_id,
                "product_id": doc.product_id,
                "doc_type": doc.doc_type,
                "title": doc.title,
            }
        )
    return queries


def _rank_for_query(retriever: Any, query: str) -> list[str]:
    """对 query 执行真实检索，返回按相关度降序的全量候选 doc_id。

    用 `retrieve_ranked()`（纯相关性排序，不分 required/optional 注入分区），
    保证 IR 指标衡量的是检索策略的相关性排序质量，而非注入优先级。
    """
    from agent.document_retriever import RetrievalRequest

    request = RetrievalRequest(
        purpose="chat",
        query=query,
        product_ids=list(PUBLISHED),
        max_optional_docs=50,
        include_shared=False,
    )
    ranked = retriever.retrieve_ranked(request)
    return [d.doc_id for d in ranked]


def run_all(*, write_report: bool = False) -> dict[str, Any]:
    retriever = _build_retriever()
    indexer = _load_index()
    queries = _build_ranking_queries(indexer)

    per_query_rows: list[dict[str, Any]] = []
    for q in queries:
        ranked = _rank_for_query(retriever, q["query"])
        relevant = {q["relevant_doc_id"]}
        metrics = per_query_metrics(ranked, relevant, KS)
        per_query_rows.append(
            {
                "query": q["query"],
                "relevant_doc_id": q["relevant_doc_id"],
                "product_id": q["product_id"],
                "doc_type": q["doc_type"],
                "title": q["title"],
                "rank": (ranked.index(q["relevant_doc_id"]) + 1)
                if q["relevant_doc_id"] in ranked
                else 0,
                "ranked_docs": ranked[:5],
                "metrics": metrics,
            }
        )

    agg = aggregate([r["metrics"] for r in per_query_rows], KS)

    # 目标文档出现在前 5 的比例
    in_top5 = sum(1 for r in per_query_rows if 0 < r["rank"] <= 5) / len(per_query_rows)

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
        "stage": "phase0",
        "schema_version": "1.0",
        "dataset_version": "doc-ranking",
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": "agent-security-briefs",
        "total": len(per_query_rows),
        "ks": list(KS),
        "metrics": agg,
        "target_in_top5": in_top5,
        "gates": gates,
        "results": per_query_rows,
    }

    if write_report:
        out = REPO_ROOT / "reports" / "knowledge-retrieval-ranking.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report


def print_report(report: dict[str, Any]) -> None:
    m = report["metrics"]
    print(f"\n{'=' * 68}")
    print("  文档检索排序指标评测 (Recall@K / Precision@K / MRR / NDCG@K / HitRate)")
    print(f"{'=' * 68}")
    print(f"  语料: {report['corpus']}  文档级真值样本: {report['total']}")
    print(f"  目标文档进入 Top5: {report['target_in_top5'] * 100:.1f}%")
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
    print("  未进入排名(rank=0)的用例:")
    for r in report["results"]:
        if r["rank"] == 0:
            print(
                f"    {r['title'][:30]} | {r['product_id']}/{r['doc_type']} | query='{r['query'][:40]}'"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="文档检索排序指标评测")
    parser.add_argument(
        "--report", action="store_true", help="写入 reports/knowledge-retrieval-ranking.json"
    )
    args = parser.parse_args()
    _report = run_all(write_report=args.report)
    print_report(_report)
