"""临时知识库校验、Prompt预览与试打分服务。"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from pathlib import Path

from agent.knowledge import KnowledgeLoader
from knowledge_admin.usage_classifier import UsageClassifier

logger = logging.getLogger("backend.knowledge_admin.preview")


class KnowledgePreviewService:
    """在隔离临时目录中校验草稿并预览影响。"""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).resolve()

    def create_temp_snapshot(self, drafts: list[dict]) -> str:
        """创建临时知识库快照，用草稿替换目标文件。

        Args:
            drafts: [{"relative_path": str, "content_md": str}, ...]

        Returns:
            临时目录路径
        """
        tmp_dir = tempfile.mkdtemp(prefix="kb-preview-")
        tmp_root = Path(tmp_dir) / self.root_dir.name

        # Copy entire knowledge base
        shutil.copytree(self.root_dir, tmp_root, dirs_exist_ok=False)

        # Replace draft files
        for draft in drafts:
            rel_path = draft["relative_path"]
            content = draft["content_md"]
            target = tmp_root / rel_path

            # Validate path is within temp root
            if not target.resolve().is_relative_to(tmp_root.resolve()):
                logger.warning("Skipping unsafe path in preview: %s", rel_path)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        logger.info(
            "Temp knowledge snapshot created at %s (%d drafts applied)", tmp_root, len(drafts)
        )
        return str(tmp_root)

    def cleanup_temp(self, tmp_dir: str) -> None:
        """清理临时目录。"""
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # Also remove parent if empty
            parent = Path(tmp_dir).parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as exc:
            logger.warning("Failed to cleanup temp dir %s: %s", tmp_dir, exc)

    async def validate_draft(
        self,
        relative_path: str,
        content_md: str,
    ) -> dict:
        """校验草稿内容并返回结果。

        Returns:
            {
                "status": "passed" | "failed",
                "errors": [str],
                "warnings": [str],
                "loader_file_count": int,
                "loader_relevant_count": int,
            }
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Basic content validation
        if not content_md or not content_md.strip():
            errors.append("内容不能为空")

        if "\x00" in content_md:
            errors.append("内容包含 NUL 字符")

        # Check for at least one heading in protected files
        if UsageClassifier.is_protected_path(relative_path) and not any(
            line.strip().startswith("#") for line in content_md.split("\n")
        ):
            errors.append("核心打分文件必须包含至少一个 Markdown 标题")

        # Check if file is a direct scoring file
        is_direct = UsageClassifier.is_direct_scoring_prompt(relative_path)
        if not is_direct:
            warnings.append("此文件不直接参与 V2 打分 Prompt，修改后 V2 Prompt 可能不变")

        # Create temp snapshot and validate with Loader
        loader_file_count = 0
        loader_relevant_count = 0

        if not errors:
            tmp_dir = None
            try:
                tmp_dir = self.create_temp_snapshot(
                    [{"relative_path": relative_path, "content_md": content_md}]
                )
                tmp_loader = KnowledgeLoader(docs_dir=tmp_dir)
                knowledge = await tmp_loader.load(force=True)

                loader_file_count = len(tmp_loader._discover_files())
                loader_relevant_count = len(knowledge.source_files)

                # Verify knowledge loaded successfully
                if loader_relevant_count == 0:
                    errors.append("加载后未发现任何评分相关文件")

            except Exception as exc:
                errors.append(f"Loader 校验失败: {exc}")
            finally:
                if tmp_dir:
                    self.cleanup_temp(tmp_dir)

        status = "failed" if errors else "passed"
        return {
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "loader_file_count": loader_file_count,
            "loader_relevant_count": loader_relevant_count,
        }

    async def preview_prompt(
        self,
        relative_path: str,
        content_md: str,
    ) -> dict:
        """预览新旧打分 Prompt 对比。

        Returns:
            {
                "old_prompt": str,
                "new_prompt": str,
                "old_hash": str,
                "new_hash": str,
                "prompt_changed": bool,
                "file_in_prompt": bool,
                "char_count_old": int,
                "char_count_new": int,
            }
        """
        # Get old prompt from formal loader
        formal_loader = KnowledgeLoader(docs_dir=str(self.root_dir))
        await formal_loader.load(force=True)
        old_prompt = formal_loader.as_scoring_prompt()
        old_hash = hashlib.sha256(old_prompt.encode("utf-8")).hexdigest()

        # Get new prompt from temp snapshot
        tmp_dir = None
        try:
            tmp_dir = self.create_temp_snapshot(
                [{"relative_path": relative_path, "content_md": content_md}]
            )
            temp_loader = KnowledgeLoader(docs_dir=tmp_dir)
            await temp_loader.load(force=True)
            new_prompt = temp_loader.as_scoring_prompt()
            new_hash = hashlib.sha256(new_prompt.encode("utf-8")).hexdigest()
        finally:
            if tmp_dir:
                self.cleanup_temp(tmp_dir)

        # Check if the file is actually in the scoring prompt
        is_direct = UsageClassifier.is_direct_scoring_prompt(relative_path)

        return {
            "old_prompt": old_prompt,
            "new_prompt": new_prompt,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "prompt_changed": old_hash != new_hash,
            "file_in_prompt": is_direct,
            "char_count_old": len(old_prompt),
            "char_count_new": len(new_prompt),
        }

    async def preview_score(
        self,
        relative_path: str,
        content_md: str,
        article: dict,
    ) -> dict:
        """试打分对比新旧知识下的评分差异。

        Args:
            relative_path: 草稿文件相对路径
            content_md: 草稿内容
            article: 测试文章 dict (title, source, category_v2, summary_cn, content_md)

        Returns:
            {
                "old_score": dict | None,
                "new_score": dict | None,
                "score_changed": bool,
            }
        """
        from agent.scorer_v2 import ScoringAgentV2
        from config import get_settings
        from langchain_openai import ChatOpenAI

        settings = get_settings()
        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0.1,
            timeout=settings.DEEPSEEK_TIMEOUT,
        )

        # Score with formal knowledge
        formal_loader = KnowledgeLoader(docs_dir=str(self.root_dir))
        await formal_loader.load(force=True)
        formal_scorer = ScoringAgentV2(llm=llm, knowledge=formal_loader, db=None)

        old_score = None
        try:
            old_score = await formal_scorer.score_single(article)
        except Exception as exc:
            logger.warning("Formal scoring failed: %s", exc)
            old_score = {"error": str(exc)}

        # Score with draft knowledge
        tmp_dir = None
        new_score = None
        try:
            tmp_dir = self.create_temp_snapshot(
                [{"relative_path": relative_path, "content_md": content_md}]
            )
            temp_loader = KnowledgeLoader(docs_dir=tmp_dir)
            await temp_loader.load(force=True)
            temp_scorer = ScoringAgentV2(llm=llm, knowledge=temp_loader, db=None)
            new_score = await temp_scorer.score_single(article)
        except Exception as exc:
            logger.warning("Draft scoring failed: %s", exc)
            new_score = {"error": str(exc)}
        finally:
            if tmp_dir:
                self.cleanup_temp(tmp_dir)

        return {
            "old_score": old_score,
            "new_score": new_score,
            "score_changed": _scores_differ(old_score, new_score),
        }


def _scores_differ(old: dict | None, new: dict | None) -> bool:
    """比较两次评分结果是否有差异。"""
    if old is None or new is None:
        return old != new
    if "error" in old or "error" in new:
        return True
    return (
        old.get("product_relevance") != new.get("product_relevance")
        or old.get("event_impact") != new.get("event_impact")
        or old.get("pr_total_score") != new.get("pr_total_score")
    )
