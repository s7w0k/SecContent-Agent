"""基于反馈、操作和改稿指令构建用户风格画像。"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from agent.template_compat import template_reference
from langchain_core.messages import HumanMessage, SystemMessage
from models.feedback import (
    ActionType,
    PreferredLength,
    PreferredTone,
    StyleProfile,
)

logger = logging.getLogger("backend.agent.style_profiler")

MIN_INSTRUCTIONS_FOR_LLM = 3
MAX_LLM_ITEMS = 100

_PATTERN_SYSTEM_PROMPT = """你是 PR 编辑偏好分析助手。
根据用户的历史改稿指令归纳稳定偏好，只输出 JSON，不要补充解释。
字段必须为：
{
  "common_revise_directions": ["3-5 个简短方向"],
  "avoid_patterns": ["用户倾向避免的模式"],
  "preferred_tone": "market_oriented | technical | executive",
  "preferred_length": "short | medium | long",
  "analysis": "一句话总结"
}
不要推断指令中没有体现的事实。"""

_FEEDBACK_SYSTEM_PROMPT = """你是 PR 反馈标签提取助手。
根据评分、评论和已有标签提取最多 5 个简短中文标签，只输出 JSON：
{"tags": ["标签1", "标签2"]}。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content
        )
    return str(content)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM response does not contain a JSON object")
    result = json.loads(match.group())
    if not isinstance(result, dict):
        raise ValueError("LLM response must be a JSON object")
    return result


def _string_list(value: Any, limit: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:limit]


