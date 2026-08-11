"""阶段4 4.6：容量模型与负载模拟 CLI（§5）。

输出容量模型：max concurrent runs / max LLM calls/s / max tokens/min /
max tool calls/s / queue depth threshold / estimated USD/day at 1%,10%,50%,100%。

用法（仓库根目录）:
    # 查看默认容量模型
    python scripts/run_capacity_model.py

    # 查看全部场景模板 + 负载模拟
    python scripts/run_capacity_model.py --preset all --simulate --arrival-rps 3

    # 单一场景 + 重试风暴（provider 5% 失败）
    python scripts/run_capacity_model.py --preset multi_tool --failure-ratio 0.05

    # JSON 输出（供报告/门禁消费）
    python scripts/run_capacity_model.py --preset all --json

退出码：0；本脚本只做观测，不执行任何限流/熔断副作用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"

sys.path.insert(0, str(BACKEND_SRC))

from agent.harness.capacity import (  # noqa: E402
    SCENARIO_PRESETS,
    CapacityInputs,
    CapacityModel,
    LoadScenario,
    run_load_simulation,
)


def _print_capacity(inputs: CapacityInputs, *, as_json: bool = False) -> dict:
    report = CapacityModel(inputs).compute()
    if as_json:
        return report.to_legacy_dict()
    print("── 容量模型 ──")
    print(f"  max concurrent runs           = {report.max_concurrent_runs}")
    print(f"  max LLM calls/s               = {report.max_llm_calls_per_second}")
    print(f"  max tokens/min                = {report.max_tokens_per_minute}")
    print(f"  max tool calls/s              = {report.max_tool_calls_per_second}")
    print(f"  queue depth threshold         = {report.queue_depth_threshold}")
    print(f"  sustainable runs/s            = {report.sustainable_runs_per_second}")
    print(f"  estimated run duration (s)    = {report.estimated_run_duration_seconds}")
    print(f"  USD per run                   = {report.usd_per_run:.6f}")
    for tier, usd in report.usd_per_day_by_rollout.items():
        print(f"  estimated USD/day @ {tier:<4} = {usd:.2f}")
    return report.to_legacy_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段4 容量模型与负载模拟 CLI")
    parser.add_argument(
        "--preset",
        default="short_qa",
        choices=[*SCENARIO_PRESETS.keys(), "all"],
        help="压测场景模板",
    )
    parser.add_argument("--simulate", action="store_true", help="执行负载模拟")
    parser.add_argument("--arrival-rps", type=float, default=2.0, help="到达率（run/s）")
    parser.add_argument("--failure-ratio", type=float, default=0.0, help="provider 失败比例(0-1)")
    parser.add_argument("--duration", type=float, default=120.0, help="模拟时长（秒）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可重复）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    presets = (
        {k: v for k, v in SCENARIO_PRESETS.items() if k == args.preset}
        if args.preset != "all"
        else SCENARIO_PRESETS
    )
    if args.json and len(presets) == 1 and not args.simulate:
        print(json.dumps(_print_capacity(next(iter(presets.values())), as_json=True)))
        return 0

    output: dict = {}
    for name, inputs in presets.items():
        if not args.json:
            print(f"\n## 场景: {name}")
        cap = _print_capacity(inputs, as_json=args.json)
        if args.json:
            output[name] = {"capacity": cap}
        if args.simulate:
            scenario = LoadScenario(
                arrival_rps=args.arrival_rps,
                duration_seconds=args.duration,
                failure_ratio=args.failure_ratio,
                seed=args.seed,
            )
            sim = run_load_simulation(inputs, scenario)
            if args.json:
                output[name]["simulation"] = sim.to_legacy_dict()
            else:
                print("── 负载模拟 ──")
                print(
                    f"  arrivals={sim.total_arrivals} served={sim.served} "
                    f"failed={sim.failed} retries={sim.retries} rejected={sim.rejected}"
                )
                print(
                    f"  peak_queue={sim.peak_queue_depth} p95_wait_ms={sim.p95_queue_wait_ms:.1f} "
                    f"utilization={sim.utilization_ratio:.2f} saturation_s={sim.saturation_seconds:.0f}"
                )
                print(f"  success_rate={sim.success_rate:.3f}")

    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
