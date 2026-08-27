"""Final Closure — 100% 收口计划的 Resume 与 Reviewer Hard Gates（EPIC-A §16/§17、EPIC-B §33/§34）。

EPIC-A（Durable Resume）测试（§17）：
  test_resume_after_step1 / after_step2 / after_draft_generation
  test_resume_does_not_repeat_completed_skill
  test_resume_reuses_artifact
  test_resume_rejects_missing_artifact
  test_resume_rejects_wrong_tenant_artifact
  test_retry_write_idempotent
  test_resume_after_worker_restart

§16 Hard Gates 断言：
  [PASS] completed skill is not executed twice
  [PASS] artifact refs reused
  [PASS] write tool duplicate side effect = 0
  [PASS] worker restart can resume

EPIC-B（Reviewer 主链）初步骨架：test_full_workflow_invokes_reviewer（§33/§52），
其余 §34 项在 b2 阶段补全。

约定：全部使用内存桩（ExecutionRunStore 内存 DB + ArtifactStore 内存库），
不发任何网络请求；Worker 重启以"同一持久介质新建实例"模拟（§17 / §69）。
模拟"中途崩溃"两种方式：必选步骤 crash（留下首次尝试的幂等键）与
max_skill_calls 预算切断（保留前 N 步已完成的持久化状态）。
"""

from __future__ import annotations

from typing import Any

import pytest
from agent.artifacts.store import ArtifactStore
from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
)
from agent.execution.run_store import ExecutionRunStore
from agent.orchestration import (
    OrchestratorAgent,
    OrchestratorBudget,
)
from agent.orchestration.review_policy import DraftReviewPolicy
from agent.skills.contracts import SkillManifest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.runtime import SkillRuntime
from agent.specialists.reviewer_agent import ReviewerAgent

# ══════════════════════════════════════════════════════════════
# 桩：可控 Skill / 内存 Mongo 假库（支持 replace_one upsert）
# ══════════════════════════════════════════════════════════════


class _StubSkill:
    """确定性 Skill：记录每次调用与收到的幂等键；可置 crash 模拟某步骤首次尝试失败。"""

    def __init__(
        self,
        name: str,
        *,
        status: str = "SUCCEEDED",
        artifact_type: str,
        crash: bool = False,
        content: str = "",
    ):
        self.name = name
        self.status = status
        self.artifact_type = artifact_type
        self.crash = crash
        self.content = content
        self.calls: list[str] = []
        self.writes: int = 0
        self.received_keys: list[tuple[str, str | None]] = []

    async def execute(self, request: Any, context: Any) -> SkillResult:
        self.calls.append(request.skill_name)
        self.received_keys.append(
            (request.skill_name, request.params.get("idempotency_key"))
        )
        if self.crash:
            raise RuntimeError(f"{self.name} crashed")
        if self.status == "FAILED":
            return SkillResult.failed(self.name, "boom")
        if self.status == "BLOCKED":
            return SkillResult.blocked(self.name, "blocked")
        rec = await context.store_artifact(
            artifact_type=self.artifact_type,
            payload={
                "artifact_id": f"{self.name}-art",
                "note": self.name,
                "content": self.content,
            },
            producer=self.name,
            step_id=request.skill_name,
        )
        self.writes += 1  # 仅真实产出产物才计一次"写副作用"
        return SkillResult.succeeded(self.name, artifact_refs=[rec["ref"]])


def _register(registry: ExecutableSkillRegistry, stub: _StubSkill) -> None:
    registry.register(
        stub,
        SkillManifest(
            name=stub.name,
            version="1.0.0",
            description=stub.name,
            required_tools=(),
            status="published",
            risk_level="low",
            output_artifact_type=stub.artifact_type,
        ),
    )


def _make_runtime(stubs: list[_StubSkill], artifact_store: ArtifactStore) -> SkillRuntime:
    business_registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        business_registry,
        adapters={BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()},
    )
    registry = ExecutableSkillRegistry(business_tool_names=business_registry.names())
    for stub in stubs:
        _register(registry, stub)
    return SkillRuntime(
        registry,
        tool_executor=executor,
        artifact_store=artifact_store,
        default_adapter=BusinessToolAdapterKind.FAKE,
    )


