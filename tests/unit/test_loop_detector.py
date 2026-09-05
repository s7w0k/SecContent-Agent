"""LoopDetector 单元测试 -- 阶段1 1.3 节（六类无进展/循环检测）。"""

from __future__ import annotations

from agent.loop_detector import LoopDetector, LoopSignal


class TestExactRepeat:
    def test_second_identical_action_hits_replan(self):
        detector = LoopDetector()
        assert detector.observe_action(tool_name="t", args_hash="h1") is None
        d1 = detector.observe_action(tool_name="t", args_hash="h1")
        assert d1 is not None
        assert d1.signal == LoopSignal.EXACT_REPEAT
        assert d1.should_replan and not d1.should_stop
        assert d1.hit_count == 1

    def test_third_identical_action_stops(self):
        detector = LoopDetector()
        detector.observe_action(tool_name="t", args_hash="h1")
        detector.observe_action(tool_name="t", args_hash="h1")
        d2 = detector.observe_action(tool_name="t", args_hash="h1")
        assert d2 is not None
        assert d2.should_stop
        assert d2.hit_count == 2


class TestSameResult:
    def test_same_result_hash_different_args(self):
        detector = LoopDetector(same_result_window=5)
        for i in range(1, 5):  # a1~a4 未达窗口
            assert (
                detector.observe_action(
                    tool_name="t", args_hash=f"a{i}", result_hash="R", new_evidence_count=1
                )
                is None
            )
        d = detector.observe_action(
            tool_name="t", args_hash="a5", result_hash="R", new_evidence_count=1
        )
        assert d is not None
        assert d.signal == LoopSignal.SAME_RESULT
        assert d.detail.get("count", 0) >= 5


class TestNoNewEvidence:
    def test_three_steps_no_evidence(self):
        detector = LoopDetector(max_no_progress_steps=3)
        detector.observe_action(tool_name="t1", args_hash="a1", new_evidence_count=0)
        detector.observe_action(tool_name="t2", args_hash="a2", new_evidence_count=0)
        d = detector.observe_action(tool_name="t3", args_hash="a3", new_evidence_count=0)
        assert d is not None
        assert d.signal == LoopSignal.NO_NEW_EVIDENCE

    def test_evidence_resets_detection(self):
        detector = LoopDetector(max_no_progress_steps=3)
        detector.observe_action(tool_name="t1", args_hash="a1", new_evidence_count=0)
        detector.observe_action(tool_name="t2", args_hash="a2", new_evidence_count=2)
        assert detector.observe_action(tool_name="t3", args_hash="a3", new_evidence_count=0) is None


class TestSameError:
    def test_same_error_three_times(self):
        detector = LoopDetector(same_error_window=3)
        detector.observe_action(
            tool_name="t1", args_hash="a1", error_code="timeout", new_evidence_count=1
        )
        detector.observe_action(
            tool_name="t2", args_hash="a2", error_code="timeout", new_evidence_count=1
        )
        d = detector.observe_action(
            tool_name="t3", args_hash="a3", error_code="timeout", new_evidence_count=1
        )
        assert d is not None
        assert d.signal == LoopSignal.SAME_ERROR


class TestPlanOscillation:
    def test_abab_oscillation(self):
        detector = LoopDetector(plan_oscillation_window=4)
        detector.observe_action(
            tool_name="t1", args_hash="a1", plan_state="A", new_evidence_count=1
        )
        detector.observe_action(
            tool_name="t2", args_hash="a2", plan_state="B", new_evidence_count=1
        )
        detector.observe_action(
            tool_name="t3", args_hash="a3", plan_state="A", new_evidence_count=1
        )
        d = detector.observe_action(
            tool_name="t4", args_hash="a4", plan_state="B", new_evidence_count=1
        )
        assert d is not None
        assert d.signal == LoopSignal.PLAN_OSCILLATION


class TestStalledCoverage:
    def test_coverage_stalled(self):
        detector = LoopDetector(stalled_coverage_window=3)
        detector.observe_action(tool_name="t1", args_hash="a1", coverage=0.5, new_evidence_count=1)
        detector.observe_action(tool_name="t2", args_hash="a2", coverage=0.5, new_evidence_count=1)
        d = detector.observe_action(
            tool_name="t3", args_hash="a3", coverage=0.5, new_evidence_count=1
        )
        assert d is not None
        assert d.signal == LoopSignal.STALLED_COVERAGE


class TestReset:
    def test_reset_clears_action_history(self):
        detector = LoopDetector()
        detector.observe_action(tool_name="t", args_hash="h1")
        detector.observe_action(tool_name="t", args_hash="h1")
        detector.reset()
        assert detector.observe_action(tool_name="t", args_hash="h1") is None
