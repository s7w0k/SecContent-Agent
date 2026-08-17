"""ContextBridge — 阶段二 Step 5：在 scorer/draft/chat 与 ContextManager 之间搭桥。

职责：
  1. 依据 KNOWLEDGE_SKILLS_* 开关决策 off / shadow / active 三种模式
  2. 按 purpose 收集 ContextSource（skill_core + required_product + 可选 skill_references）
  3. 调用 ContextManager.build 生成 ContextPlan（token 预算、冲突抑制）
  4. 生成 LLM 日志 telemetry：context_plan_hash / skill_versions / knowledge_snapshot / source_ids

模式语义：
  - off    ：完全走旧知识路径（不构建 ContextPlan）
  - shadow ：后台构建 ContextPlan 并记录差异，LLM 仍注入旧上下文
  - active ：注入 plan.rendered()（Skill 指令 + 产品知识），不再并行注入旧知识块

灰度：active 模式下按 KNOWLEDGE_SKILLS_ROLLOUT_PERCENT 对 user_id 确定性分流，
  未命中灰度的用户回退旧路径（默认 ROLLOUT_PERCENT=0，即 active 不生效）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal

from agent.context_cache import ContextCacheKey, _sha256
from agent.context_manager import (
    ALLOCATION_ORDER,
    ContextManager,
    ContextRequest,
    ContextSource,
)
from agent.knowledge_slice import KnowledgeSliceResolver

logger = logging.getLogger("backend.agent.context_bridge")

Mode = Literal["off", "shadow", "active"]

# draft 用途的 Skill 可选 references（ALLOCATION_ORDER 中属于 skill_references）
DRAFT_OPTIONAL_REFERENCES: tuple[tuple[str, str], ...] = (
    ("draft-writing", "references/writing-guidelines.md"),
)

# score/draft 必需 Skill 名称（与 skill_registry.PURPOSE_SKILLS 对齐，缺省由 registry 决定）
_PURPOSE_SKILL_SOURCE = {
    "score": (("scoring-knowledge", None),),
    "draft": (("draft-writing", None), ("compliance-review", None)),
    "chat": (),
}

# 阶段3 S3-4：明确的知识上下文 fallback 原因（禁止跨产品静默回退）
FALLBACK_INDEX_UNAVAILABLE = "index_unavailable"
FALLBACK_CONTEXT_BUILD_FAILED = "context_build_failed"
FALLBACK_LEGACY_TASK_MISSING_SNAPSHOT = "legacy_task_missing_snapshot"
FALLBACK_REQUIRED_KNOWLEDGE_MISSING = "required_knowledge_missing"
FALLBACK_PRODUCT_UNRESOLVED = "product_unresolved"

# 禁止回退到全局产品知识的模式/场景（阶段3 S3-4）
_FALLBACK_FORBIDDEN_PRODUCT_REASONS = frozenset(
    {
        FALLBACK_PRODUCT_UNRESOLVED,
        FALLBACK_REQUIRED_KNOWLEDGE_MISSING,
    }
)


def allow_global_product_fallback(reason: str | None) -> bool:
    """判断 given fallback 原因是否允许回退到全局产品知识。

    mode=none、selected 产品知识缺失、auto 产品未解析、用户知识越权或未发布时，
    禁止回退全局产品知识（返回 False）。
    """
    if not reason:
        return True
    return reason not in _FALLBACK_FORBIDDEN_PRODUCT_REASONS


def context_mode(settings: Any) -> Mode:
    """根据配置开关返回全局模式。"""
    if not settings.KNOWLEDGE_SKILLS_ENABLED:
        return "off"
    if settings.KNOWLEDGE_SKILLS_SHADOW_ENABLED:
        return "shadow"
    return "active"


def user_in_rollout(user_id: str, percent: int) -> bool:
    """按 user_id 确定性分流（灰度用）。percent<=0 恒 False，>=100 恒 True。"""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent


def _sort_key(section_type: str) -> int:
    """按 ALLOCATION_ORDER 排序；未知类型排最后。"""
    if section_type in ALLOCATION_ORDER:
        return ALLOCATION_ORDER.index(section_type)
    return len(ALLOCATION_ORDER)


class ContextBridge:
    """将 SkillRegistry + KnowledgeSlice + ContextManager 组合成统一的上下文服务。"""

    def __init__(
        self,
        *,
        db: Any = None,
        settings: Any = None,
        registry: Any = None,
        knowledge_base_dir: str | None = None,
        cache: Any = None,
    ):
        from config import get_settings

        self._db = db
        self._settings = settings if settings is not None else get_settings()
        self._registry = registry
        self._knowledge_base_dir = knowledge_base_dir or self._settings.KNOWLEDGE_BASE_DIR
        self._cache = cache

    # ── 模式决策 ──────────────────────────────────────────

    def mode(self) -> Mode:
        return context_mode(self._settings)

    def effective_mode(self, user_id: str = "") -> Mode:
        """结合灰度后的实际模式：active 且未命中灰度 → off。"""
        mode = self.mode()
        if mode == "active" and not user_in_rollout(
            user_id, int(self._settings.KNOWLEDGE_SKILLS_ROLLOUT_PERCENT)
        ):
            return "off"
        return mode

    def rollout_on(self, user_id: str) -> bool:
        return user_in_rollout(user_id, int(self._settings.KNOWLEDGE_SKILLS_ROLLOUT_PERCENT))

    # ── 上下文构建 ────────────────────────────────────────

    async def build_plan(
        self,
        *,
        purpose: str,
        user_id: str = "",
        products: list[str] | None = None,
        query: str = "",
        model_id: str = "deepseek-chat",
        system_tokens: int = 0,
        max_input_tokens: int | None = None,
        task_id: str = "",
        trace_id: str = "",
        index_version: str = "",
    ):
        """收集来源并构建 ContextPlan；无任何来源或构建失败时返回 None。

        返回对象含 .plan / .telemetry / .sources。
        传入 cache（ContextCache）时启用版本化缓存 + single-flight。
        """
        effective_max = (
            max_input_tokens
            if max_input_tokens is not None
            else int(self._settings.CONTEXT_MAX_INPUT_TOKENS)
        )
        # 廉价版本分量（用于缓存键，避免为命中缓存而做全量构建）
        skill_hash = self._skill_snapshot_hash()
        knowledge_snapshot = await self._knowledge_fingerprint(purpose, user_id, products)
        memory_version = self._memory_version()

        key = ContextCacheKey(
            user_id=user_id,
            purpose=purpose,
            product_ids=tuple(products or []),
            query_hash=_sha256(query or ""),
            model_id=model_id,
            token_budget=effective_max,
            skill_snapshot_hash=skill_hash,
            knowledge_snapshot=knowledge_snapshot,
            memory_version=memory_version,
        )

        async def _builder():
            return await self._build_plan_inner(
                purpose=purpose,
                user_id=user_id,
                products=products,
                query=query,
                model_id=model_id,
                system_tokens=system_tokens,
                max_input_tokens=effective_max,
                knowledge_snapshot=knowledge_snapshot,
                memory_version=memory_version,
                task_id=task_id,
                trace_id=trace_id,
                index_version=index_version,
            )

        if self._cache is not None:
            plan, status = await self._cache.get_or_build(key, _builder)
            self._cache.record(key.key_hash, status, self.effective_mode(user_id))
            if plan is None:
                return None
            telemetry = self._telemetry(plan, purpose=purpose, task_id=task_id, trace_id=trace_id)
            telemetry["cache"] = status
            return _PlanResult(plan=plan, telemetry=telemetry)

        plan = await _builder()
        if plan is None:
            return None
        telemetry = self._telemetry(plan, purpose=purpose, task_id=task_id, trace_id=trace_id)
        telemetry["cache"] = "off"
        return _PlanResult(plan=plan, telemetry=telemetry)

    async def _build_plan_inner(
        self,
        *,
        purpose: str,
        user_id: str,
        products: list[str] | None,
        query: str,
        model_id: str,
        system_tokens: int,
        max_input_tokens: int,
        knowledge_snapshot: str,
        memory_version: str,
        task_id: str = "",
        trace_id: str = "",
        index_version: str = "",
    ):
        """实际构建（昂贵部分：收集来源 + token 预算分配）。"""
        sources = await self._collect_sources(
            purpose=purpose,
            user_id=user_id,
            products=products,
            query=query,
        )
        if not sources:
            return None

        sources.sort(key=lambda s: (0 if s.required else 1, _sort_key(s.section_type)))

        request = ContextRequest(
            purpose=purpose,  # type: ignore[arg-type]
            user_id=user_id,
            products=list(products or []),
            query=query,
            model_id=model_id,
            max_input_tokens=max_input_tokens,
            task_id=task_id,
            trace_id=trace_id,
            index_version=index_version,
            metadata={"system_tokens": system_tokens},
        )
        snapshot = {
            "skill_versions": self._skill_versions_text(),
            "knowledge_snapshot": knowledge_snapshot,
            "memory_version": memory_version,
        }
        plan = ContextManager().build(request, sources, snapshot=snapshot)
        return plan

    async def resolve_knowledge(
        self,
        *,
        purpose: str,
        user_id: str = "",
        products: list[str] | None = None,
        query: str = "",
        model_id: str = "deepseek-chat",
        system_tokens: int = 0,
        max_input_tokens: int | None = None,
        task_id: str = "",
        trace_id: str = "",
        index_version: str = "",
    ) -> tuple[str | None, dict[str, Any]]:
        """按模式解析注入文本。

        Returns:
            (content, telemetry)
              - off / shadow：content=None（调用方使用旧路径），telemetry 含 mode
              - active：content=plan.rendered()
        """
        mode = self.effective_mode(user_id)
        if mode == "off":
            return None, {"mode": "off"}

        result = await self.build_plan(
            purpose=purpose,
            user_id=user_id,
            products=products,
            query=query,
            model_id=model_id,
            system_tokens=system_tokens,
            max_input_tokens=max_input_tokens,
            task_id=task_id,
            trace_id=trace_id,
            index_version=index_version,
        )
        if result is None:
            return None, {"mode": mode, "error": "no_sources"}

        telemetry = result.telemetry
        telemetry["mode"] = mode
        if mode == "shadow":
            logger.info(
                "ContextBridge shadow: purpose=%s plan_hash=%s tokens=%d/%d",
                purpose,
                result.plan.plan_hash[:16],
                result.plan.total_tokens,
                result.plan.input_budget_tokens,
            )
            return None, telemetry

        return result.plan.rendered(), telemetry

    # ── 内部实现 ──────────────────────────────────────────

    async def _collect_sources(
        self,
        *,
        purpose: str,
        user_id: str = "",
        products: list[str] | None = None,
        query: str = "",
    ) -> list[ContextSource]:
        sources: list[ContextSource] = []

        # 1. skill_core：purpose 必需 Skill 的 SKILL.md 指令
        skill_names = _PURPOSE_SKILL_SOURCE.get(purpose, ())
        for skill_name, _ref in skill_names:
            manifest = self._load_skill_manifest(skill_name)
            if manifest is None:
                continue
            instructions = self._load_skill_instructions(skill_name)
            if not instructions:
                continue
            sources.append(
                ContextSource(
                    source=f"skill_core:{skill_name}",
                    content=instructions,
                    section_type="skill_core",
                    version=getattr(manifest, "version", "") or "1.0",
                    source_hash=getattr(manifest, "content_hash", ""),
                    trust="system",
                    published=True,
                    required=True,
                )
            )

        # 2. required_product：产品知识切片（全局 + 用户级，purpose 分层）
        try:
            # 阶段七 10.1/10.3：确定知识检索模式（off/shadow/active + 灰度分流）
            from agent.knowledge_shadow import (
                RetrievalShadowTracker,
                effective_retrieval_mode,
                evaluate_stop_conditions,
                record_retrieval_shadow,
            )

            retr_mode = effective_retrieval_mode(self._settings, user_id or "")
            # 旧链路：始终走阶段三切片解析（无检索注入）
            legacy_resolver = KnowledgeSliceResolver(
                self._knowledge_base_dir,
                db=self._db,
            )
            # 新链路：仅当非 off 且带 query 时启用自适应检索
            new_retrieval = retr_mode in ("shadow", "active") and bool(
                (query or "").strip()
            )
            new_resolver = None
            if new_retrieval:
                from agent.document_retriever import DocumentRetriever

                new_resolver = KnowledgeSliceResolver(
                    self._knowledge_base_dir,
                    db=self._db,
                    retriever=DocumentRetriever(
                        index_path=getattr(
                            self._settings, "KNOWLEDGE_INDEX_PATH", None
                        ),
                        settings=self._settings,
                    ),
                    max_optional_docs=int(
                        getattr(self._settings, "KNOWLEDGE_MAX_OPTIONAL_DOCS", 6)
                    ),
                )

            expected_index_version = getattr(
                self._settings, "KNOWLEDGE_INDEX_VERSION", ""
            )

            async def _resolve(resolver, with_query: bool):
                return await resolver.resolve(
                    purpose=purpose,  # type: ignore[arg-type]
                    product_ids=list(products) if products else None,
                    include_shared=True,
                    user_id=user_id or None,
                    query=query if with_query else "",
                    index_version=expected_index_version,
                )

            legacy_slice = await _resolve(legacy_resolver, with_query=False)
            new_slice = None
            if new_resolver is not None:
                # shadow：新链路并行构建但注入旧上下文；active：注入新链路
                tracker = RetrievalShadowTracker(
                    purpose=purpose,
                    user_id=user_id,
                    query=query,
                    product_ids=list(products or []),
                    index_version=expected_index_version,
                )
                try:
                    new_slice = await _resolve(new_resolver, with_query=True)
                except Exception as new_exc:  # 新链路异常不影响旧链路
                    logger.warning(
                        "ContextBridge new retrieval chain failed purpose=%s: %s",
                        purpose,
                        new_exc,
                    )
                    diff = tracker.finish(
                        old_char_count=legacy_slice.char_count,
                        new_char_count=legacy_slice.char_count,
                        old_source_docs=legacy_slice.source_document_ids,
                        required_missing_old=legacy_slice.knowledge_missing,
                        error=str(new_exc),
                    )
                    record_retrieval_shadow(diff)
                    new_slice = None
                else:
                    diff = tracker.finish(
                        old_char_count=legacy_slice.char_count,
                        new_char_count=new_slice.char_count,
                        old_source_docs=legacy_slice.source_document_ids,
                        new_source_docs=new_slice.source_document_ids,
                        required_missing_old=legacy_slice.knowledge_missing,
                        required_missing_new=new_slice.knowledge_missing,
                        new_index_version=new_slice.index_version,
                    )
                    record_retrieval_shadow(diff)
                    stop = evaluate_stop_conditions(diff)
                    if stop:
                        logger.warning(
                            "ContextBridge retrieval stop-condition hit purpose=%s: %s",
                            purpose,
                            "; ".join(stop),
                        )

            # 注入：active 用新链路，shadow/off 用旧链路
            injected_slice = new_slice if retr_mode == "active" and new_slice else legacy_slice
            if injected_slice.content:
                pid_key = ",".join(injected_slice.product_ids or products or [])
                sources.append(
                    ContextSource(
                        source=f"required_product:{pid_key or 'all'}",
                        content=injected_slice.content,
                        section_type="required_product",
                        product=pid_key,
                        doc_type="purpose_layered",
                        source_hash=injected_slice.content_hash,
                        trust="published",
                        published=True,
                        required=True,
                    )
                )
            if injected_slice.knowledge_missing:
                logger.info(
                    "ContextBridge knowledge_missing purpose=%s: %s",
                    purpose,
                    injected_slice.knowledge_missing,
                )
        except Exception as exc:
            logger.warning("ContextBridge slice resolve failed purpose=%s: %s", purpose, exc)

        # 3. skill_references（可选）：draft 用途加载写作规范
        if purpose == "draft":
            for skill_name, ref_path in DRAFT_OPTIONAL_REFERENCES:
                ref_text = self._load_skill_reference(skill_name, ref_path)
                if not ref_text:
                    continue
                sources.append(
                    ContextSource(
                        source=f"skill_references:{skill_name}:{ref_path}",
                        content=ref_text,
                        section_type="skill_references",
                        trust="system",
                        published=True,
                        required=False,
                    )
                )

        return sources

    # ── 版本分量（缓存键 + 快照） ───────────────────────

    def _skill_versions_text(self) -> str:
        """skill 版本文本（name=version:hash），用于 snapshot 与缓存键。"""
        skill_versions: dict[str, str] = {}
        registry = self._registry
        if registry is None:
            try:
                from agent.skill_registry import get_skill_registry

                registry = get_skill_registry()
            except Exception as exc:
                logger.debug("ContextBridge default registry unavailable: %s", exc)
        if registry is not None:
            snapshot = getattr(registry, "snapshot", None)
            if snapshot is not None:
                for name, manifest in getattr(snapshot, "skills", {}).items():
                    content_hash = getattr(manifest, "content_hash", "")
                    version = getattr(manifest, "version", "") or "1.0"
                    skill_versions[name] = (
                        f"{version}:{content_hash[:16]}" if content_hash else version
                    )
        text = ",".join(f"{name}={ver}" for name, ver in sorted(skill_versions.items()))
        return text or "none"

    def _skill_snapshot_hash(self) -> str:
        """Skill 快照整体 hash（registry.snapshot_hash）。"""
        registry = self._registry
        if registry is None:
            try:
                from agent.skill_registry import get_skill_registry

                registry = get_skill_registry()
            except Exception as exc:
                logger.debug("ContextBridge registry unavailable: %s", exc)
        if registry is not None:
            return getattr(registry, "snapshot_hash", "") or "none"
        return "none"

    def _memory_version(self) -> str:
        """记忆配置版本指纹（无记忆时返回 none）。"""
        settings = self._settings
        if not getattr(settings, "MEMORY_READ_MODE", None):
            return "none"
        return f"{settings.MEMORY_READ_MODE}:{settings.MEMORY_ACTIVE_THRESHOLD}"

    async def _knowledge_fingerprint(
        self,
        purpose: str,
        user_id: str,
        products: list[str] | None,
    ) -> str:
        """廉价知识版本指纹：用户条目版本字段 + 全局产品文件 stat。

        用于缓存键；任一用户知识启停/发布、产品文件发布变化都会改变指纹，
        从而自动落新键（版本化失效）。无需全量解析切片即可计算。
        """
        parts: list[str] = []
        if user_id and self._db is not None:
            query: dict[str, Any] = {"user_id": user_id}
            if products:
                query["product_id"] = {"$in": products}
            try:
                docs = await self._db["user_knowledge_entries"].find(query).to_list(length=1000)
                for doc in docs:
                    parts.append(
                        ":".join(
                            [
                                str(doc.get("entry_id", "")),
                                str(doc.get("content_hash", "")),
                                str(doc.get("enabled", "")),
                                str(doc.get("sort_order", "")),
                                str(doc.get("doc_type", "")),
                                str(doc.get("updated_at", "")),
                            ]
                        )
                    )
            except Exception as exc:
                logger.debug("ContextBridge user knowledge fingerprint failed: %s", exc)

        # 全局产品文件（磁盘 stat，廉价）
        try:
            from agent.product_catalog import ProductCatalogService

            catalog = ProductCatalogService(self._knowledge_base_dir)
            for pid in products or []:
                try:
                    for fp in catalog.get_purpose_files(pid, purpose):
                        st = fp.stat()
                        parts.append(f"{pid}:{fp.name}:{st.st_mtime_ns}:{st.st_size}")
                except Exception:
                    continue
            for fp in catalog.get_shared_files(purpose):
                try:
                    st = fp.stat()
                    parts.append(f"shared:{fp.name}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    continue
        except Exception as exc:
            logger.debug("ContextBridge global knowledge fingerprint failed: %s", exc)

        if not parts:
            return "none"
        return "sha256:" + hashlib.sha256(
            "\n".join(sorted(parts)).encode("utf-8")
        ).hexdigest()

    def _telemetry(
        self,
        plan,
        *,
        purpose: str,
        task_id: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        """生成 LLM 日志 telemetry（不含知识全文）。

        阶段3 S3-5：统一记录产品、来源、预算、drop 与 fallback，便于 trace 还原每次来源。
        """
        snapshot = plan.snapshot or {}
        return {
            "context_plan_hash": plan.plan_hash,
            "purpose": purpose,
            "task_id": task_id,
            "trace_id": trace_id,
            "skill_versions": snapshot.get("skill_versions", "none"),
            "knowledge_snapshot": snapshot.get("knowledge_snapshot", "none"),
            "memory_version": snapshot.get("memory_version", "none"),
            "source_ids": [s.source_id for s in plan.sections],
            "products": list(plan.request.products),
            "source": [s.source_id for s in plan.sections],
            "budget_tokens": plan.budget_tokens,
            "total_tokens": plan.total_tokens,
            "dropped": [
                {"source": d.source, "reason": d.reason, "tokens": d.tokens}
                for d in plan.dropped
            ],
            "fallback": None,
            "conflicts": [
                {"source": c.source, "suppressed_by": c.suppressed_by}
                for c in plan.conflicts
            ],
        }

    # ── Skill 加载辅助 ────────────────────────────────────

    def _load_skill_manifest(self, name: str):
        registry = self._registry
        if registry is None:
            try:
                from agent.skill_registry import get_skill_registry

                registry = get_skill_registry()
            except Exception as exc:
                logger.debug("ContextBridge registry unavailable for %s: %s", name, exc)
                return None
        if registry is None or not bool(getattr(registry, "is_ready", False)):
            return None
        try:
            manifests = registry.get_required(
                "draft" if name in ("draft-writing", "compliance-review") else "score"
            )
            for manifest in manifests:
                if manifest.name == name:
                    return manifest
        except Exception as exc:
            logger.warning("ContextBridge get_required failed for %s: %s", name, exc)
        return None

    def _load_skill_instructions(self, name: str) -> str:
        registry = self._registry
        if registry is None:
            try:
                from agent.skill_registry import get_skill_registry

                registry = get_skill_registry()
            except Exception as exc:
                logger.debug("ContextBridge instructions unavailable: %s", exc)
                return ""
        try:
            return (registry.load_instructions(name) or "").strip()
        except Exception as exc:
            logger.warning("ContextBridge load_instructions %s failed: %s", name, exc)
            return ""

    def _load_skill_reference(self, skill_name: str, ref_path: str) -> str:
        registry = self._registry
        if registry is None:
            try:
                from agent.skill_registry import get_skill_registry

                registry = get_skill_registry()
            except Exception as exc:
                logger.debug("ContextBridge reference unavailable: %s", exc)
                return ""
        try:
            return (registry.load_reference(skill_name, ref_path) or "").strip()
        except Exception as exc:
            logger.debug("ContextBridge load_reference %s:%s failed: %s", skill_name, ref_path, exc)
            return ""


class _PlanResult:
    """build_plan 的返回载体（避免 dataclass 对外暴露太多）。"""

    __slots__ = ("plan", "telemetry")

    def __init__(self, plan, telemetry: dict[str, Any]):
        self.plan = plan
        self.telemetry = telemetry