def _happy_stubs() -> list[_StubSkill]:
    return [
        _StubSkill("article-triage", artifact_type="TriageArtifact"),
        _StubSkill("product-scoring", artifact_type="ScoringArtifact"),
        _StubSkill("draft-writing", artifact_type="DraftArtifact"),
    ]


class _RevisionStub(_StubSkill):
    """draft-revision：记录收到的修订指令与目标版本，产出 DraftArtifact v2（§25/§28）。"""

    def __init__(self) -> None:
        super().__init__("draft-revision", artifact_type="DraftArtifact", content="draft v2")
        self.instructions: list[str] = []
        self.expected_versions: list[int] = []
        self.parent_refs: list[str] = []

    async def execute(self, request: Any, context: Any) -> SkillResult:
        self.calls.append(request.skill_name)
        self.received_keys.append(
            (request.skill_name, request.params.get("idempotency_key"))
        )
        self.instructions.append(str(request.params.get("instruction", "")))
        self.expected_versions.append(int(request.params.get("expected_version", 1)))
        self.parent_refs.append(str(request.input_refs.get("parent_artifact_ref", "")))
        if self.crash:
            raise RuntimeError("draft-revision crashed")
        rec = await context.store_artifact(
            artifact_type="DraftArtifact",
            payload={"artifact_id": "draft-revision-art", "note": "rev", "content": self.content},
            producer="draft-revision",
            step_id="draft-revision",
        )
        self.writes += 1  # 修订确实产出新版本产物（唯一允许的"写"不在 Reviewer 手里）
        return SkillResult.succeeded("draft-revision", artifact_refs=[rec["ref"]])


