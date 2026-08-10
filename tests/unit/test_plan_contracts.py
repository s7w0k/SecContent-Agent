"""Plan Schema 与 PlanValidator 单元测试 — 阶段三 Step 1/2。"""

from __future__ import annotations

import pytest

from agent.plan_contracts import (
    ALLOWED_INPUT_KEYS,
    DEFAULT_MAX_RATIONALE_CHARS,
    FORBIDDEN_WORKERS,
    PlanStep,
    PlanValidationResult,
    PlanValidator,
    PipelinePlan,
    build_default_plan,
    input_snapshot_hash,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _default_plan(**kwargs) -> PipelinePlan:
    kwargs.setdefault("user_id", "u-1")
    kwargs.setdefault("product_ids", ["agent-identity-security"])
    kwargs.setdefault("article_ids", ["art-1", "art-2"])
    kwargs.setdefault("needs_fulltext", True)
    return build_default_plan(
        run_id="run-1",
        input_snapshot_hash_value=input_snapshot_hash(
            user_id=kwargs["user_id"],
            product_ids=kwargs["product_ids"],
        ),
        **kwargs,
    )


def _step(
    step_id: str,
    worker: str,
    depends_on: list[str] | None = None,
    input_refs: dict | None = None,
    policy: str = "required",
    timeout_s: int = 300,
    max_attempts: int = 3,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        worker=worker,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        input_refs=input_refs or {},
        policy=policy,  # type: ignore[arg-type]
        timeout_s=timeout_s,
        max_attempts=max_attempts,
    )


def _make_plan(steps: list[PlanStep], **kwargs) -> PipelinePlan:
    return PipelinePlan(
        schema_version=kwargs.pop("schema_version", "1.0"),
        plan_id=kwargs.pop("plan_id", "plan-1"),
        run_id=kwargs.pop("run_id", "run-1"),
        planner_version=kwargs.pop("planner_version", "test-v1"),
        input_snapshot_hash=kwargs.pop(
            "input_snapshot_hash",
            input_snapshot_hash(user_id="u-1", product_ids=["agent-identity-security"]),
        ),
        steps=steps,
        rationale_summary=kwargs.pop("rationale_summary", "test"),
    )


def _valid_steps() -> list[PlanStep]:
    """合法计划（含 draft guard 路径）。"""
    return [
        _step("s1", "crawl", [], {"crawl_days": 1}),
        _step("s2", "classify", ["s1"], {"article_ids": ["art-1"]}),
        _step("s3", "filter", ["s2"], {"article_ids": ["art-1"]}),
        _step("s4", "score", ["s3"], {"article_ids": ["art-1"], "product_ids": ["agent-identity-security"]}),
        _step("s5", "draft", ["s4"], {"article_ids": ["art-1"], "product_ids": ["agent-identity-security"]}),
        _step("s6", "quality_check", ["s5"], {"article_ids": ["art-1"]}),
        _step("s7", "review", ["s6"], {"article_ids": ["art-1"]}),
    ]


def _make_plan_unvalidated(steps: list[PlanStep]) -> PipelinePlan:
    """用 model_construct 绕过 Pydantic 校验（验证 validator 防御纵深）。"""
    return PipelinePlan.model_construct(
        schema_version="1.0",
        plan_id="plan-1",
        run_id="run-1",
        planner_version="test-v1",
        input_snapshot_hash="h" * 64,
        steps=steps,
        rationale_summary="test",
    )


class TestPlanSchema:
    def test_default_plan_structure(self):
        plan = _default_plan()
        assert plan.schema_version == "1.0"
        assert plan.run_id == "run-1"
        assert plan.input_snapshot_hash.startswith("sha256:")
        assert len(plan.steps) == 9  # 含 enrich
        workers = [s.worker for s in plan.steps]
        assert workers == [
            "crawl", "enrich", "classify", "filter",
            "score", "draft", "quality_check", "rewrite", "review",
        ]

    def test_default_plan_without_fulltext(self):
        plan = build_default_plan(
            run_id="run-1",
            input_snapshot_hash_value="h",
            needs_fulltext=False,
        )
        assert len(plan.steps) == 8  # 无 enrich

    def test_plan_hash_stable(self):
        a = _default_plan()
        b = _default_plan()
        assert a.plan_hash == b.plan_hash

    def test_plan_hash_changes_with_intent(self):
        a = _default_plan()
        b = _default_plan(product_ids=["agent-security"])
        assert a.plan_hash != b.plan_hash

    def test_plan_step_self_dependency_rejected(self):
        with pytest.raises(ValueError):
            PlanStep(
                step_id="s1", worker="crawl", depends_on=["s1"],
                input_refs={}, policy="required", timeout_s=60, max_attempts=3,
            )

    def test_rationale_length_limited(self):
        with pytest.raises(ValueError):
            _make_plan(
                _valid_steps(),
                rationale_summary="x" * (DEFAULT_MAX_RATIONALE_CHARS + 1),
            )

    def test_input_snapshot_hash_deterministic(self):
        h1 = input_snapshot_hash(user_id="u-1", product_ids=["a", "b"])
        h2 = input_snapshot_hash(user_id="u-1", product_ids=["b", "a"])
        assert h1 == h2


class TestValidatorPass:
    def test_default_plan_valid(self):
        result = PlanValidator().validate(
            _default_plan(),
            expected_run_id="run-1",
        )
        assert not result.rejected, result.reason
        assert result.plan_hash == _default_plan().plan_hash

    def test_valid_steps_pass(self):
        result = PlanValidator().validate(
            _make_plan(_valid_steps()),
            expected_run_id="run-1",
            allowed_products={"agent-identity-security"},
            allowed_article_ids={"art-1"},
        )
        assert not result.rejected, result.reason


class TestValidatorIdentity:
    def test_run_id_mismatch(self):
        result = PlanValidator().validate(_default_plan(), expected_run_id="other-run")
        assert result.rejected and "run_id" in result.reason

    def test_input_snapshot_mismatch(self):
        result = PlanValidator().validate(
            _default_plan(),
            expected_input_snapshot_hash="sha256:wrong",
        )
        assert result.rejected and "input_snapshot" in result.reason

    def test_user_id_impersonation_rejected(self):
        steps = _valid_steps()
        steps = [
            PlanStep(**{**s.model_dump(), "input_refs": {**s.input_refs, "user_id": "u-2"}})
            for s in steps
        ]
        result = PlanValidator().validate(_make_plan(steps), allow_user_id="u-1")
        assert result.rejected and "user_id" in result.reason

    def test_bad_schema_version_rejected(self):
        plan = PipelinePlan.model_construct(
            schema_version="2.0",
            plan_id="plan-1",
            run_id="run-1",
            planner_version="test-v1",
            input_snapshot_hash="h" * 64,
            steps=_valid_steps(),
            rationale_summary="test",
        )
        result = PlanValidator().validate(plan)
        assert result.rejected and "schema_version" in result.reason


class TestValidatorWhitelist:
    def test_product_not_allowed(self):
        steps = _valid_steps()
        steps = [
            PlanStep(**{**s.model_dump(), "input_refs": {**s.input_refs, "product_ids": ["evil-product"]}})
            for s in steps
        ]
        result = PlanValidator().validate(_make_plan(steps), allowed_products={"agent-identity-security"})
        assert result.rejected and "product not allowed" in result.reason

    def test_article_not_allowed(self):
        steps = _valid_steps()
        steps = [
            PlanStep(**{**s.model_dump(), "input_refs": {**s.input_refs, "article_ids": ["evil-art"]}})
            for s in steps
        ]
        result = PlanValidator().validate(_make_plan(steps), allowed_article_ids={"art-1"})
        assert result.rejected and "article not allowed" in result.reason

    def test_disallowed_input_key(self):
        steps = _valid_steps()
        steps = [
            PlanStep(**{**s.model_dump(), "input_refs": {**s.input_refs, "sql": "DROP TABLE"}})
            for s in steps
        ]
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "disallowed input key" in result.reason

    def test_worker_contract_violation(self):
        """crawl 不允许引用 product_ids（输入契约外）。"""
        steps = _valid_steps()
        steps[0] = PlanStep(**{**steps[0].model_dump(), "input_refs": {"product_ids": ["x"]}})
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "unexpected inputs" in result.reason


class TestValidatorTopology:
    def test_duplicate_step_id(self):
        steps = _valid_steps()
        steps[1] = PlanStep(**{**steps[1].model_dump(), "step_id": "s1", "depends_on": ["s3"]})
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "duplicate step_id" in result.reason

    def test_missing_dependency(self):
        steps = _valid_steps()
        steps[1] = PlanStep(**{**steps[1].model_dump(), "depends_on": ["ghost"]})
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "missing dependency" in result.reason

    def test_cycle_detected(self):
        steps = [
            _step("a", "crawl", ["b"], {"crawl_days": 1}),
            _step("b", "classify", ["a"], {}),
        ]
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "cycle" in result.reason

    def test_max_steps_exceeded(self):
        validator = PlanValidator(max_steps=5)
        plan = _make_plan(_valid_steps())  # 7 steps
        result = validator.validate(plan)
        assert result.rejected and "max_steps" in result.reason

    def test_max_depth_exceeded(self):
        validator = PlanValidator(max_depth=2)
        result = validator.validate(_make_plan(_valid_steps()))
        assert result.rejected and "depth" in result.reason

    def test_max_fanout_exceeded(self):
        validator = PlanValidator(max_fanout=2)
        steps = [_step("root", "crawl", [], {"crawl_days": 1})]
        for i in range(3):
            steps.append(_step(f"c{i}", "classify", ["root"], {}))
        result = validator.validate(_make_plan(steps))
        assert result.rejected and "fanout" in result.reason


class TestValidatorDraftGuard:
    def test_draft_without_quality_check(self):
        """quality_check 被替换为 rewrite（保持拓扑完整）→ guard 缺失。"""
        steps = _valid_steps()
        steps = [
            PlanStep(
                **{
                    **s.model_dump(),
                    "worker": "rewrite",
                }
            )
            if s.worker == "quality_check"
            else s
            for s in steps
        ]
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "guard" in result.reason

    def test_draft_without_review(self):
        steps = _valid_steps()
        steps = [s for s in steps if s.worker != "review"]
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "guard" in result.reason

    def test_guard_before_draft_rejected(self):
        """review 出现在 draft 之前。"""
        steps = [
            _step("s1", "crawl", [], {"crawl_days": 1}),
            _step("s2", "review", ["s1"], {}),
            _step("s3", "draft", ["s2"], {}),
            _step("s4", "quality_check", ["s3"], {}),
        ]
        result = PlanValidator().validate(_make_plan(steps))
        assert result.rejected and "must follow" in result.reason


class TestValidatorForbiddenWorkers:
    def _plan_with_worker(self, worker_name: str) -> PipelinePlan:
        """构造含禁止 Worker 的未校验计划（绕过 Pydantic Literal）。"""
        steps = [
            PlanStep.model_construct(
                step_id=f"s{i}",
                worker="crawl",
                depends_on=[],
                input_refs={},
                policy="required",
                timeout_s=60,
                max_attempts=3,
            )
            for i in range(1, 4)
        ]
        steps.append(
            PlanStep.model_construct(
                step_id="s9",
                worker=worker_name,
                depends_on=["s3"],
                input_refs={},
                policy="required",
                timeout_s=60,
                max_attempts=3,
            )
        )
        return _make_plan_unvalidated(steps)

    def test_publish_worker_rejected(self):
        result = PlanValidator().validate(self._plan_with_worker("publish"))
        assert result.rejected and "forbidden" in result.reason

    def test_external_send_rejected(self):
        result = PlanValidator().validate(self._plan_with_worker("external_send"))
        assert result.rejected and "forbidden" in result.reason

    def test_delete_worker_rejected(self):
        result = PlanValidator().validate(self._plan_with_worker("delete"))
        assert result.rejected and "forbidden" in result.reason

    def test_forbidden_worker_names_constant(self):
        assert FORBIDDEN_WORKERS == {"publish", "delete", "external_send", "notify"}


class TestValidatorBudgets:
    def test_concurrency_groups_exceeded(self):
        validator = PlanValidator(max_concurrency_groups=2)
        steps = _valid_steps()
        steps = [
            PlanStep(**{**s.model_dump(), "concurrency_key": f"grp-{i}"})
            for i, s in enumerate(steps)
        ]
        result = validator.validate(_make_plan(steps))  # 7 groups
        assert result.rejected and "concurrency groups" in result.reason

    def test_total_timeout_exceeded(self):
        validator = PlanValidator(max_total_timeout_s=100)
        result = validator.validate(_make_plan(_valid_steps()))
        assert result.rejected and "total timeout" in result.reason


class TestValidatorSkips:
    def test_optional_ratio_exceeded(self):
        validator = PlanValidator(max_optional_ratio=0.2)
        steps = _valid_steps()
        steps[1] = PlanStep(**{**steps[1].model_dump(), "policy": "optional"})
        steps[2] = PlanStep(**{**steps[2].model_dump(), "policy": "best_effort"})
        result = validator.validate(_make_plan(steps))  # 2/7 > 20%
        assert result.rejected and "optional steps" in result.reason

    def test_special_handling_exceeded(self):
        validator = PlanValidator(max_special_handling=2)
        steps = _valid_steps()
        steps.insert(1, _step("s1e", "enrich", ["s1"], {"needs_fulltext": ["a", "b", "c"]}))
        result = validator.validate(_make_plan(steps))
        assert result.rejected and "special handling" in result.reason


class TestFallbackSemantics:
    def test_rejected_plan_falls_back_to_default(self):
        """违规计划被拒后，服务端回退默认计划且默认计划本身合法。"""
        bad = _valid_steps()
        bad = [s for s in bad if s.worker != "review"]
        result = PlanValidator().validate(_make_plan(bad))
        assert result.rejected

        fallback = _default_plan()
        fb_result = PlanValidator().validate(fallback, expected_run_id="run-1")
        assert not fb_result.rejected, fb_result.reason
        assert result.plan_hash != fb_result.plan_hash

    def test_validation_result_fields(self):
        result = PlanValidator().validate(_make_plan(_valid_steps()))
        assert isinstance(result, PlanValidationResult)
        assert result.reason == "ok"
        assert result.plan_hash.startswith("sha256:")


class TestAllowedInputKeys:
    def test_allowed_keys_are_explicit(self):
        assert "sql" not in ALLOWED_INPUT_KEYS
        assert "command" not in ALLOWED_INPUT_KEYS
        assert "url" not in ALLOWED_INPUT_KEYS
        assert "path" not in ALLOWED_INPUT_KEYS
