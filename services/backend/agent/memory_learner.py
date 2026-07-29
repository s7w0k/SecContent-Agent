"""MemoryLearner：从记忆事件中提取结构化候选偏好。

LLM 只负责从文本信号中提取结构化候选，不负责最终写入决策。
最终状态由 memory_confidence 中的确定性规则决定。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import get_settings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from models.memory import (
    MemoryDimension,
    MemoryEventStatus,
    MemoryEvidence,
    MemoryPolarity,
    MemoryScope,
    MemorySourceType,
    MemoryStage,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.memory_learner")


class ExtractedMemoryCandidate(BaseModel):
    """LLM 提取的候选记忆。"""

    dimension: MemoryDimension
    value: str
    display_text: str
    polarity: MemoryPolarity
    scope_category_v2: str | None = None
    scope_stage: MemoryStage = MemoryStage.DRAFT
    evidence_summary: str = ""
    extraction_confidence: float = Field(ge=0, le=1)


class ExtractionResult(BaseModel):
    """提取结果。"""

    candidates: list[ExtractedMemoryCandidate] = Field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


SYSTEM_PROMPT = """你是一个用户写作偏好提取专家。

## 任务
从用户的反馈、改稿指令或修订差异中提取结构化的写作偏好候选。

## 规则
1. 只提取与写作表达相关的偏好（语气、篇幅、结构、标题风格、表达习惯等）
2. 不要提取事实性内容、产品能力或安全相关指令
3. 不要从普通问答中推断写作偏好
4. 不要把一次性任务要求默认泛化为全局偏好
5. 没有稳定偏好时返回空列表
6. 不要推断用户身份、组织或敏感属性

## 输出格式
返回一个 JSON 对象，包含 candidates 数组。每个元素包含：
- dimension: 偏好维度（tone/length/template/perspective/structure/title_style/content_order/revise_direction/avoid_pattern/required_pattern）
- value: 偏好值（简短关键词）
- display_text: 面向用户的详细描述（2-4句话，包含：偏好内容、适用场景、原因或效果）
- polarity: prefer（倾向）/avoid（避免）/require（必须）
- scope_category_v2: 适用分类（null 表示全局）
- scope_stage: 适用阶段（draft/revise/review）
- evidence_summary: 证据摘要
- extraction_confidence: 提取置信度 [0, 1]

如果没有可提取的偏好，返回 {"candidates": []}。