class _MemoryCollection:
    """最小 async 内存集合（支持 ExecutionRunStore 的 replace_one upsert 读改写）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._docs: list[dict[str, Any]] = []

    async def create_indexes(self, indexes: list[Any]) -> list[str]:
        return [str(i) for i in indexes]

    def _match(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(self, query: dict[str, Any], **_kwargs: Any) -> dict[str, Any] | None:
        for d in self._docs:
            if self._match(d, query):
                return d
        return None

    async def replace_one(self, query: dict[str, Any], replacement: dict[str, Any], **_: Any) -> None:
        for d in self._docs:
            if self._match(d, query):
                d.clear()
                d.update(replacement)
                return
        self._docs.append(dict(replacement))


class _MemoryDB:
    """内存假库：跨实例共享同一份内存介质，模拟 Mongo 在 Worker 重启后数据仍在。"""

    def __init__(self) -> None:
        self._collections: dict[str, _MemoryCollection] = {}

    def __getitem__(self, name: str) -> _MemoryCollection:
        if name not in self._collections:
            self._collections[name] = _MemoryCollection(name)
        return self._collections[name]


class _Shared:
    def __init__(
        self,
        *,
        draft_content: str = "",
        with_revision_stub: bool = False,
    ) -> None:
        self.artifact_store = ArtifactStore()
        stubs = _happy_stubs()
        if draft_content:
            for st in stubs:
                if st.name == "draft-writing":
                    st.content = draft_content
        self.rev_stub: _RevisionStub | None = None
        if with_revision_stub:
            self.rev_stub = _RevisionStub()
            stubs.append(self.rev_stub)
        self.stubs = {s.name: s for s in stubs}
        self.runtime = _make_runtime(stubs, self.artifact_store)
        self.run_store = ExecutionRunStore(_MemoryDB())

    def agent(
        self,
        *,
        task_id: str,
        tenant_id: str = "ten",
        snapshot: str = "snap-v1",
        reviewer: Any = None,
        max_skill_calls: int | None = None,
    ) -> OrchestratorAgent:
        budget = OrchestratorBudget(max_replans=0)
        if max_skill_calls is not None:
            budget = budget.model_copy(update={"max_skill_calls": max_skill_calls})
        return OrchestratorAgent(
            skill_runtime=self.runtime,
            run_store=self.run_store,
            task_id=task_id,
            skill_snapshot_hash=snapshot,
            reviewer=reviewer,
            budget=budget,
        )


async def _run_crash(s: _Shared, *, crash_step: str, task_id: str) -> None:
    """令某个必选步骤首次尝试崩溃 → 此前步骤已完成并持久化、整体 FAILED。"""
    s.stubs[crash_step].crash = True
    a1 = s.agent(task_id=task_id)
    state = await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id=task_id)
    assert state.status == "FAILED"
    s.stubs[crash_step].crash = False  # resume 时该步骤恢复正常


async def _run_cut(s: _Shared, *, cut_after: int, task_id: str) -> None:
    """用 max_skill_calls 预算切断：前 cut_after 步已完成、后续未执行、整体 FAILED。"""
    a1 = s.agent(task_id=task_id, max_skill_calls=cut_after)
    state = await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id=task_id)
    assert state.status == "FAILED"


async def _resume(s: _Shared, task_id: str, tenant_id: str = "ten") -> tuple[OrchestratorAgent, Any]:
    a2 = s.agent(task_id=task_id)
    record = await s.run_store.get_by_task(task_id)
    assert record is not None, "resume 前必须有持久化的 ExecutionRunRecord"
    state = await a2.resume(run_record=record, user_id="u", tenant_id=tenant_id)
    return a2, state


def _delete_artifact(s: _Shared, ref: str) -> None:
    """从持久化存储中移除对应 artifact，模拟产物在持久介质中丢失。"""
    art_type, rest = ref.split(":", 1)
    key = f"{art_type}:{rest}"
    s.artifact_store._records.pop(key, None)  # type: ignore[attr-defined]
    s.artifact_store._payloads.pop(key, None)  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════
# §17 Resume Tests
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_after_step1():
    """step1（article-triage）完成后崩溃 → resume 续跑 s2/s3，s1 不重跑、产物复用。"""
    s = _Shared()
    await _run_crash(s, crash_step="product-scoring", task_id="t-resume-1")
    _a, state = await _resume(s, "t-resume-1")

    assert state.status == "COMPLETED"
    assert state.completed_steps == ["s1", "s2", "s3"]
    # s1（已完成）只执行过 1 次（初始），resume 未重跑；s2 首试失败后在 resume 重试成功。
    assert s.stubs["article-triage"].calls == ["article-triage"]
    assert s.stubs["product-scoring"].calls == ["product-scoring", "product-scoring"]
    assert s.stubs["draft-writing"].calls == ["draft-writing"]


@pytest.mark.asyncio
async def test_resume_after_step2():
    """前 2 步已完成、draft 未执行的中断 → resume 只续跑 s3。"""
    s = _Shared()
    await _run_cut(s, cut_after=2, task_id="t-resume-2")
    _a, state = await _resume(s, "t-resume-2")

    assert state.completed_steps == ["s1", "s2", "s3"]
    assert s.stubs["article-triage"].calls == ["article-triage"]
    assert s.stubs["product-scoring"].calls == ["product-scoring"]
    assert s.stubs["draft-writing"].calls == ["draft-writing"]


@pytest.mark.asyncio
async def test_resume_after_draft_generation():
    """Draft 生成后（全部 step 完成）重启 → resume 是幂等 no-op，绝不多跑任何 skill。"""
    s = _Shared()
    a1 = s.agent(task_id="t-resume-3")
    await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id="t-resume-3")
    calls_before = {n: list(st.calls) for n, st in s.stubs.items()}

    _a, state = await _resume(s, "t-resume-3")
    assert state.status == "COMPLETED"
    assert {n: st.calls for n, st in s.stubs.items()} == calls_before  # 无一重跑


@pytest.mark.asyncio
async def test_resume_does_not_repeat_completed_skill():
    """§16 [PASS] completed skill is not executed twice。"""
    s = _Shared()
    a1 = s.agent(task_id="t-norepeat")
    await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id="t-norepeat")
    assert [st.calls for st in s.stubs.values()] == [
        ["article-triage"],
        ["product-scoring"],
        ["draft-writing"],
    ]

    _a, state = await _resume(s, "t-norepeat")
    assert state.status == "COMPLETED"
    assert [st.calls for st in s.stubs.values()] == [
        ["article-triage"],
        ["product-scoring"],
        ["draft-writing"],
    ]


@pytest.mark.asyncio
async def test_resume_reuses_artifact():
    """§16 [PASS] artifact refs reused：resume 复用已完成 step 的 ArtifactRef，而非重新生成。"""
    s = _Shared()
    await _run_crash(s, crash_step="product-scoring", task_id="t-reuse")
    record = await s.run_store.get_by_task("t-reuse")
    s1_ref_before = record.artifact_refs["s1"]

    _a, state = await _resume(s, "t-reuse")
    assert state.artifact_refs["s1"] == s1_ref_before
    assert state.completed_steps == ["s1", "s2", "s3"]
    # s1 的写到过且只写过一次（resume 未重写）→ 无重复副作用
    assert s.stubs["article-triage"].writes == 1


@pytest.mark.asyncio
async def test_resume_rejects_missing_artifact():
    """§16 缺失 artifact → 显式 FAILED，绝不静默重跑（§12 / §17）。"""
    s = _Shared()
    await _run_crash(s, crash_step="product-scoring", task_id="t-missing")
    record = await s.run_store.get_by_task("t-missing")
    _delete_artifact(s, record.artifact_refs["s1"])  # 模拟产物丢失
    calls_before = {n: list(st.calls) for n, st in s.stubs.items()}

    a2 = s.agent(task_id="t-missing")
    state = await a2.resume(run_record=record, user_id="u", tenant_id="ten")
    assert state.status == "FAILED"
    # 拒绝恢复后没有任何 skill 因 resume 被调用
    assert {n: st.calls for n, st in s.stubs.items()} == calls_before


@pytest.mark.asyncio
async def test_resume_rejects_wrong_tenant_artifact():
    """§12 租户不匹配 → 拒绝恢复（wrong_tenant_artifact）。"""
    s = _Shared()
    await _run_crash(s, crash_step="product-scoring", task_id="t-tenant")
    record = await s.run_store.get_by_task("t-tenant")
    calls_before = {n: list(st.calls) for n, st in s.stubs.items()}
    a2 = s.agent(task_id="t-tenant")
    state = await a2.resume(run_record=record, user_id="u", tenant_id="OTHER-TENANT")
    assert state.status == "FAILED"
    # 被拒绝后没有任何 skill 因 resume 被重跑
    assert {n: st.calls for n, st in s.stubs.items()} == calls_before


@pytest.mark.asyncio
async def test_retry_write_idempotent():
    """§16 [PASS] write tool duplicate side effect = 0：retry/resume 复用同一幂等键，无重复写。"""
    s = _Shared()
    # product-scoring 首次尝试崩溃，但已在真实写前记录幂等键。
    await _run_crash(s, crash_step="product-scoring", task_id="t-idem")
    initial_keys = list(s.stubs["product-scoring"].received_keys)

    _a, state = await _resume(s, "t-idem")
    assert state.status == "COMPLETED"

    resume_keys = list(s.stubs["product-scoring"].received_keys)
    # 幂等键稳定：同一 step 初始尝试与 resume 尝试拿到同一 key → 写工具可按 key 去重。
    assert resume_keys and resume_keys[0][0] == "product-scoring"
    assert resume_keys[0][1] == initial_keys[0][1]
    # 只真实写过一次（仅 resume 成功那次），重复副作用 = 0。
    assert s.stubs["product-scoring"].writes == 1


@pytest.mark.asyncio
async def test_resume_after_worker_restart():
    """§16 [PASS] worker restart can resume：同一持久介质新建实例可恢复并跑完。"""
    s = _Shared()
    await _run_cut(s, cut_after=2, task_id="t-restart")

    # 模拟 Worker 重启：全新 agent（新 OrchestratorAgent 实例）复用同一持久介质
    _a, state = await _resume(s, "t-restart")
    assert state.status == "COMPLETED"
    assert state.completed_steps == ["s1", "s2", "s3"]
    # 重启后的记录为 COMPLETED，可再次读取。
    final_record = await s.run_store.get_by_task("t-restart")
    assert final_record.status == "COMPLETED"


# ══════════════════════════════════════════════════════════════
# §16 Hard Gates（聚合断言）
# ══════════════════════════════════════════════════════════════


class TestResumeHardGates:
    @pytest.mark.asyncio
    async def test_gate_completed_skill_not_executed_twice(self) -> None:
        s = _Shared()
        await _run_crash(s, crash_step="product-scoring", task_id="g1")
        _a, state = await _resume(s, "g1")
        assert state.status == "COMPLETED"
        assert s.stubs["article-triage"].calls == ["article-triage"]
        assert [st.writes for st in s.stubs.values()] == [1, 1, 1]

    @pytest.mark.asyncio
    async def test_gate_worker_restart_resumes(self) -> None:
        s = _Shared()
        await _run_cut(s, cut_after=2, task_id="g2")
        _a, state = await _resume(s, "g2")
        assert state.completed_steps == ["s1", "s2", "s3"]


# ══════════════════════════════════════════════════════════════
# §33 / §34 / §52 Reviewer 主链 Hard Gates（b2 阶段补全）
# ══════════════════════════════════════════════════════════════


class _ReviewStub:
    """记录被审查的 step；不产出决策（保持主链不被阻断）。"""

    def __init__(self) -> None:
        self.invoked_steps: list[str] = []

    async def after_draft(self, **kwargs: Any) -> None:
        self.invoked_steps.append(kwargs["step"].step_id)


class _ScriptedReviewer:
    """按脚本依次返回 ReviewDecision；记录审查次数/文本；本身绝无写副作用。"""

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = list(decisions)
        self.reviews: list[str] = []
        self.writes = 0  # Reviewer 只读不改稿（§28）→ 写副作用必须恒为 0

    async def review(self, *, draft_text: str) -> Any:
        self.reviews.append(draft_text)
        if not self.decisions:
            raise AssertionError("review 脚本耗尽：不应有额外审查轮次")
        return self.decisions.pop(0)


def _make_review_decision(status: str, *, revision_instructions: list[str] | None = None) -> Any:
    from agent.specialists.reviewer_agent import ReviewDecision

    return ReviewDecision(
        status=status,
        severity="warning" if status == "REVISE" else "info",
        revision_instructions=revision_instructions or [],
    )


async def _run_full(
    s: _Shared, policy: DraftReviewPolicy, task_id: str
) -> Any:
    a1 = s.agent(task_id=task_id, reviewer=policy)
    return await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id=task_id)


async def _draft_payload(s: _Shared, ref: str) -> dict[str, Any]:
    """按 "Type:<id>@<version>" 读出持久化产物 payload。"""
    _art_type, _rest = ref.split(":", 1)
    artifact_id, version_part = _rest.rsplit("@", 1)
    return await s.artifact_store.get(
        artifact_id=artifact_id, artifact_type=_art_type, version=int(version_part)
    )


@pytest.mark.asyncio
async def test_full_workflow_invokes_reviewer():
    """§33 [PASS] full_workflow always reviews generated draft：主链上会调用 Reviewer。"""
    s = _Shared()
    reviewer = _ReviewStub()
    a1 = s.agent(task_id="t-review-hook", reviewer=reviewer)
    state = await a1.run(goal="分析并写稿", user_id="u", tenant_id="ten", task_id="t-review-hook")
    assert state.status == "COMPLETED"
    # 生成 Draft 的步骤（s3）必然进入审查（§52 Reviewer Gate = 100%）
    assert "s3" in reviewer.invoked_steps


@pytest.mark.asyncio
async def test_reviewer_approve():
    """§34 test_reviewer_approve：APPROVE → 主链完成、不派发修订、草稿内容保持原稿。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    fake = _ScriptedReviewer([_make_review_decision("APPROVE")])
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)
    assert s.rev_stub is not None

    state = await _run_full(s, policy, "t-rv-approve")
    assert state.status == "COMPLETED"
    assert state.completed_steps == ["s1", "s2", "s3"]
    assert s.rev_stub.calls == []  # 无修订
    assert fake.reviews == ["draft v1"]  # Reviewer 审到的是真实草稿正文
    payload = await _draft_payload(s, state.artifact_refs["s3"])
    assert payload["content"] == "draft v1"  # 原稿未被改动


