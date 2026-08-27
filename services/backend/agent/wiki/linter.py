"""Wiki Linter - 编译后发布前的静态校验。

PR-03 第四步，至少检查：
  schema_valid / source_ref_valid / broken_link / duplicate_page_id /
  orphan_page / empty_page / ungrounded_claim / stale_source / conflict

只有 LintResult 通过（无致命错误）才允许进入发布。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.wiki.contracts import (
    WikiPageMeta,
    compute_claim_id,
    is_valid_page_id,
)
from agent.wiki.store import WikiStore, extract_frontmatter, meta_from_frontmatter

logger = logging.getLogger("backend.agent.wiki.linter")

FATAL_CODES = frozenset({"schema_valid", "source_ref_valid", "broken_link", "duplicate_page_id"})

# Phase 15 / PR-18：加固阈值与规则集
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
KNOWN_RELATION_TYPES = frozenset(
    {
        "belongs_to",
        "related_to",
        "mitigates",
        "depends_on",
        "requires",
        "subsumes",
        "example_of",
        "alternative_to",
        "synonym_of",
        "implemented_by",
        "extended_by",
        "synthesis_of",
    }
)
# 嵌套于 product 命名空间下的页面类型（product.<pid>.<type>.<slug>）
_PRODUCT_NESTED_TYPES = frozenset(
    {"capability", "scenario", "integration", "limitation", "positioning", "overview"}
)

MAX_PAGE_BYTES = 100_000
MAX_SECTION_BYTES = 30_000
MAX_RELATIONS_PER_PAGE = 30

# prompt injection 特征（Raw Source / Wiki 内容一律视为低信任数据，Phase 16.1）
_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "忽略之前的指令",
    "ignore all previous",
    "disregard previous",
    "forget all instructions",
    "you are now",
    "reveal your system prompt",
    "print your system prompt",
    "system instruction override",
    "override your instructions",
    "你是新的系统",
    "devmode",
    "jailbreak",
)
# secret / credential 特征（Phase 19.4 Secret Quarantine）
_SECRET_RE = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"""(?i)api[_-]?key[\s:=]+\"?(?:sk-|AKIA|AIza)"""),
    re.compile(r"""(?i)(?:mongodb\+srv|mongodb|postgres(?:ql)?|mysql)://[^\s:@]+:"""),
]
_SYSTEM_PROMPT_RESIDUE = (
    "you are a helpful assistant",
    "as an ai",
    "you are an llm",
    "你能帮助用户",
    "你的角色是",
)


