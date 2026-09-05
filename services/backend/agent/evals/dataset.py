"""数据集治理 -- 阶段2 §2（WBS 2.2）。

实现版本化数据集加载、schema 校验与无泄漏 group split：
  - train / validation / holdout / safety_holdout 四切分；
  - group split：按业务对象（case 的 payload_hash，即输入+租户 fixture）分组，
    同一输入快照的近似样本不会同时进入 train 与 holdout；
  - safety_holdout 独立维护，不参与自动期望答案生成（只标记，不生成）；
  - 数据集文件命名约定：<name>_v<N>.jsonl，版本号随内容变更递增。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agent.evals.contracts import EvalCase

VALID_SPLITS = ("train", "validation", "holdout", "safety_holdout")

# 阶段2 §2.1 数据集至少覆盖的场景类别
REQUIRED_CATEGORIES = {
    "no_tool",  # 无工具直接回答
    "product_knowledge",  # 产品知识检索
    "article",  # 文章/草稿/记忆检索
    "multi_turn",  # 多轮上下文和长历史
    "multi_tool",  # 多工具与并行工具
    "insufficient_evidence",  # 不充分证据和冲突证据
    "budget_limits",  # 超预算、超时、429、5xx
    "permissions",  # 工具权限、跨用户、跨租户
    "security",  # 提示注入与恶意工具文本
    "finalization",  # 审批、取消、重启与补偿
    "a2a",  # A2A 重复投递、乱序、断流和恶意 peer
}

DATASET_VERSION_RE = re.compile(r"^(?P<name>.+)_v(?P<version>\d+)\.jsonl$")


class DatasetError(ValueError):
    """数据集加载 / 校验错误。"""


def parse_dataset_version(filename: str) -> tuple[str, int]:
    """从文件名解析数据集名称与版本（如 real_v1.jsonl -> ("real", 1)）。"""
    m = DATASET_VERSION_RE.match(filename)
    if not m:
        raise DatasetError(f"数据集文件名必须形如 <name>_v<N>.jsonl，得到: {filename}")
    return m.group("name"), int(m.group("version"))


def load_dataset(path: Path) -> list[EvalCase]:
    """加载版本化 JSONL 数据集并做基础校验。"""
    if not path.exists():
        raise DatasetError(f"数据集不存在: {path}")
    name, version = parse_dataset_version(path.name)
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path.name}:{lineno} JSON 解析失败: {exc}") from exc
            case = EvalCase.from_dict(raw)
            if case.case_id in seen_ids:
                raise DatasetError(f"{path.name} 存在重复 case_id: {case.case_id}")
            seen_ids.add(case.case_id)
            if not case.dataset_version:
                case = EvalCase(
                    **{
                        **raw,
                        "dataset_version": f"{name}_v{version}",
                    }
                )
            if case.split not in VALID_SPLITS:
                raise DatasetError(
                    f"{path.name}:{lineno} 非法 split={case.split!r}，"
                    f"合法值: {', '.join(VALID_SPLITS)}"
                )
            cases.append(case)
    if not cases:
        raise DatasetError(f"数据集为空: {path}")
    return cases


def coverage_report(cases: list[EvalCase]) -> dict[str, Any]:
    """数据集类别覆盖报告（阶段2 §2.1 至少覆盖清单）。"""
    by_category: dict[str, int] = {}
    by_split: dict[str, int] = {}
    for case in cases:
        by_category[case.category] = by_category.get(case.category, 0) + 1
        by_split[case.split] = by_split.get(case.split, 0) + 1
    missing = sorted(REQUIRED_CATEGORIES - set(by_category))
    return {
        "total": len(cases),
        "by_category": by_category,
        "by_split": by_split,
        "missing_categories": missing,
        "complete": not missing,
    }


def group_split(
    cases: list[EvalCase],
    *,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    seed: int = 42,
) -> dict[str, list[EvalCase]]:
    """无泄漏 group split（阶段2 §2.2）。

    按业务对象分组：以 case.payload_hash() 为组键（同一输入+租户 fixture 属于同组），
    同组样本只会进入同一个切分，杜绝近似样本同时出现在 train/holdout。
    已在数据集中显式标记 split 的 case 优先遵循显式标记。

    Args:
        cases: 数据集用例
        train_ratio / val_ratio: train/validation 比例（剩余为 holdout）
        seed: 随机种子（可重复）

    Returns:
        {"train": [...], "validation": [...], "holdout": [...], "safety_holdout": [...]}
    """
    import random

    rng = random.Random(seed)

    explicit: dict[str, list[EvalCase]] = {s: [] for s in VALID_SPLITS}
    unassigned: list[EvalCase] = []
    for case in cases:
        if case.split in VALID_SPLITS:
            explicit[case.split].append(case)
        else:
            unassigned.append(case)

    # 按业务对象分组（payload_hash），组内样本同进退
    groups: dict[str, list[EvalCase]] = {}
    for case in unassigned:
        groups.setdefault(case.payload_hash(), []).append(case)
    group_list = list(groups.values())
    rng.shuffle(group_list)

    result = {s: list(explicit[s]) for s in VALID_SPLITS}
    for group in group_list:
        n = len(group)
        assigned = False
        # 依次填充 train -> validation -> holdout（safety_holdout 由数据显式标记）
        for split_name, _ratio in (
            ("train", train_ratio),
            ("validation", val_ratio),
        ):
            current_total = sum(len(result[s]) for s in ("train", "validation", "holdout"))
            target = _ratio * len(cases)
            if current_total + n <= target + 0.5 or not result[split_name]:
                result[split_name].extend(group)
                assigned = True
                break
        if not assigned:
            result["holdout"].extend(group)
    return result


def holdout_identity(cases: list[EvalCase]) -> list[dict[str, Any]]:
    """holdout / safety_holdout 用例清单（用于发布候选验证，禁止预生成答案）。"""
    out: list[dict[str, Any]] = []
    for case in cases:
        if case.split in ("holdout", "safety_holdout"):
            out.append(
                {
                    "case_id": case.case_id,
                    "dataset_version": case.dataset_version,
                    "category": case.category,
                    "split": case.split,
                    "payload_hash": case.payload_hash(),
                    "expected_outcome": case.expected_outcome,
                }
            )
    return out


def dataset_fingerprint(cases: list[EvalCase]) -> str:
    """数据集指纹：全部 case 的 payload hash 聚合（用于 manifest 冻结）。"""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda c: c.case_id):
        digest.update(case.payload_hash().encode("utf-8"))
    return digest.hexdigest()[:16]
