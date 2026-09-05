"""Replay CLI — 阶段3 WBS 3.3（trace / candidate / recovery replay）。

用法（仓库根目录）:
    # 1. trace replay：使用记录的工具结果验证状态转换（不执行副作用）
    python scripts/run_replay.py --mode trace --state-file state.json

    # 2. candidate replay：固定 fixture 比较两个候选（新模型/新 prompt）
    python scripts/run_replay.py --mode candidate --inputs "q1,q2,q3"
    python scripts/run_replay.py --mode candidate --dataset tests/agent_evals/eval_datasets/real_v1.jsonl --runs 3

    # 3. recovery replay：从最后检查点继续真实执行未完成步骤
    python scripts/run_replay.py --mode recovery --state-file state.json

退出码：
    0 = 重放通过（trace 无违规 / candidate 一致 / recovery 完成）
    1 = 重放不通过（硬性违规 / 不一致 / 恢复失败）
    2 = 运行/加载错误
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"
DEFAULT_DATASET = REPO_ROOT / "tests" / "agent_evals" / "eval_datasets" / "real_v1.jsonl"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

sys.path.insert(0, str(BACKEND_SRC))

from agent.runtime_state import RuntimeState  # noqa: E402


def _load_state_file(path: Path):
    from agent.runtime_state import migrate_runtime_state

    raw = json.loads(path.read_text(encoding="utf-8"))
    return migrate_runtime_state(raw)


def _candidate_backends() -> tuple[Any, Any]:
    """确定性 mock 后端（无外部依赖，可重复）。"""

    async def backend_a(_question: str) -> str:
        return "答案A：基于资料，采用确定方案。"

    async def backend_b(question: str) -> str:
        return f"答案B：{question} 的确定性回复。"

    return backend_a, backend_b


async def _run_trace(state_file: Path, output: Path) -> int:
    from agent.replay import trace_replay

    state = _load_state_file(state_file)
    result = trace_replay(state)
    print(f"[replay] trace: run={result.run_id} valid={result.valid} steps={result.steps_replayed}")
    print(f"[replay] terminal_status={result.terminal_status}")
    for v in result.violations:
        print(f"  !! {v}")
    payload = {
        "mode": "trace",
        "run_id": result.run_id,
        "valid": result.valid,
        "violations": result.violations,
        "steps_replayed": result.steps_replayed,
        "terminal_status": result.terminal_status,
    }
    _write_output(output, "replay-trace", payload)
    return 0 if result.valid else 1


async def _run_candidate(inputs: list[str], runs: int, dataset: Path, output: Path) -> int:
    from agent.replay import candidate_replay

    if dataset:
        from agent.evals.dataset import load_dataset

        cases = load_dataset(dataset)
        inputs = [c.question for c in cases]
    if not inputs:
        print("[replay] candidate 无输入", file=sys.stderr)
        return 2
    backend_a, backend_b = _candidate_backends()
    result = await candidate_replay(
        inputs=inputs, backend_a=backend_a, backend_b=backend_b, n_runs=runs
    )
    print(
        f"[replay] candidate: inputs={result.input_count} runs={result.n_runs} "
        f"match={result.match_count} ratio={result.match_ratio:.2%}"
    )
    for o in result.outputs[:10]:
        print(
            f"  {'OK ' if o['matched'] else 'MIS'} {o['input'][:40]!r} "
            f"A={o['backend_a_hash'][:8]} B={o['backend_b_hash'][:8]}"
        )
    payload = {
        "mode": "candidate",
        "input_count": result.input_count,
        "match_count": result.match_count,
        "match_ratio": result.match_ratio,
        "outputs": result.outputs,
    }
    _write_output(output, "replay-candidate", payload)
    return 0 if result.consistent else 1


async def _run_recovery(state_file: Path, output: Path) -> int:
    from agent.agent_runtime import AgentRuntime
    from agent.autonomous_service import DemoExecutor, DemoPlanner
    from agent.goal_validator import GoalValidator
    from agent.policy_engine import PolicyEngine
    from agent.replay import recovery_replay

    state = _load_state_file(state_file)
    saved: dict[str, object] = {}

    async def _checkpoint(s: RuntimeState) -> None:
        saved["state"] = s

    runtime = AgentRuntime(
        # 恢复链必须含已完成步骤（completed + pending），否则 DemoPlanner
        # 会认为计划已执行完毕而立即停止（阶段3 §1.3 检查点恢复语义）
        planner=DemoPlanner(chain=[*state.completed_steps, *state.pending_steps]),
        executor=DemoExecutor(),
        policy=PolicyEngine(),
        goal_validator=GoalValidator(required_artifact_keys=(), high_risk_requires_confirm=False),
        checkpointer=_checkpoint,
        max_retries=2,
        backoff_jitter=0.0,
    )
    result = await recovery_replay(
        runtime=runtime,
        state_store=_InMemoryStateStore(saved, state),
        run_id=state.run_id,
        owner_id="replay-cli",
    )
    print(
        f"[replay] recovery: run={result.run_id} executed={result.executed} "
        f"status={result.status} lease_ok={result.lease_ok}"
    )
    print(f"[replay] completed_steps={result.completed_steps}")
    if result.lease_conflict:
        print("  !! 租约被他人持有，跳过执行")
    for m in result.idempotency_missing:
        print(f"  !! 缺幂等键: {m}")
    payload = {
        "mode": "recovery",
        "run_id": result.run_id,
        "executed": result.executed,
        "status": result.status,
        "completed_steps": result.completed_steps,
        "lease_conflict": result.lease_conflict,
        "idempotency_missing": result.idempotency_missing,
    }
    _write_output(output, "replay-recovery", payload)
    passed = (
        result.executed
        and result.status in ("completed", "budget_exceeded")
        and not result.idempotency_missing
    )
    return 0 if passed else 1


class _InMemoryStateStore:
    """CLI 用内存 store（不连 Mongo）：save 写回，load 读初值。"""

    def __init__(self, saved: dict[str, object], initial: RuntimeState):
        self._saved = saved
        self._initial = initial

    async def load(self, run_id: str):
        return self._saved.get("state") or self._initial

    async def save(self, state: RuntimeState) -> None:
        self._saved["state"] = state


def _write_output(output: Path, name: str, payload: dict) -> None:
    out_path = DEFAULT_REPORTS_DIR / f"{name}.json" if output == Path("") else output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(f"report written: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="阶段3 Replay CLI（trace/candidate/recovery）")
    parser.add_argument("--mode", choices=["trace", "candidate", "recovery"], required=True)
    parser.add_argument("--state-file", type=str, default="", help="RuntimeState JSON 文件")
    parser.add_argument("--inputs", type=str, default="", help="candidate 输入（逗号分隔）")
    parser.add_argument("--dataset", type=str, default="", help="candidate 从数据集读输入")
    parser.add_argument("--runs", type=int, default=1, help="candidate 每输入重复次数")
    parser.add_argument("--output", type=str, default="", help="报告输出路径")
    args = parser.parse_args()

    output = Path(args.output)

    if args.mode in ("trace", "recovery"):
        if not args.state_file:
            print(f"[replay] --mode {args.mode} 需要 --state-file", file=sys.stderr)
            sys.exit(2)
        state_file = Path(args.state_file)
        if not state_file.exists():
            print(f"[replay] 状态文件不存在: {state_file}", file=sys.stderr)
            sys.exit(2)

    try:
        if args.mode == "trace":
            code = asyncio.run(_run_trace(Path(args.state_file), output))
        elif args.mode == "candidate":
            inputs = [q.strip() for q in args.inputs.split(",") if q.strip()]
            dataset = Path(args.dataset) if args.dataset else None
            code = asyncio.run(_run_candidate(inputs, args.runs, dataset, output))
        else:
            code = asyncio.run(_run_recovery(Path(args.state_file), output))
    except Exception as exc:
        print(f"[replay] 运行失败: {exc}", file=sys.stderr)
        sys.exit(2)
    sys.exit(code)


if __name__ == "__main__":
    main()
