"""容量模型与负载模拟 — 阶段4 §5（WBS 4.6）。

输出容量模型：
  - max concurrent runs（最大并发运行数）
  - max LLM calls/s（最大模型调用吞吐）
  - max tokens/min（最大 token 吞吐）
  - max tool calls/s（最大工具调用吞吐）
  - queue depth threshold（队列深度告警阈值，超过即饱和）
  - estimated USD/day at 1% / 10% / 50% / 100%（各灰度档位日成本）

组成：
  - CapacityInputs：容量模型输入（生产值取自 config，测试可注入）
  - CapacityModel / CapacityReport：静态容量计算（含成本表）
  - run_load_simulation：无真实流量的负载模拟（到达率 / 队列堆积 /
    provider 慢响应 / 重试风暴与熔断降级 / 队列恢复）
  - SCENARIO_PRESETS：压测场景模板（短问答 / 高上下文 / 多工具 / 多 Agent）

安全约束：容量计算只输出边界与建议，不做任何自动限流/熔断副作用；
实际的限流拒绝逻辑由 ModelRateLimiter / worker 队列实现。
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from typing import Any

from agent.pricing_catalog import compute_cost

_INF = float("inf")


@dataclass(frozen=True)
class CapacityInputs:
    """容量模型输入参数。

    生产值应与 config 对齐（ORCHESTRATOR_MAX_CONCURRENCY 等），
    测试时直接注入构造即可，无需加载全局配置。
    """

    worker_concurrency: int = 5  # 全局最大并发 Worker（≈最大并发 run）
    llm_calls_per_run: int = 6  # 平均每次 run 的 LLM 调用次数
    tool_calls_per_run: int = 4  # 平均每次 run 的工具调用次数
    input_tokens_per_call: int = 12_000  # 平均单次 LLM 调用输入 token
    output_tokens_per_call: int = 800  # 平均单次 LLM 调用输出 token
    cached_input_ratio: float = 0.4  # 输入缓存命中比例（0-1）
    llm_p95_latency_ms: float = 5_000.0  # 单次 LLM 调用 p95 延迟（ms）
    tool_p95_latency_ms: float = 3_000.0  # 单次工具调用 p95 延迟（ms）
    provider_llm_calls_per_second: float = 0.0  # provider 模型调用吞吐上限；0=不限制
    provider_tokens_per_minute: float = 0.0  # provider token 吞吐上限；0=不限制
    safety_factor: float = 0.8  # 容量安全系数（0-1，预留熔断/重试缓冲）
    model_id: str = "deepseek-chat"  # 成本估算用模型

    def __post_init__(self) -> None:
        if self.worker_concurrency < 1:
            raise ValueError("worker_concurrency 必须 >= 1")
        if self.llm_calls_per_run < 0 or self.tool_calls_per_run < 0:
            raise ValueError("llm/tool calls per run 不能为负")
        if not 0.0 <= self.cached_input_ratio <= 1.0:
            raise ValueError("cached_input_ratio 必须在 [0,1]")
        if not 0.0 < self.safety_factor <= 1.0:
            raise ValueError("safety_factor 必须在 (0,1]")
        if self.llm_p95_latency_ms <= 0 or self.tool_p95_latency_ms <= 0:
            raise ValueError("延迟必须为正")

    def run_duration_seconds(self) -> float:
        """估算一次完整 run 的墙钟时长（LLM 串行 + 工具串行 + 调度开销）。"""
        llm_time = self.llm_calls_per_run * self.llm_p95_latency_ms / 1000
        tool_time = self.tool_calls_per_run * self.tool_p95_latency_ms / 1000
        return max(0.1, llm_time + tool_time + 0.5)

    def sustainable_runs_per_second(self) -> float:
        """理论可持续吞吐（无排队时）。"""
        return self.worker_concurrency / self.run_duration_seconds()

    def queue_depth_threshold(self) -> int:
        """队列深度阈值：排队 run 数超过并发容量即视为饱和。"""
        return max(1, int(self.worker_concurrency))

    def tokens_per_run(self) -> tuple[int, int, int]:
        """每次 run 的 (输入 token, 缓存命中输入 token, 输出 token)。"""
        input_tokens = self.llm_calls_per_run * self.input_tokens_per_call
        output_tokens = self.llm_calls_per_run * self.output_tokens_per_call
        cached = int(input_tokens * self.cached_input_ratio)
        return input_tokens, cached, output_tokens

    def usd_per_run(self) -> float:
        """单次 run 的估算成本（USD），基于 pricing catalog。"""
        input_tokens, cached, output_tokens = self.tokens_per_run()
        cost = compute_cost(
            self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
        )
        return float(cost["cost_usd"])


@dataclass(frozen=True)
class CapacityReport:
    """容量模型输出（§5）。"""

    max_concurrent_runs: int
    max_llm_calls_per_second: float
    max_tokens_per_minute: int
    max_tool_calls_per_second: float
    queue_depth_threshold: int
    sustainable_runs_per_second: float
    estimated_run_duration_seconds: float
    usd_per_run: float
    usd_per_day_by_rollout: dict[str, float]
    inputs: CapacityInputs = field(default_factory=CapacityInputs)

    def to_legacy_dict(self) -> dict[str, Any]:
        """转旧版 dict（供报告/CLI 输出，不含敏感信息）。"""
        return {
            "max_concurrent_runs": self.max_concurrent_runs,
            "max_llm_calls_per_second": round(self.max_llm_calls_per_second, 2),
            "max_tokens_per_minute": self.max_tokens_per_minute,
            "max_tool_calls_per_second": round(self.max_tool_calls_per_second, 2),
            "queue_depth_threshold": self.queue_depth_threshold,
            "sustainable_runs_per_second": round(self.sustainable_runs_per_second, 2),
            "estimated_run_duration_seconds": round(self.estimated_run_duration_seconds, 2),
            "usd_per_run": round(self.usd_per_run, 6),
            "usd_per_day_by_rollout": {
                k: round(v, 2) for k, v in self.usd_per_day_by_rollout.items()
            },
        }


def _cap(value: float, provider_limit: float) -> float:
    """provider 吞吐上限裁剪；0 表示不限制。"""
    if provider_limit and provider_limit > 0:
        return min(value, provider_limit)
    return value


class CapacityModel:
    """按输入参数计算容量边界（纯函数式，无副作用）。"""

    def __init__(self, inputs: CapacityInputs):
        self.inputs = inputs

    def compute(self) -> CapacityReport:
        inputs = self.inputs
        sf = inputs.safety_factor
        sustained = inputs.sustainable_runs_per_second()
        max_concurrent = max(1, int(inputs.worker_concurrency * sf))
        max_llm = _cap(sustained * inputs.llm_calls_per_run * sf,
                       inputs.provider_llm_calls_per_second)
        input_tokens, _cached, output_tokens = inputs.tokens_per_run()
        tokens_per_min = max_llm * (input_tokens + output_tokens) * 60
        max_tokens = _cap(tokens_per_min, inputs.provider_tokens_per_minute)
        max_tool = sustained * inputs.tool_calls_per_run * sf
        usd_per_day_by_rollout = estimate_usd_per_day_by_rollout(inputs)
        return CapacityReport(
            max_concurrent_runs=max_concurrent,
            max_llm_calls_per_second=round(max_llm, 2),
            max_tokens_per_minute=int(max_tokens),
            max_tool_calls_per_second=round(max_tool, 2),
            queue_depth_threshold=inputs.queue_depth_threshold(),
            sustainable_runs_per_second=round(sustained, 2),
            estimated_run_duration_seconds=round(inputs.run_duration_seconds(), 2),
            usd_per_run=round(inputs.usd_per_run(), 6),
            usd_per_day_by_rollout=usd_per_day_by_rollout,
            inputs=inputs,
        )


# 灰度档位（与 rollout_controller.STAGE_ORDER 中带百分比的档位一致）
_ROLLOUT_TIERS: tuple[float, ...] = (0.01, 0.10, 0.50, 1.00)


def estimate_usd_per_day_by_rollout(
    inputs: CapacityInputs,
    *,
    daily_runs: int = 10_000,
) -> dict[str, float]:
    """估算各灰度档位（1%/10%/50%/100%）的日成本（USD）。

    Args:
        inputs: 容量输入（usd_per_run 决定单次成本）
        daily_runs: 全天 Agent 触发总量（按 100% 口径）
    """
    usd_per_run = inputs.usd_per_run()
    return {
        f"{int(p * 100)}%": daily_runs * p * usd_per_run
        for p in _ROLLOUT_TIERS
    }


@dataclass(frozen=True)
class LoadScenario:
    """负载模拟场景参数。"""

    arrival_rps: float  # 平均到达率（run/s）
    duration_seconds: float = 120.0  # 模拟时长
    failure_ratio: float = 0.0  # 服务失败比例（provider 慢响应/5xx）
    max_attempts: int = 3  # 单任务最大尝试次数（重试风暴上限）
    retry_delay_seconds: float = 0.2  # 失败后重试间隔
    queue_cap: int = 0  # 队列上限；0=按 queue_depth_threshold
    seed: int | None = None  # 固定随机种子保证可重复

    def __post_init__(self) -> None:
        if self.arrival_rps <= 0:
            raise ValueError("arrival_rps 必须为正")
        if not 0.0 <= self.failure_ratio <= 1.0:
            raise ValueError("failure_ratio 必须在 [0,1]")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")


@dataclass(frozen=True)
class SimulationReport:
    """负载模拟结果。"""

    scenario: LoadScenario
    total_arrivals: int
    served: int
    failed: int
    retries: int
    rejected: int
    peak_queue_depth: int
    p95_queue_wait_ms: float
    utilization_ratio: float  # 0-1，worker 平均占用率
    saturation_seconds: float  # 排队/满载时长
    success_rate: float  # served / (served + failed)

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "arrival_rps": self.scenario.arrival_rps,
            "duration_seconds": self.scenario.duration_seconds,
            "failure_ratio": self.scenario.failure_ratio,
            "total_arrivals": self.total_arrivals,
            "served": self.served,
            "failed": self.failed,
            "retries": self.retries,
            "rejected": self.rejected,
            "peak_queue_depth": self.peak_queue_depth,
            "p95_queue_wait_ms": round(self.p95_queue_wait_ms, 1),
            "utilization_ratio": round(self.utilization_ratio, 3),
            "saturation_seconds": round(self.saturation_seconds, 1),
            "success_rate": round(self.success_rate, 4),
        }


def _percentile(sorted_values: list[float], q: float) -> float:
    """p 分位（sorted_values 已排序）。"""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[max(0, idx)]


def run_load_simulation(
    inputs: CapacityInputs,
    scenario: LoadScenario,
) -> SimulationReport:
    """负载模拟：到达 → 并发容量 → 队列堆积 → 重试风暴 → 队列恢复。

    无真实流量的确定性模拟（固定 seed 可重复），用于验证容量模型：
    - 到达率高于可持续吞吐时观察队列堆积与拒绝；
    - failure_ratio > 0 时观察重试放大与成功率的下降。

    返回 SimulationReport（不含任何 payload/敏感数据）。
    """
    rng = random.Random(scenario.seed)
    worker_cap = max(1, inputs.worker_concurrency)
    queue_cap = max(1, scenario.queue_cap or inputs.queue_depth_threshold())
    service_time = inputs.run_duration_seconds()
    fail_time = max(0.05, min(0.5, service_time * 0.1))  # 失败快速返回

    arrivals: list[tuple[float, bool]] = [(0.0, False)]  # (时间, 是否重试)
    completions: list[tuple[float, bool]] = []  # (完成时间, 是否失败)
    waiting: list[float] = []  # 排队到达时间（FIFO）
    now = 0.0
    base_arrivals = 0
    total_arrivals = 0
    served = 0
    failed = 0
    retries = 0
    rejected = 0
    peak_queue = 0
    wait_times: list[float] = []
    busy_accum = 0.0
    saturation_seconds = 0.0
    max_retry_budget = max(0, (scenario.max_attempts - 1))

    while now < scenario.duration_seconds:
        # 队列恢复：无占用且无人处理时，立即把排队任务放入执行
        if waiting and not completions and (not arrivals or arrivals[0][0] > now):
            ts = waiting.pop(0)
            wait_times.append(now - ts)
            is_fail = rng.random() < scenario.failure_ratio
            heapq.heappush(completions, (now + (fail_time if is_fail else service_time), is_fail))
            continue

        next_arr = arrivals[0][0] if arrivals else _INF
        next_done = completions[0][0] if completions else _INF
        event_t = min(next_arr, next_done, scenario.duration_seconds)
        if event_t > now:
            busy = len(completions)
            busy_accum += (event_t - now) * busy
            if waiting or busy >= worker_cap:
                saturation_seconds += event_t - now
            now = event_t

        if next_arr <= next_done:
            _ts, is_retry = heapq.heappop(arrivals)
            total_arrivals += 1
            if is_retry:
                retries += 1
            else:
                base_arrivals += 1
            if len(completions) < worker_cap:
                is_fail = rng.random() < scenario.failure_ratio
                heapq.heappush(
                    completions,
                    (now + (fail_time if is_fail else service_time), is_fail),
                )
            elif len(waiting) < queue_cap:
                waiting.append(now)
                peak_queue = max(peak_queue, len(waiting))
            else:
                rejected += 1
            if not is_retry:
                heapq.heappush(arrivals, (now + rng.expovariate(scenario.arrival_rps), False))
        else:
            _ts, is_fail = heapq.heappop(completions)
            if is_fail:
                failed += 1
                # 重试风暴：失败触发重试到达，但受 max_attempts 预算约束
                if retries < base_arrivals * max_retry_budget:
                    heapq.heappush(arrivals, (now + scenario.retry_delay_seconds, True))
            else:
                served += 1
            if waiting:
                ts = waiting.pop(0)
                wait_times.append(now - ts)
                is_fail = rng.random() < scenario.failure_ratio
                heapq.heappush(
                    completions,
                    (now + (fail_time if is_fail else service_time), is_fail),
                )

    completed = served + failed
    success_rate = served / completed if completed else 0.0
    utilization = busy_accum / scenario.duration_seconds / worker_cap
    p95_wait = _percentile(sorted(wait_times), 0.95) * 1000
    return SimulationReport(
        scenario=scenario,
        total_arrivals=total_arrivals,
        served=served,
        failed=failed,
        retries=retries,
        rejected=rejected,
        peak_queue_depth=peak_queue,
        p95_queue_wait_ms=p95_wait,
        utilization_ratio=min(1.0, utilization),
        saturation_seconds=saturation_seconds,
        success_rate=success_rate,
    )


# ── 压测场景模板（§5 场景覆盖） ──────────────────────────────
SCENARIO_PRESETS: dict[str, CapacityInputs] = {
    # 短问答：低频 LLM/工具，低上下文
    "short_qa": CapacityInputs(
        llm_calls_per_run=2,
        tool_calls_per_run=1,
        input_tokens_per_call=4_000,
        output_tokens_per_call=500,
        llm_p95_latency_ms=2_000,
        tool_p95_latency_ms=1_000,
    ),
    # 高上下文：长输入，缓存命中高
    "high_context": CapacityInputs(
        llm_calls_per_run=4,
        tool_calls_per_run=2,
        input_tokens_per_call=40_000,
        output_tokens_per_call=1_000,
        cached_input_ratio=0.6,
        llm_p95_latency_ms=8_000,
        tool_p95_latency_ms=2_000,
    ),
    # 多工具：工具调用密集
    "multi_tool": CapacityInputs(
        llm_calls_per_run=8,
        tool_calls_per_run=12,
        input_tokens_per_call=10_000,
        output_tokens_per_call=800,
        llm_p95_latency_ms=5_000,
        tool_p95_latency_ms=4_000,
    ),
    # 多 Agent：长链路，高并发 worker 需求
    "multi_agent": CapacityInputs(
        worker_concurrency=8,
        llm_calls_per_run=14,
        tool_calls_per_run=8,
        input_tokens_per_call=8_000,
        output_tokens_per_call=700,
        llm_p95_latency_ms=6_000,
        tool_p95_latency_ms=3_000,
    ),
}