class StyleProfiler:
    """聚合历史信号并生成可持久化的用户风格画像。"""

    def __init__(self, llm: Any, db: Any):
        self.llm = llm
        self.db = db

    async def _find_all(self, collection: str, query: dict) -> list[dict]:
        cursor = self.db[collection].find(query)
        return await cursor.to_list(length=None)

    async def aggregate_feedbacks(self, user_id: str) -> dict:
        """聚合有效反馈，并补齐反馈对应草稿的模板和视角。"""
        feedbacks = await self._find_all(
            "feedbacks",
            {"user_id": user_id, "status": "active"},
        )
        draft_cache: dict[str, dict | None] = {}
        enriched: list[dict] = []
        for feedback in feedbacks:
            item = dict(feedback)
            target = item.get("target_ref", {})
            article_hash = target.get("article_url_hash")
            draft_index = target.get("draft_index")
            has_template_reference = bool(item.get("template_id") or item.get("template_name"))
            if not has_template_reference and article_hash and isinstance(draft_index, int):
                if article_hash not in draft_cache:
                    draft_cache[article_hash] = await self.db["user_drafts"].find_one(
                        {"user_id": user_id, "article_url_hash": article_hash},
                    )
                user_draft = draft_cache[article_hash]
                drafts = user_draft.get("drafts", []) if user_draft else []
                if 0 <= draft_index < len(drafts):
                    draft = drafts[draft_index]
                    item.update(template_reference(draft))
                    item["template"] = draft.get("template")
                    item["perspective"] = draft.get("perspective")
            elif item.get("template_name"):
                item.setdefault("template", item["template_name"])
            enriched.append(item)

        ratings = [item["rating"] for item in enriched if isinstance(item.get("rating"), int)]
        tags = Counter(tag for item in enriched for tag in item.get("tags", []))
        return {
            "items": enriched,
            "summary": {
                "total_feedbacks": len(enriched),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
                "positive_count": sum(rating >= 4 for rating in ratings),
                "negative_count": sum(rating <= 2 for rating in ratings),
                "neutral_count": sum(rating == 3 for rating in ratings),
                "top_tags": [tag for tag, _ in tags.most_common(5)],
            },
        }

    async def aggregate_activities(self, user_id: str) -> dict:
        """聚合用户关键操作及最近活跃时间。"""
        activities = await self._find_all("user_activities", {"user_id": user_id})
        actions = Counter(str(item.get("action", "")) for item in activities)
        timestamps = [
            item["created_at"]
            for item in activities
            if isinstance(item.get("created_at"), datetime)
        ]
        return {
            "items": activities,
            "summary": {
                "total_downloads": actions[ActionType.DRAFT_DOWNLOAD],
                "total_applies": actions[ActionType.REVISION_APPLY],
                "total_revises": actions[ActionType.DRAFT_REVISE],
                "total_feedbacks": actions[ActionType.FEEDBACK_SUBMIT],
                "last_active_at": max(timestamps) if timestamps else None,
            },
        }

    async def aggregate_revise_instructions(
        self,
        user_id: str,
    ) -> list[str]:
        """从指定用户的对话会话中提取并去重改稿指令。"""
        query = {"user_id": user_id}
        sessions = await self._find_all("chat_sessions", query)
        instructions = [
            str(message.get("content", "")).strip()
            for session in sessions
            for message in session.get("messages", [])
            if message.get("role") == "user" and str(message.get("content", "")).strip()
        ]
        return list(dict.fromkeys(instructions))

    @staticmethod
    def calculate_preference_score(
        feedback_count: int,
        avg_rating: float,
        download_count: int,
        apply_count: int,
        revise_count: int,
    ) -> float:
        """按阶段六定义的信号权重计算单项偏好分。"""
        score = 0.0
        if feedback_count:
            if avg_rating >= 4:
                score += avg_rating * 2
            elif avg_rating <= 2:
                score -= (3 - avg_rating) * 3
            else:
                score += avg_rating
        score += download_count * 3
        score += apply_count * 5
        score += revise_count
        if feedback_count and avg_rating <= 2 and download_count > 0:
            score = max(score, 0)
        return round(score, 1)

    def calculate_preference_scores(
        self,
        feedbacks: list[dict],
        activities: list[dict],
        template_catalog: dict[str, dict[str, str]] | None = None,
    ) -> dict:
        """分别计算模板和视角的反馈及行为指标。"""
        return {
            "template_scores": self._calculate_template_dimension(
                feedbacks,
                activities,
                template_catalog or {},
            ),
            "perspective_scores": self._calculate_dimension(
                feedbacks,
                activities,
                "perspective",
            ),
        }

    def _calculate_template_dimension(
        self,
        feedbacks: list[dict],
        activities: list[dict],
        template_catalog: dict[str, dict[str, str]],
    ) -> dict[str, dict]:
        """Group modern signals by immutable template ID and legacy signals by name."""

        def values(
            item: dict,
            *,
            activity: bool = False,
        ) -> tuple[str, str, str | None, str | None]:
            source = item.get("target", {}) if activity else item
            name = str(source.get("template_name") or source.get("template") or "").strip()
            raw_template_id = str(source.get("template_id") or "").strip()
            is_modern = raw_template_id and not raw_template_id.startswith("legacy:")
            template_id = raw_template_id if is_modern else ""
            template_key = str(source.get("template_key") or "").strip() or None
            return template_id or name, name, template_key, template_id or None

        identities = {
            identity
            for item in feedbacks
            if (identity := values(item)[0])
        } | {
            identity
            for item in activities
            if (identity := values(item, activity=True)[0])
        }
        result: dict[str, dict] = {}
        for identity in identities:
            matching_feedbacks = [item for item in feedbacks if values(item)[0] == identity]
            matching_activities = [
                item for item in activities if values(item, activity=True)[0] == identity
            ]
            signals = [(item, False) for item in matching_feedbacks] + [
                (item, True) for item in matching_activities
            ]
            ratings = [
                item["rating"]
                for item in matching_feedbacks
                if isinstance(item.get("rating"), int)
            ]
            actions = Counter(str(item.get("action", "")) for item in matching_activities)
            observed_names = list(
                dict.fromkeys(
                    name
                    for item, activity in signals
                    if (name := values(item, activity=activity)[1])
                )
            )
            catalog_item = template_catalog.get(identity, {})
            display_name = catalog_item.get("name") or (
                observed_names[-1] if observed_names else identity
            )
            template_key = catalog_item.get("template_key") or next(
                (
                    key
                    for item, activity in signals
                    if (key := values(item, activity=activity)[2])
                ),
                None,
            )
            modern = any(values(item)[3] for item in matching_feedbacks) or any(
                values(item, activity=True)[3] for item in matching_activities
            )
            result[identity] = {
                "count": len(ratings),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
                "download_count": actions[ActionType.DRAFT_DOWNLOAD],
                "apply_count": actions[ActionType.REVISION_APPLY],
                "revise_count": actions[ActionType.DRAFT_REVISE],
                "template_id": identity if modern else None,
                "template_key": template_key,
                "display_name": display_name,
                "historical_names": [name for name in observed_names if name != display_name],
                "legacy": not modern,
            }
        return dict(sorted(result.items()))

    def _calculate_dimension(
        self,
        feedbacks: list[dict],
        activities: list[dict],
        field: str,
    ) -> dict[str, dict]:
        names = {str(item.get(field)) for item in feedbacks if item.get(field)} | {
            str(item.get("target", {}).get(field))
            for item in activities
            if item.get("target", {}).get(field)
        }
        result: dict[str, dict] = {}
        for name in names:
            ratings = [
                item["rating"]
                for item in feedbacks
                if item.get(field) == name and isinstance(item.get("rating"), int)
            ]
            matching_actions = Counter(
                str(item.get("action", ""))
                for item in activities
                if item.get("target", {}).get(field) == name
            )
            result[name] = {
                "count": len(ratings),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
                "download_count": matching_actions[ActionType.DRAFT_DOWNLOAD],
                "apply_count": matching_actions[ActionType.REVISION_APPLY],
                "revise_count": matching_actions[ActionType.DRAFT_REVISE],
            }
        return dict(sorted(result.items()))

    def _rank_preferences(self, metrics: dict[str, dict]) -> list[str]:
        ranked = [
            (
                name,
                self.calculate_preference_score(
                    metric["count"],
                    metric["avg_rating"],
                    metric["download_count"],
                    metric["apply_count"],
                    metric["revise_count"],
                ),
            )
            for name, metric in metrics.items()
        ]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return [
            str(metrics[name].get("display_name") or name)
            for name, score in ranked
            if score > 0
        ][:2]

    async def _current_template_catalog(self, user_id: str) -> dict[str, dict[str, str]]:
        """Resolve current tenant template names without making profile rebuild dependent on it."""
        try:
            from agent.template_repository import TemplateRepository

            templates = await TemplateRepository(self.db).list_effective_templates(user_id)
            return {
                template.template_id: {
                    "name": template.name,
                    "template_key": str(template.template_key),
                }
                for template in templates
            }
        except Exception as exc:
            logger.warning(
                "Failed to resolve current template catalog: user_id=%s: %s",
                user_id,
                exc,
            )
            return {}

    async def extract_revise_patterns(self, instructions: list[str]) -> dict:
        """使用 LLM 提取改稿偏好；样本不足或调用失败时返回默认值。"""
        fallback = {
            "common_revise_directions": [],
            "avoid_patterns": [],
            "preferred_tone": PreferredTone.MARKET_ORIENTED,
            "preferred_length": PreferredLength.MEDIUM,
            "analysis": "",
        }
        if len(instructions) < MIN_INSTRUCTIONS_FOR_LLM or self.llm is None:
            return fallback

        numbered = "\n".join(
            f"{index}. {instruction}"
            for index, instruction in enumerate(instructions[:MAX_LLM_ITEMS], start=1)
        )
        try:
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=_PATTERN_SYSTEM_PROMPT),
                    HumanMessage(content=f"改稿指令列表：\n{numbered}"),
                ],
            )
            parsed = _parse_json_object(_response_text(response))
            tone = str(parsed.get("preferred_tone", ""))
            length = str(parsed.get("preferred_length", ""))
            return {
                "common_revise_directions": _string_list(
                    parsed.get("common_revise_directions"),
                ),
                "avoid_patterns": _string_list(parsed.get("avoid_patterns")),
                "preferred_tone": (
                    tone
                    if tone in {item.value for item in PreferredTone}
                    else fallback["preferred_tone"]
                ),
                "preferred_length": (
                    length
                    if length in {item.value for item in PreferredLength}
                    else fallback["preferred_length"]
                ),
                "analysis": str(parsed.get("analysis", "")).strip()[:5000],
            }
        except Exception as exc:
            logger.warning("Failed to extract revise patterns, using rules only: %s", exc)
            return fallback

    async def analyze_feedback(self, feedback: dict) -> list[str]:
        """实时提取单条反馈标签，失败时保留已有标签并做轻量规则补充。"""
        existing = _string_list(feedback.get("tags", []))
        try:
            if self.llm is None:
                raise RuntimeError("LLM is unavailable")
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=_FEEDBACK_SYSTEM_PROMPT),
                    HumanMessage(
                        content=json.dumps(
                            {
                                "rating": feedback.get("rating"),
                                "comment": feedback.get("comment", ""),
                                "existing_tags": existing,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )
            return _string_list(_parse_json_object(_response_text(response)).get("tags"))
        except Exception as exc:
            logger.warning("Failed to analyze feedback, using fallback tags: %s", exc)
            fallback = list(existing)
            rating = feedback.get("rating")
            if isinstance(rating, int):
                fallback.append(
                    "正向反馈" if rating >= 4 else "负向反馈" if rating <= 2 else "中性反馈"
                )
            return list(dict.fromkeys(fallback))[:5]

    async def build_profile(self, user_id: str) -> dict:
        """构建完整画像并幂等写入 user_profiles 集合。"""
        feedback_data = await self.aggregate_feedbacks(user_id)
        activity_data = await self.aggregate_activities(user_id)
        instructions = await self.aggregate_revise_instructions(user_id)
        template_catalog = await self._current_template_catalog(user_id)
        preference_scores = self.calculate_preference_scores(
            feedback_data["items"],
            activity_data["items"],
            template_catalog,
        )
        patterns = await self.extract_revise_patterns(instructions)
        pattern_counts = [
            {
                "pattern": pattern,
                "count": max(
                    1,
                    sum(pattern in instruction for instruction in instructions),
                ),
            }
            for pattern in patterns["common_revise_directions"]
        ]

        existing = await self.db["user_profiles"].find_one({"user_id": user_id})
        now = _now()
        profile = StyleProfile(
            user_id=user_id,
            style_hints={
                "preferred_templates": self._rank_preferences(
                    preference_scores["template_scores"],
                ),
                "preferred_perspectives": self._rank_preferences(
                    preference_scores["perspective_scores"],
                ),
                "preferred_length": patterns["preferred_length"],
                "preferred_tone": patterns["preferred_tone"],
                "common_revise_directions": patterns["common_revise_directions"],
                "avoid_patterns": patterns["avoid_patterns"],
            },
            preference_scores=preference_scores,
            feedback_summary=feedback_data["summary"],
            activity_summary=activity_data["summary"],
            revise_instruction_patterns=pattern_counts,
            llm_analysis=patterns["analysis"],
            version=int(existing.get("version", 0)) + 1 if existing else 1,
            created_at=existing.get("created_at", now) if existing else now,
            updated_at=now,
        )
        document = profile.model_dump(exclude={"id"}, mode="python")
        await self.db["user_profiles"].replace_one(
            {"user_id": user_id},
            document,
            upsert=True,
        )
        return profile.model_dump(mode="json", by_alias=False)

    def get_style_hints(self, profile: dict | StyleProfile) -> str:
        """将结构化画像转换为可直接注入生成 Prompt 的文本。"""
        data = profile.model_dump(mode="json") if isinstance(profile, StyleProfile) else profile
        hints = data.get("style_hints", {})

        def display(value: Any) -> str:
            return "、".join(value) if isinstance(value, list) and value else "暂无明确偏好"

        return "\n".join(
            [
                "## 用户风格偏好（基于历史反馈学习）",
                f"- 偏好模板：{display(hints.get('preferred_templates'))}",
                f"- 偏好视角：{display(hints.get('preferred_perspectives'))}",
                f"- 偏好语气：{hints.get('preferred_tone', PreferredTone.MARKET_ORIENTED)}",
                f"- 偏好篇幅：{hints.get('preferred_length', PreferredLength.MEDIUM)}",
                f"- 常见改稿方向：{display(hints.get('common_revise_directions'))}",
                f"- 避免模式：{display(hints.get('avoid_patterns'))}",
                "",
                "请在生成时参考以上偏好，但不要牺牲事实准确性。",
            ],
        )


async def load_style_hints(db: Any, user_id: str) -> str | None:
    """读取指定用户画像并转换成可注入 Prompt 的风格提示。"""
    try:
        profile = await db["user_profiles"].find_one({"user_id": user_id})
        if not profile:
            logger.info("Style hints not available: user_id=%s", user_id)
            return None
        hints = StyleProfiler(llm=None, db=db).get_style_hints(profile)
        logger.info(
            "Style hints injected: user_id=%s, profile_version=%s",
            user_id,
            profile.get("version", 1),
        )
        return hints
    except Exception as exc:
        logger.warning("Failed to load style hints for user_id=%s: %s", user_id, exc)
        return None
