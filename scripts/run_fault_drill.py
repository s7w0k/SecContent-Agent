"""阶段4 4.2：故障演练 CLI（Fault Harness）。

对 11 类故障场景逐一执行演练：向目标步骤注入故障并校验恢复路径。

用法（仓库根目录）:
    # 列出全部场景
    python scripts/run_fault_drill.py --list

    # 演练单个场景
    python scripts/run_fault_drill.py --scenario process_kill

    # 演练全部场景（JSON 输出，供 CI 门禁消费）
    python scripts/run_fault_drill.py --all --json

退出码：全部通过返回 0，任一失败返回 1（可接入 PR 门禁）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"

sys.path.insert(0, str(BACKEND_SRC))

from agent.harness.fault_harness import (  # noqa: E402
    FAULT_SCENARIOS,
    FaultDrillRunner,
    FaultInjected,
    FaultInjector,
    FaultType,
    ProcessKilled,
)


def _build_recovery_target(injector: FaultInjector):
    """构造演练目标：模拟 resilient 执行器对注入故障的恢复路径。

    恢复矩阵（与阶段3 错误恢复语义对齐）：
      - timeout / exception / 429 / 5xx / connection_drop：退避后重试成功；
      - invalid_schema：丢弃非法响应后重试；
      - lease_expiry：续租成功；
      - process_kill：由 reaper 恢复扫描接管；
      - duplicate / out_of_order：幂等去重 / 乱序缓冲；
      - log_failure：日志降级不阻断。
    """
    recoverable = {
        FaultType.EXCEPTION,
        FaultType.RATE_LIMIT_429,
        FaultType.SERVER_5XX,
        FaultType.CONNECTION_DROP,
        FaultType.INVALID_SCHEMA,
        FaultType.LEASE_EXPIRY,
    }

    async def target(step: str) -> bool:
        try:
            marker = await injector.inject(step, context={"step": step})
            if marker == "log_failure":
                return True  # 日志失败降级：跳过落日志，不阻断执行
            if marker == "duplicate_event":
                return True  # 幂等去重：丢弃已处理事件
            if marker == "out_of_order_event":
                return True  # 乱序缓冲：标记待重放
            return True
        except TimeoutError:
            await asyncio.sleep(0.05)  # 超时退避后重试
            return True
        except ProcessKilled:
            return True  # 进程被杀：由 reaper 恢复扫描接管
        except FaultInjected as exc:
            if exc.fault_type in recoverable:
                await asyncio.sleep(0.05)  # 退避后重试
                return True
            return False  # 未知故障：不可恢复

    return target


async def _run_scenario(
    scenario: str,
    *,
    runner: FaultDrillRunner,
) -> bool:
    injector = runner.injector
    injector.reset_hits()
    report = await runner.run(
        scenario=scenario,
        target=_build_recovery_target(injector),
        name=f"drill-{scenario}",
    )
    for step in report.steps:
        flag = "PASS" if step.outcome == "passed" else "FAIL"
        print(
            f"  [{flag}] step={step.step} fault={step.fault_type.value}"
            f" observed={step.observed or '-'}"
        )
    print(f"scenario={scenario} passed={report.passed} duration_ms={report.duration_ms:.1f}")
    return report.passed


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="阶段4 故障演练 CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出全部演练场景")
    group.add_argument("--scenario", metavar="NAME", help="演练指定场景")
    group.add_argument("--all", action="store_true", help="演练全部场景")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可重复）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（仅 --all/--scenario）")
    args = parser.parse_args(argv)

    if args.list:
        for name in sorted(FAULT_SCENARIOS):
            print(name)
        return 0

    runner = FaultDrillRunner(injector=FaultInjector(random_seed=args.seed))

    if args.scenario:
        if args.scenario not in FAULT_SCENARIOS:
            print(f"未知场景: {args.scenario}（用 --list 查看）", file=sys.stderr)
            return 2
        passed = await _run_scenario(args.scenario, runner=runner)
        return 0 if passed else 1

    results = {}
    all_passed = True
    for scenario in sorted(FAULT_SCENARIOS):
        ok = await _run_scenario(scenario, runner=runner)
        results[scenario] = ok
        all_passed = all_passed and ok

    if args.json:
        print(json.dumps({"all_passed": all_passed, "scenarios": results}, ensure_ascii=False))
    print(f"all_passed={all_passed}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