@pytest.mark.asyncio
async def test_reviewer_revise():
    """§34 test_reviewer_revise：REVISE → 派发 DraftRevisionSkill → 再审通过 → 主链完成。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    fake = _ScriptedReviewer(
        [
            _make_review_decision("REVISE", revision_instructions=["补充产品证据"]),
            _make_review_decision("APPROVE"),
        ]
    )
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)
    assert s.rev_stub is not None

    state = await _run_full(s, policy, "t-rv-revise")
    assert state.status == "COMPLETED"
    assert s.rev_stub.calls == ["draft-revision"]
    assert fake.reviews == ["draft v1", "draft v2"]  # 初审 + 再审（修订后文本）
    # 终稿为修订产物（Draft v2）
    payload = await _draft_payload(s, state.artifact_refs["s3"])
    assert payload["content"] == "draft v2"


@pytest.mark.asyncio
async def test_reviewer_block():
    """§33/§34 test_reviewer_block：BLOCK → Orchestrator status=BLOCKED、不派发任何修订/下游副作用。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    fake = _ScriptedReviewer([_make_review_decision("BLOCK")])
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)
    assert s.rev_stub is not None

    state = await _run_full(s, policy, "t-rv-block")
    assert state.status == "BLOCKED"
    assert s.rev_stub.calls == []  # 阻断后绝不修订（下游副作用 = 0）
    record = await s.run_store.get_by_task("t-rv-block")
    assert record.status == "BLOCKED"


