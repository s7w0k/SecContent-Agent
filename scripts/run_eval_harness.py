"""真实 Eval Harness CLI -- 阶段2（WBS 2.6 流水线入口）。

用法（仓库根目录）:
    python scripts/run_eval_harness.py --level pr        # PR 快速门禁（mock，CI）
    python scripts/run_eval_harness.py --level nightly   # Nightly（含统计与成本门禁）
    python scripts/run_eval_harness.py --level release   # Release（要求 holdout）
    python scripts/run_eval_harness.py --llm real        # 真实模型（需 DEEPSEEK_API_KEY）

退出码：
    0  = 全部门禁通过（可继续发布流程）
    1  = 硬门禁失败（阻断发布）
    2  = 运行/加载错误
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"
DEFAULT_DATASET = REPO_ROOT / "tests" / "agent_evals" / "eval_datasets" / "real_v1.jsonl"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

sys.path.insert(0, str(BACKEND_SRC))


def _has_real_key() -> bool:
    import os

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    return bool(key) and "your-" not in key and "placeholder" not in key.lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="真实 Agent Eval Harness（阶段2）")
    parser.add_argument("--dataset", type=str, default=str(DEFAULT_DATASET), help="版本化数据集路径")
    parser.add_argument("--level", choices=["pr", "nightly", "release"], default="pr")
    parser.add_argument("--llm", choices=["mock", "real", "auto"], default="auto")
    parser.add_argument("--runs", type=int, default=3, help="每个 case 每后端重复次数")
    parser.add_argument("--model", type=str, default="deepseek-chat")
    parser.add_argument("--split", type=str, default="", help="只评测指定 split（如 train）")
    parser.add_argument("--output", type=str, default="", help="报告输出路径")
    parser.add_argument("--print-results", action="store_true", help="打印逐条结果")
    args = parser.parse_args()

    from agent.evals import coverage_report, dataset_fingerprint, load_dataset, run_eval_pipeline

    dataset_path = Path(args.dataset)
    try:
        cases = load_dataset(dataset_path)
    except Exception as exc:
        print(f"[eval-harness] 数据集加载失败: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.split:
        cases = [c for c in cases if c.split == args.split]
        if not cases:
            print(f"[eval-harness] split={args.split} 无用例", file=sys.stderr)
            sys.exit(2)

    coverage = coverage_report(cases)
    print(
        f"[eval-harness] dataset={dataset_path.name} cases={coverage['total']} "
        f"categories={len(coverage['by_category'])} missing={coverage['missing_categories']}"
    )
    print(f"[eval-harness] dataset_fingerprint={dataset_fingerprint(cases)}")

    backend = args.llm
    if backend == "auto":
        # PR 门禁不调收费模型（对齐阶段2 §6.3：PR 用确定性模拟器后端）
        backend = "mock" if args.level == "pr" else "real" if _has_real_key() else "mock"
    print(f"[eval-harness] level={args.level} llm={backend} runs={args.runs} model={args.model}")

    async def _run() -> dict:
        return await run_eval_pipeline(
            cases,
            level=args.level,
            llm_backend=backend,
            n_runs=args.runs,
            model_name=args.model,
        )

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        print(f"[eval-harness] 运行失败: {exc}", file=sys.stderr)
        sys.exit(2)

    report = result["report"]
    gates = result["gates"]
    violation = gates.pop("_violation", "")

    print(f"\n{'=' * 68}")
    print(f"  Eval Harness 报告  [level={args.level}] [llm={backend}]")
    print(f"{'=' * 68}")
    print(f"  样本: {report['n_cases']}（runs={args.runs}）")
    print(
        f"  legacy   成功率 {report['legacy']['success_rate']:.2%}  "
        f"token均值 {report['legacy']['tokens']['mean']:.0f}  "
        f"USD均值 {report['legacy']['cost_usd']['mean']:.6f}"
    )
    print(
        f"  candidate 成功率 {report['candidate']['success_rate']:.2%}  "
        f"token均值 {report['candidate']['tokens']['mean']:.0f}  "
        f"USD均值 {report['candidate']['cost_usd']['mean']:.6f}"
    )
    paired = report["paired"]
    print(
        f"  paired: token 下降 {paired['token_reduction_pct']:.1f}%  "
        f"cost 下降 {paired['cost_reduction_pct']:.1f}%  "
        f"p95 时延变化 {paired['latency_p95_reduction_pct']:.1f}%  "
        f"质量退化 {len(paired['quality_regressed_cases'])} 例"
    )
    print(f"{'-' * 68}")
    print(f"  门禁 ({len(gates)}):")
    for name, g in gates.items():
        mark = "PASS" if g["pass"] else "FAIL"
        hard = "H" if g["hard"] else "S"
        print(f"    [{mark}][{hard}] {name}: {g['value']}  {g['reason']}")
    if violation:
        print(f"\n  !! {violation}")
    print(f"{'=' * 68}")

    out_path = (
        Path(args.output)
        if args.output
        else DEFAULT_REPORTS_DIR / f"eval-harness-{args.level}-{dataset_path.stem}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "level": args.level,
        "llm_backend": backend,
        "passed": result["passed"],
        "report": report,
        "gates": gates,
        "violation": violation,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(f"report written: {out_path}")

    if not result["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