@dataclass
class LintResult:
    """Lint 结果。ok = 无致命错误。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(_is_fatal(e) for e in self.errors)

    def __bool__(self) -> bool:
        return self.ok

    def add(self, error: str) -> None:
        self.errors.append(error)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def __str__(self) -> str:
        if self.ok and not self.errors:
            return f"LINT PASS ({len(self.warnings)} warnings)"
        return f"LINT FAIL: {len(self.errors)} errors, {len(self.warnings)} warnings"


def _is_fatal(error: str) -> bool:
    return any(error.startswith(code) or error.startswith(code + "[") for code in FATAL_CODES)


class WikiLinter:
    """Wiki Linter。registry 可选：提供则为 stale_source 校验提供源哈希。"""

    def __init__(self, store: WikiStore, registry: Any | None = None):
        self.store = store
        self.registry = registry

    # ── 主入口 ────────────────────────────────────────────

    def lint(self, page_ids: list[str] | None = None) -> LintResult:
        result = LintResult()
        target_ids = page_ids or self.store.list_page_ids()
        if not target_ids:
            return result

        self._lint_duplicates(result)

        # 建立外链映射用于 orphan / broken 检测
        outbound: dict[str, set[str]] = {}
        page_types: dict[str, str] = {}

        for page_id in target_ids:
            page = self._open(page_id)
            if page is None:
                result.add(f"schema_valid[{page_id}] 页面无法解析")
                continue

            page_types[page_id] = page.meta.page_type
            outbound[page_id] = {r.target_page_id for r in page.meta.relations}
            self._lint_page(result, page)

        self._lint_orphans(result, target_ids, outbound, page_types)

        return result

    # ── 页面级检查 ────────────────────────────────────────

    def _open(self, page_id: str):
        try:
            return self.store.open_page(page_id)
        except Exception as exc:
            logger.debug("open_page failed %s: %s", page_id, exc)
            return None

    def _lint_page(self, result: LintResult, page) -> None:
        page_id = page.meta.page_id
        meta = page.meta

        # empty_page
        if not page.body.strip() and not page.sections:
            result.add(f"empty_page[{page_id}]")

        # ── Schema（Phase 15）──────────────────────────────
        if meta.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            result.add(f"unsupported_schema[{page_id}] schema_version={meta.schema_version}")
        if not self._namespace_matches(meta.page_id, meta.page_type):
            result.add(
                f"namespace_mismatch[{page_id}] page_type={meta.page_type!r} 与 page_id 命名空间不符"
            )

        # ── Claim（Phase 3/Phase 15）───────────────────────
        seen_claims: set[str] = set()
        for c in meta.claims:
            cid = c.claim_id or compute_claim_id(
                product_id=meta.product_id or "",
                claim_type=c.claim_type,
                semantic_key=c.text,
            )
            if cid in seen_claims:
                result.add(f"duplicate_claim_id[{page_id}] 重复 claim_id: {cid}")
            seen_claims.add(cid)

        # ── Provenance（Phase 15）──────────────────────────
        for ref in meta.source_refs:
            for err in self.store.check_source_ref(ref):
                result.add(f"source_ref_valid[{page_id}] {err}")
            self._lint_stale_source(result, page_id, ref)
            self._lint_deleted_source(result, page_id, ref)

        # ── Link（Phase 15）────────────────────────────────
        seen_links: set[tuple[str, str]] = set()
        if len(meta.relations) > MAX_RELATIONS_PER_PAGE:
            result.warn(
                f"excessive_relations[{page_id}] {len(meta.relations)} 条关系超过上限 {MAX_RELATIONS_PER_PAGE}"
            )
        for rel in meta.relations:
            if rel.relation_type not in KNOWN_RELATION_TYPES:
                result.add(f"invalid_relation[{page_id}] 未知关系类型: {rel.relation_type!r}")
            if self._is_broken_target(rel.target_page_id):
                result.add(f"broken_link[{page_id}] 目标不存在: {rel.target_page_id}")
            if rel.target_page_id == page_id:
                result.add(f"self_link[{page_id}] 自引用关系")
            if (rel.relation_type, rel.target_page_id) in seen_links:
                result.add(
                    f"duplicate_relation[{page_id}] ({rel.relation_type}, {rel.target_page_id})"
                )
            seen_links.add((rel.relation_type, rel.target_page_id))

        # ungrounded_claim（fact 类页面必须有 source_refs）
        if (
            meta.page_type in {"capability", "limitation", "scenario"}
            and not meta.source_refs
            and not _page_mentions_source(page)
        ):
            result.add(f"ungrounded_claim[{page_id}]")

        # ── Content（Phase 15）─────────────────────────────
        self._lint_content(result, page)

    def _namespace_matches(self, page_id: str, page_type: str) -> bool:
        """page_type 与 page_id 命名空间是否一致。

        product 下的子类型（capability/scenario/...）合法形态为
        product.<pid>.<type>.<slug>；其余类型首段必须等于其类型名。
        """
        ns = page_id.split(".", 1)[0]
        if page_type in _PRODUCT_NESTED_TYPES and ns in {"product", page_type}:
            return True
        return ns == page_type

    def _is_broken_target(self, target_page_id: str) -> bool:
        if not is_valid_page_id(target_page_id):
            return True
        return not self.store.page_exists(target_page_id)

    def _lint_stale_source(self, result: LintResult, page_id: str, ref) -> None:
        if self.registry is None:
            return
        entry = self.registry.get(ref.source_id)
        if entry is None:
            result.add(f"stale_source[{page_id}] 源不存在: {ref.source_id}")
        elif entry.sha256 != ref.content_hash:
            result.add(f"stale_source[{page_id}] 源哈希变化: {ref.relative_path}")

    def _lint_deleted_source(self, result: LintResult, page_id: str, ref) -> None:
        """Deleted Source 不得出现在已发布内容中（Phase 15 Provenance）。"""
        if self.registry is None:
            return
        entry = self.registry.get(ref.source_id)
        if entry is not None and entry.status == "deleted":
            result.add(f"deleted_source[{page_id}] 引用已删除源: {ref.relative_path}")

    def _lint_content(self, result: LintResult, page) -> None:
        """Content 检查：huge / secret / injection / system-prompt residue / 递归。"""
        page_id = page.meta.page_id
        total = len(page.body.encode("utf-8"))
        seen_bodies: set[str] = set()
        for sec in page.sections:
            n = len(sec.body.encode("utf-8"))
            if n > MAX_SECTION_BYTES:
                result.warn(f"huge_section[{page_id}] 章节 {sec.title!r} 过大: {n}")
            if not sec.body.strip():
                result.warn(f"empty_section[{page_id}] 章节 {sec.title!r} 为空")
            norm = " ".join(sec.body.split())
            if norm in seen_bodies:
                result.warn(f"duplicate_content[{page_id}] 章节 {sec.title!r} 内容重复")
            seen_bodies.add(norm)
            total += n
        if total > MAX_PAGE_BYTES:
            result.warn(f"huge_page[{page_id}] 页面过大: {total} bytes")

        text = (page.body or "") + "\n" + "\n".join(s.body for s in page.sections)
        _check_injections(result, page_id, text)
        _check_secrets(result, page_id, text)
        for phrase in _SYSTEM_PROMPT_RESIDUE:
            if phrase.lower() in text.lower():
                result.warn(f"system_prompt_residue[{page_id}] 疑似系统提示残留: {phrase!r}")
                break

    # ── 重复 page_id ──────────────────────────────────────

    def _lint_duplicates(self, result: LintResult) -> None:
        counts: dict[str, list[str]] = {}
        if not self.store.root.is_dir():
            return
        for fp in self.store.root.rglob("*.md"):
            if "_meta" in fp.parts:
                continue
            meta = self._read_page_meta(fp)
            if meta is None:
                continue
            counts.setdefault(meta.page_id, []).append(str(fp))
        for page_id, paths in counts.items():
            if len(paths) > 1:
                result.add(f"duplicate_page_id[{page_id}] {len(paths)} 个文件: {paths}")

    def _read_page_meta(self, path: Path) -> WikiPageMeta | None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk")
        fm_text, _ = extract_frontmatter(text)
        if not fm_text:
            return None
        try:
            return meta_from_frontmatter(fm_text)
        except Exception:
            return None

    # ── orphan ────────────────────────────────────────────

    def _lint_orphans(
        self,
        result: LintResult,
        target_ids: list[str],
        outbound: dict[str, set[str]],
        page_types: dict[str, str],
    ) -> None:
        inbound: dict[str, int] = {}
        for targets in outbound.values():
            for t in targets:
                inbound[t] = inbound.get(t, 0) + 1
        for page_id in target_ids:
            if page_types.get(page_id) in {"product", "concept", "competitor", "synthesis"}:
                continue
            if inbound.get(page_id, 0) == 0:
                result.warn(f"orphan_page[{page_id}] 无入链")


def _page_mentions_source(page) -> bool:
    """页面是否至少在证据章节透露真实来源（[来源: ...] 标记）。

    仅标题叫 Source 但无来源标记的页面不能算作 grounded（无 provenance）。
    """
    for sec in page.sections:
        if (
            ("来源" in sec.title or "Source" in sec.title)
            and "[来源:" in sec.body
            and "未 grounding" not in sec.body
        ):
            return True
    return False


def _check_injections(result: LintResult, page_id: str, text: str) -> None:
    """Prompt Injection 检测：命中任一特征即记为错误（Phase 16.1）。"""
    low = text.lower()
    for pat in _INJECTION_PATTERNS:
        if pat.lower() in low:
            result.add(f"prompt_injection[{page_id}] 命中注入特征: {pat!r}")
            return


def _check_secrets(result: LintResult, page_id: str, text: str) -> None:
    """Secret / Credential 检测：明文密钥不应进入 Wiki 内容（Phase 19.4）。"""
    for pat in _SECRET_RE:
        if pat.search(text):
            result.add(f"secret_credential[{page_id}] 命中密钥特征: {pat.pattern!r}")
            return