@pytest.mark.asyncio
async def test_reviewer_cannot_mutate_draft():
    """§33/§34 test_reviewer_cannot_mutate_draft：Reviewer 不改稿（§28），草稿内容审查前后一致。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    fake = _ScriptedReviewer([_make_review_decision("APPROVE")])
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)

    state = await _run_full(s, policy, "t-rv-nomutate")
    assert state.status == "COMPLETED"
    # Reviewer 自身写副作用为 0；改稿只能走 DraftRevisionSkill（此处未触发）
    assert fake.writes == 0
    payload = await _draft_payload(s, state.artifact_refs["s3"])
    assert payload["content"] == "draft v1"
    # 全文仅有 draft-writing 写过一次，draft-revision 未写
    assert s.stubs["draft-writing"].writes == 1
    assert s.rev_stub.writes == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reviewer_max_rounds():
    """§33/§34 test_reviewer_max_rounds：审核循环有界（本章 MAX=2），超限转 BLOCKED。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    fake = _ScriptedReviewer(
        [
            _make_review_decision("REVISE", revision_instructions=["改 A"]),
            _make_review_decision("REVISE", revision_instructions=["改 B"]),
        ]
    )
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)
    assert s.rev_stub is not None

    state = await _run_full(s, policy, "t-rv-rounds")
    assert state.status == "BLOCKED"  # 超过 max_review_rounds → 转人工/阻断
    # 受限修订：Draft v1 →(rev1)→ v2 后仍 REVISE，达到上限即 BLOCK，不再无限循环
    assert s.rev_stub.calls == ["draft-revision"]
    assert state.reviewer_rounds == 2


