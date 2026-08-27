"""CI Hard Gate 5：新架构生产接管 Cutover 接缝不变量（计划 §92-97 / §102 / §126）。

校验（基于静态源码扫描，CI 无 Mongo/网络）：
  - 主执行路径 task_queue 不得直接依赖旧链：不 import `_execute_pipeline_task`、
    不使用 `ctx["pipeline_v2"]`（execute_pipeline / resume_pipeline 改走 execution_router）。
  - worker.py 必须装配统一 Execution 运行时（构建 LegacyPipelineExecutor +
    build_production_execution_runtime，并把 execution_router 注入 ctx，legacy 侧注入 executor + validate_runtime）。
  - main.py 必须装配统一 Execution 运行时（build_production_execution_runtime，
    接入 app.state.execution_* + validate_runtime fail-fast）。
  - config.py 默认 AGENT_EXECUTION_MODE 必须为合法模式（cutover 阶段默认 legacy）。
  - execution 层关键契约可导入（contracts/errors/router/factory/legacy_executor）。

用法:
    python scripts/check_agent_cutover.py
退出码：0 = 通过；1 = 不变量违反。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "services" / "backend"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

# 允许引用旧链的位置：legacy_executor（注入 runner 的分层隔离点）、
# 以及非执行主路径的海外定时抓取调度器（仅用 pipeline_v2.tools 复用抓取工具集）。
# 其余主路径（task_queue 的 execute_pipeline / resume_pipeline）一律禁止直接触达旧执行链。
_FORBIDDEN_PATTERNS = [
    ('ctx["pipeline_v2"]', '主执行路径直接使用 ctx["pipeline_v2"]'),
    ("ctx['pipeline_v2']", "主执行路径直接使用 ctx['pipeline_v2']"),
]
_MAIN_TASK_FUNCS = ("execute_pipeline", "resume_pipeline")

VALID_MODES = {"legacy", "skill_shadow", "skill_canary", "skill_planned"}


def _func_bodies(src: str, names: tuple[str, ...]) -> list[str]:
    """提取顶层 ``async def <name>(`` ... ``async def `` 之间的函数体。"""
    bodies: list[str] = []
    lines = src.splitlines()
    for name in names:
        start: int | None = None
        for i, line in enumerate(lines):
            if line.startswith(f"async def {name}("):
                start = i
                break
        if start is None:
            bodies.append("")
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("async def ") or j == len(lines) - 1:
                end = j
                break
        bodies.append("\n".join(lines[start:end]))
    return bodies


def _content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _check_task_queue() -> list[str]:
    """task_queue 主路径必须经 ExecutionRouter，禁反向依赖 api.pipeline / pipeline_v2。"""
    violations: list[str] = []
    path = BACKEND_SRC / "agent" / "task_queue.py"
    if not path.exists():
        return [f"缺失 {path.relative_to(ROOT)}"]
    src = _content(path)
    if "from api.pipeline import _execute_pipeline_task" in src:
        violations.append(
            "task_queue.py 直接 import _execute_pipeline_task（应经 ExecutionRouter）"
        )
    if "from api.pipeline import" in src:
        violations.append("task_queue.py 反向依赖 api.pipeline（Cutover PR-1 目标）")
    # 对两个主 ARQ 任务函数做 ctx["pipeline_v2"] 扫描（海外抓取调度器合法复用工具集，不在此列）
    for name, body in zip(_MAIN_TASK_FUNCS, _func_bodies(src, _MAIN_TASK_FUNCS), strict=True):
        if not body:
            violations.append(f"task_queue.py 缺失主任务函数 {name}")
            continue
        for pattern, label in _FORBIDDEN_PATTERNS:
            if pattern in body:
                violations.append(f"task_queue.py/{name} 存在 {label}")
    # 必须经 execution_router 入口
    for needle in ("execution_router", "agent.execution", "ExecutionRequest"):
        if needle not in src:
            violations.append(f"task_queue.py 缺少 {needle}（应接入 ExecutionRouter）")
    return violations


def _check_worker() -> list[str]:
    violations: list[str] = []
    path = BACKEND_SRC / "worker.py"
    if not path.exists():
        return [f"缺失 {path.relative_to(ROOT)}"]
    src = _content(path)
    if "LegacyPipelineExecutor" not in src or "build_production_execution_runtime" not in src:
        violations.append(
            "worker.py 未装配统一 Execution 运行时（LegacyPipelineExecutor + build_production_execution_runtime）"
        )
    if "legacy_executor" not in src or "validate_runtime" not in src:
        violations.append("worker.py 未注入 legacy_executor 或未做 startup 矩阵校验（validate_runtime）")
    if "execution_router" not in src:
        violations.append("worker.py 未把 execution_router 注入 ctx / app.state")
    return violations


def _check_main() -> list[str]:
    violations: list[str] = []
    path = BACKEND_SRC / "main.py"
    if not path.exists():
        return [f"缺失 {path.relative_to(ROOT)}"]
    src = _content(path)
    if "build_production_execution_runtime" not in src:
        violations.append("main.py 未装配统一 Execution 运行时（build_production_execution_runtime）")
    # app.state.execution_runtime / execution_router 均由 execution_runtime 派生，此处不做字符串强约束
    if "execution_runtime" not in src or "validate_runtime" not in src:
        violations.append("main.py 未接入 execution_runtime/startup 校验（app.state.execution_* + validate_runtime）")
    return violations


def _check_config() -> list[str]:
    violations: list[str] = []
    path = BACKEND_SRC / "config.py"
    if not path.exists():
        return [f"缺失 {path.relative_to(ROOT)}"]
    src = _content(path)
    if "AGENT_EXECUTION_MODE" not in src:
        violations.append("config.py 缺少 AGENT_EXECUTION_MODE")
    else:
        # 默认值必须为合法模式（cutover 阶段合法默认：legacy 保持行为；不得落在非法值）
        if 'default="legacy"' not in src and 'default="skill_planned"' not in src:
            # 宽松检查：至少出现一个合法默认
            matched = [m for m in VALID_MODES if f'default="{m}"' in src]
            if not matched:
                violations.append(
                    "config.py 的 AGENT_EXECUTION_MODE 默认值非法，须为 legacy 或 skill_planned 之一"
                )
    return violations


def _check_execution_contracts() -> list[str]:
    violations: list[str] = []
    mods = (
        "agent.execution.contracts",
        "agent.execution.errors",
        "agent.execution.router",
        "agent.execution.factory",
        "agent.execution.legacy_executor",
    )
    for mod in mods:
        try:
            __import__(mod)
        except Exception as exc:
            violations.append(f"execution 层模块导入失败 {mod}: {type(exc).__name__}: {exc}")
    return violations


def _collect_violations() -> list[str]:
    violations: list[str] = []
    violations += _check_task_queue()
    violations += _check_worker()
    violations += _check_main()
    violations += _check_config()
    violations += _check_execution_contracts()
    return violations


def main() -> int:
    violations = _collect_violations()
    if violations:
        print("❌ CI Hard Gate 5 failed — Cutover 接缝不变量违反：")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("✅ CI Hard Gate 5 passed — Cutover 接缝不变量成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