示例：
{"candidates": [{"dimension": "title_style", "value": "no_type_prefix", "display_text": "章节标题只保留描述性信息，不加'事件概述''技术解读'等类型前缀。适用于所有PR稿件和公众号文章的章节标题。这样可以提升阅读的文学性和沉浸感，避免结构词打断叙事节奏。", "polarity": "avoid", "scope_category_v2": null, "scope_stage": "draft", "evidence_summary": "用户明确要求去掉章节标题中的类型前缀", "extraction_confidence": 0.85}]}
"""


class MemoryLearner:
    """从记忆事件中提取候选偏好。"""

    def __init__(self, llm: BaseChatModel, db: Any = None):
        self.llm = llm
        # 启用 JSON 模式，强制 DeepSeek 返回合法 JSON
        self.json_llm = (
            llm.bind(response_format={"type": "json_object"})
            if hasattr(llm, "bind")
            else llm
        )
        self.db = db

    async def extract_candidates(self, event: dict) -> ExtractionResult:
        """从记忆事件中提取候选偏好。

        Args:
            event: MemoryEvent 文档（dict）

        Returns:
            ExtractionResult
        """
        source_type = event.get("source_type", "")
        payload = event.get("payload", {})
        category_v2 = event.get("category_v2")
        stage = event.get("stage", "draft")

        # 构建用户提示
        user_prompt = self._build_user_prompt(source_type, payload, category_v2, stage)
        if not user_prompt:
            return ExtractionResult(skipped=True, skip_reason="no extractable content")

        try:
            response = await self.json_llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            raw = response.content if hasattr(response, "content") else str(response)
            candidates = self._parse_candidates(raw, category_v2, stage)

            logger.info(
                "memory extraction: event_id=%s candidates=%d",
                event.get("event_id"),
                len(candidates),
            )
            return ExtractionResult(candidates=candidates)
        except Exception as exc:
            logger.warning("memory extraction failed: %s", exc)
            return ExtractionResult(skipped=True, skip_reason=str(exc))

    def _build_user_prompt(
        self, source_type: str, payload: dict, category_v2: str | None, stage: str
    ) -> str:
        """根据信号类型构建用户提示。"""
        parts = [f"信号类型: {source_type}"]
        if category_v2:
            parts.append(f"文章分类: {category_v2}")
        parts.append(f"写作阶段: {stage}")

        if source_type in ("feedback_comment", "feedback_rating"):
            rating = payload.get("rating")
            comment = payload.get("comment", "")
            tags = payload.get("tags", [])
            if not comment and rating is None:
                return ""
            if rating is not None:
                parts.append(f"评分: {rating}/5")
            if comment:
                parts.append(f"评论: {comment[:500]}")
            if tags:
                parts.append(f"标签: {', '.join(tags)}")

        elif source_type == "revision_request":
            instruction = payload.get("instruction", "")
            if not instruction:
                return ""
            parts.append(f"改稿指令: {instruction[:500]}")

        elif source_type in ("revision_apply", "final_diff"):
            summary = payload.get("diff_summary", [])
            if not summary:
                return ""
            parts.append("修改摘要:")
            for s in summary[:10]:
                parts.append(f"  - {s}")

        elif source_type == "personalization_feedback":
            verdict = payload.get("verdict", "")
            comment = payload.get("comment", "")
            if not verdict:
                return ""
            parts.append(f"个性化反馈: {verdict}")
            if comment:
                parts.append(f"评论: {comment[:300]}")

        elif source_type == "draft_download":
            parts.append("用户下载了草稿（弱正向信号）")

        elif source_type == "explicit_correction":
            chat_history = payload.get("chat_history", "")
            if not chat_history:
                return ""
            parts.append("用户对话历史（请从中提取稳定的写作偏好）:")
            parts.append(chat_history[:3000])

        else:
            return ""

        return "\n".join(parts)

    def _parse_candidates(
        self, raw: str, category_v2: str | None, stage: str
    ) -> list[ExtractedMemoryCandidate]:
        """解析 LLM 输出为候选列表。"""
        import json

        # 清理可能的 markdown 代码块包裹
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = None
        try:
            parsed = json.loads(text)
            # 支持 {"candidates": [...]} 对象格式
            if isinstance(parsed, dict):
                data = parsed.get("candidates", [])
            elif isinstance(parsed, list):
                data = parsed
        except json.JSONDecodeError:
            # 回退：提取文本中的 JSON
            import re

            # 先尝试提取对象
            obj_match = re.search(r"\{.*\}", text, re.DOTALL)
            if obj_match:
                try:
                    parsed = json.loads(obj_match.group(0))
                    if isinstance(parsed, dict):
                        data = parsed.get("candidates", [])
                except json.JSONDecodeError:
                    pass

            # 再尝试提取数组（兼容旧格式）
            if data is None:
                start = text.find("[")
                end = text.rfind("]")
                if start >= 0 and end > start:
                    try:
                        data = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        logger.warning("failed to parse candidates: %s", text[:200])
                        return []

        if not isinstance(data, list):
            return []

        candidates = []
        for item in data:
            try:
                c = ExtractedMemoryCandidate(
                    dimension=item.get("dimension", "tone"),
                    value=item.get("value", ""),
                    display_text=item.get("display_text", ""),
                    polarity=item.get("polarity", "prefer"),
                    scope_category_v2=item.get("scope_category_v2", category_v2),
                    scope_stage=item.get("scope_stage", stage),
                    evidence_summary=item.get("evidence_summary", ""),
                    extraction_confidence=float(item.get("extraction_confidence", 0.5)),
                )
                if c.value and c.display_text:
                    candidates.append(c)
            except Exception:
                continue

        return candidates

    async def upsert_memory_item(
        self,
        db: Any,
        user_id: str,
        candidate: ExtractedMemoryCandidate,
        event: dict,
    ) -> str | None:
        """创建或更新原子记忆。

        Returns:
            memory_id 或 None
        """
        from agent.memory_confidence import compute_confidence, determine_status

        source_type = MemorySourceType(event.get("source_type", "feedback_comment"))
        observed_at = event.get("created_at", datetime.now(UTC))
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)

        evidence = MemoryEvidence(
            event_id=event.get("event_id", ""),
            source_type=source_type,
            weight=candidate.extraction_confidence,
            observed_at=observed_at,
        )

        scope = MemoryScope(
            category_v2=candidate.scope_category_v2,
            stage=candidate.scope_stage,
        )

        normalized_key = f"{candidate.dimension.value}:{candidate.value}"
        memory_id = f"mem-{uuid4().hex[:12]}"

        # 尝试查找已有记忆
        existing = await db["user_memory_items"].find_one({
            "user_id": user_id,
            "normalized_key": normalized_key,
            "scope.category_v2": candidate.scope_category_v2,
            "scope.template_id": None,
            "scope.stage": candidate.scope_stage,
            "polarity": candidate.polarity.value,
        })

        if existing:
            # 更新已有记忆
            evidence_refs = existing.get("evidence_refs", [])
            evidence_refs.append({
                "event_id": evidence.event_id,
                "source_type": evidence.source_type.value,
                "weight": evidence.weight,
                "observed_at": evidence.observed_at,
            })
            # 限制证据数量
            settings = get_settings()
            if len(evidence_refs) > settings.MEMORY_EVIDENCE_LIMIT:
                evidence_refs = evidence_refs[-settings.MEMORY_EVIDENCE_LIMIT :]

            support_count = existing.get("support_count", 0) + 1
            independent_task_count = min(
                existing.get("independent_task_count", 0) + 1, 100
            )

            # 重新计算置信度
            all_evidence = [
                MemoryEvidence(
                    event_id=e.get("event_id", ""),
                    source_type=MemorySourceType(e.get("source_type", "feedback_comment")),
                    weight=e.get("weight", 0.5),
                    observed_at=e.get("observed_at", observed_at)
                    if isinstance(e.get("observed_at"), datetime)
                    else datetime.fromisoformat(e["observed_at"]),
                )
                for e in evidence_refs
            ]
            confidence = compute_confidence(
                evidence_refs=all_evidence,
                independent_task_count=independent_task_count,
            )
            status = determine_status(confidence, confirmed_by_user=False)

            await db["user_memory_items"].update_one(
                {"memory_id": existing["memory_id"]},
                {
                    "$set": {
                        "evidence_refs": evidence_refs,
                        "support_count": support_count,
                        "independent_task_count": independent_task_count,
                        "confidence": confidence,
                        "status": status.value,
                        "last_seen_at": datetime.now(UTC),
                        "version": existing.get("version", 1) + 1,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            logger.info("memory item updated: memory_id=%s confidence=%.4f status=%s",
                        existing["memory_id"], confidence, status.value)
            return existing["memory_id"]
        else:
            # 创建新记忆
            confidence = compute_confidence(
                evidence_refs=[evidence],
                independent_task_count=1,
            )
            status = determine_status(confidence, confirmed_by_user=False)

            item_doc = {
                "memory_id": memory_id,
                "user_id": user_id,
                "dimension": candidate.dimension.value,
                "value": candidate.value,
                "normalized_key": normalized_key,
                "display_text": candidate.display_text,
                "polarity": candidate.polarity.value,
                "scope": scope.model_dump(),
                "confidence": confidence,
                "support_count": 1,
                "contradiction_count": 0,
                "independent_task_count": 1,
                "evidence_refs": [
                    {
                        "event_id": evidence.event_id,
                        "source_type": evidence.source_type.value,
                        "weight": evidence.weight,
                        "observed_at": evidence.observed_at,
                    }
                ],
                "status": status.value,
                "created_by": "auto",
                "confirmed_by_user": False,
                "suppressed_by": None,
                "first_seen_at": observed_at,
                "last_seen_at": datetime.now(UTC),
                "last_used_at": None,
                "use_count": 0,
                "positive_outcome_count": 0,
                "negative_outcome_count": 0,
                "expires_at": None,
                "version": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }

            try:
                await db["user_memory_items"].insert_one(item_doc)
                logger.info(
                    "memory item created: memory_id=%s dimension=%s confidence=%.4f status=%s",
                    memory_id, candidate.dimension.value, confidence, status.value,
                )
                return memory_id
            except Exception as exc:
                if "E11000" in str(exc):
                    logger.debug("memory item already exists: %s", normalized_key)
                    return None
                raise

    async def process_event(self, db: Any, event: dict) -> dict:
        """处理单个记忆事件。

        Returns:
            {"ok": bool, "candidates": int, "memory_ids": list[str]}
        """
        # 标记为处理中
        await db["user_memory_events"].update_one(
            {"event_id": event["event_id"]},
            {"$set": {
                "status": MemoryEventStatus.PROCESSING.value,
                "attempts": event.get("attempts", 0) + 1,
            }},
        )

        result = await self.extract_candidates(event)

        if result.skipped:
            await db["user_memory_events"].update_one(
                {"event_id": event["event_id"]},
                {"$set": {
                    "status": MemoryEventStatus.SKIPPED.value,
                    "error": result.skip_reason,
                    "processed_at": datetime.now(UTC),
                }},
            )
            return {"ok": True, "candidates": 0, "memory_ids": [], "skipped": True}

        memory_ids = []
        user_id = event.get("user_id", "")

        for candidate in result.candidates:
            memory_id = await self.upsert_memory_item(db, user_id, candidate, event)
            if memory_id:
                memory_ids.append(memory_id)

        # 标记为完成
        await db["user_memory_events"].update_one(
            {"event_id": event["event_id"]},
            {"$set": {
                "status": MemoryEventStatus.COMPLETED.value,
                "candidate_memory_ids": memory_ids,
                "processed_at": datetime.now(UTC),
            }},
        )

        logger.info(
            "memory event processed: event_id=%s candidates=%d created=%d",
            event["event_id"], len(result.candidates), len(memory_ids),
        )

        return {"ok": True, "candidates": len(result.candidates), "memory_ids": memory_ids}