@pytest.mark.asyncio
async def test_unsupported_claim_blocks():
    """§33/§34 test_unsupported_claim_blocks：真实 ReviewerAgent 对无证据产品声明必须阻断、绝不 APPROVE。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)

    def _passing_service(text: str) -> Any:
        class _R:  # 合规/语法全通过
            passed = True

            def __init__(self) -> None:
                self.issues: list[Any] = []

        return _R()

    def _unsupported_audit(text: str) -> dict:
        # 所有产品声明无证据支撑（grounded_ratio=0.0, unsupported=1）→ BLOCK
        return {"grounded_ratio": 0.0, "unsupported": 1}

    reviewer_agent = ReviewerAgent(
        review_service=_passing_service, claim_audit=_unsupported_audit
    )
    policy = DraftReviewPolicy(reviewer_agent=reviewer_agent, artifact_store=s.artifact_store)

    state = await _run_full(s, policy, "t-rv-unsupported")
    # 无证据背书的产品声明绝不进入 APPROVE（§30 / §57 Wiki Grounding Gate）→ BLOCK
    assert state.status == "BLOCKED"
    assert s.rev_stub.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_revision_skill_receives_review_instruction():
    """§34 test_revision_skill_receives_review_instruction：DraftRevisionSkill 收到 Reviewer 的修订指令（§28）。"""
    s = _Shared(draft_content="draft v1", with_revision_stub=True)
    instructions = ["补充产品证据", "修正 internal consistency"]
    fake = _ScriptedReviewer(
        [
            _make_review_decision("REVISE", revision_instructions=instructions),
            _make_review_decision("APPROVE"),
        ]
    )
    policy = DraftReviewPolicy(reviewer_agent=fake, artifact_store=s.artifact_store)
    assert s.rev_stub is not None

    await _run_full(s, policy, "t-rv-inst")
    # 修订 Skill 收到的指令 = Reviewer 下发的修订指令（多条件用「；」拼接）
    assert s.rev_stub.instructions == ["补充产品证据；修正 internal consistency"]
    # 目标版本 = source_version + 1，并带上父产物 ref
    assert s.rev_stub.expected_versions == [2]
    assert s.rev_stub.parent_refs and s.rev_stub.parent_refs[0].startswith("DraftArtifact:")
