"""LegacyPipelineExecutor / SkillPlannedExecutor + ExecutionResultAdapter（计划 §9-15）。

验收：
  - legacy 包装旧链（注入 runner）并映射统一 ExecutionResult，且本文件不依赖 api.pipeline
  - skill executor 经 OrchestrationRuntime.run 产出 ExecutionResult（engine=skill_planned）
  - result adapter 把 OrchestratorState 的 Artifact/status 映射为统一结果
  - skill resume 显式抛 ResumeNotSupported（§66 幂等 replay 未落 checkpoint 前不支持）
"""

from __future__ import annotations

import pytest
from agent.execution.contracts import (
    ExecutionRequest,
)
from agent.execution.errors import ResumeNotSupported
from agent.execution.legacy_executor import LegacyPipelineExecutor
from agent.execution.result_adapter import ExecutionResultAdapter
from agent.execution.skill_executor import SkillPlannedExecutor
from agent.orchestration.contracts import OrchestratorState


def _req(**kw) -> ExecutionRequest:
    d = {
        "task_id": "t1",
        "task_type": "run-v2",
        "tenant_id": "ten",
        "user_id": "usr",
        "trace_id": "tr",
    }
    d.update(kw)
    return ExecutionRequest(**d)


class _OrchStub:
    """OrchestrationRuntime 桩：记录入参并返回给定 state。"""

    def __init__(self, state: OrchestratorState) -> None:
        self.state = state
        self.kwargs: dict = {}

    async def run(self, **kwargs) -> OrchestratorState:
        self.kwargs = kwargs
        return self.state


class TestLegacyExecutor:
    async def test_execute_wraps_injected_runner(self) -> None:
        captured: dict = {}

        async def runner(request: ExecutionRequest) -> dict:
            captured["request"] = request
            return {"status": "completed", "artifact_refs": ["art:1"], "output": {"n": 1}}

        exec_ = LegacyPipelineExecutor(execute_runner=runner)
        result = await exec_.execute(_req(run_id="r9"))
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"
        assert result.artifact_refs == ["art:1"]
        assert captured["request"].run_id == "r9"

    async def test_execute_maps_failed_status(self) -> None:
        async def runner(request: ExecutionRequest) -> dict:
            return {"status": "failed", "error": "boom"}

        exec_ = LegacyPipelineExecutor(execute_runner=runner)
        result = await exec_.execute(_req())
        assert result.status == "FAILED"
        assert result.error_message == "boom"

    async def test_resume_uses_resume_runner(self) -> None:
        async def run_runner(request):
            return {"status": "completed"}

        async def resume_runner(request):
            return {"status": "completed", "run_id": "resumed"}

        exec_ = LegacyPipelineExecutor(execute_runner=run_runner, resume_runner=resume_runner)
        result = await exec_.resume(_req())
        assert result.status == "SUCCEEDED"

    async def test_resume_without_runner_raises(self) -> None:
        async def run_runner(request):
            return {"status": "completed"}

        exec_ = LegacyPipelineExecutor(execute_runner=run_runner)
        with pytest.raises(RuntimeError, match="resume runner"):
            await exec_.resume(_req())

    async def test_exception_propagates_for_arq_retry(self) -> None:
        async def runner(request):
            raise RuntimeError("infra down")

        exec_ = LegacyPipelineExecutor(execute_runner=runner)
        with pytest.raises(RuntimeError, match="infra down"):
            await exec_.execute(_req())


class TestSkillExecutor:
    async def test_runs_full_workflow_via_orchestration(self) -> None:
        state = OrchestratorState(
            run_id="run-1",
            goal="写一篇报道",
            status="COMPLETED",
            completed_steps=["s1", "s2"],
            artifact_refs={"draft": "art:draft-1"},
        )
        stub = _OrchStub(state)
        exec_ = SkillPlannedExecutor(orchestration_runtime=stub)  # type: ignore[arg-type]
        req = _req(goal="写一篇报道")
        result = await exec_.execute(req)
        assert result.engine == "skill_planned"
        assert result.status == "SUCCEEDED"
        assert result.artifact_refs == ["art:draft-1"]
        assert stub.kwargs["goal"] == "写一篇报道"
        assert stub.kwargs["tenant_id"] == "ten"

    async def test_failed_state_maps_to_failed(self) -> None:
        state = OrchestratorState(run_id="run-2", goal="g", status="FAILED")
        exec_ = SkillPlannedExecutor(orchestration_runtime=_OrchStub(state))  # type: ignore[arg-type]
        result = await exec_.execute(_req())
        assert result.status == "FAILED"

    async def test_resume_not_supported(self) -> None:
        state = OrchestratorState(run_id="run-3", goal="g")
        exec_ = SkillPlannedExecutor(orchestration_runtime=_OrchStub(state))  # type: ignore[arg-type]
        with pytest.raises(ResumeNotSupported):
            await exec_.resume(_req())


class TestResultAdapter:
    async def test_maps_orchestrator_state(self) -> None:
        state = OrchestratorState(
            run_id="r",
            goal="g",
            status="COMPLETED",
            completed_steps=["a", "b"],
            failed_steps=["c"],
            artifact_refs={"x": "art:x", "y": "art:y"},
            reviewer_rounds=1,
            replan_count=0,
        )
        result = await ExecutionResultAdapter().from_orchestrator(request=_req(), state=state)
        assert result.status == "SUCCEEDED"
        assert result.engine == "skill_planned"
        assert result.artifact_refs == ["art:x", "art:y"]
        assert result.output["reviewer_rounds"] == 1
        assert result.error_code is None
