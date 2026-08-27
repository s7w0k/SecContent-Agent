"""Wiki CLI（Phase 22 / PR-21）— Bootstrap / Validate 运维入口。

- `python -m agent.wiki.cli bootstrap`：从 Raw Sources 出发，事务式构建并发布
  一个 Active Wiki Artifact（Compiler → Lint → Gate → Publish → Commit）。
  生产不依赖“某台机器之前运行过 Compiler”。
- `python -m agent.wiki.cli validate`：校验当前生产 Backend 是否满足
  Phase 23 的全部生产门禁（active/manifest/registry/schema/grounding）。

依赖说明：标准库 + 已有 wiki 模块，无第三方新增依赖。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("backend.agent.wiki.cli")


def _wire_maintainer(settings: Any, db: Any | None = None) -> Any:
    """根据 settings 装配 WikiMaintainer（SourceRegistry + Store + Linter + Publisher）。"""
    from agent.wiki.compiler import PageCompiler
    from agent.wiki.index import WikiIndex, WikiIndexStore
    from agent.wiki.linter import WikiLinter
    from agent.wiki.maintainer import WikiMaintainer
    from agent.wiki.publisher import WikiPublisher
    from agent.wiki.runtime_factory import _meta_dir, _registry_path, resolve_wiki_root
    from agent.wiki.source_registry import SourceRegistry
    from agent.wiki.store import WikiStore

    wiki_root = resolve_wiki_root(settings)
    meta = _meta_dir(wiki_root)
    source_root = Path(getattr(settings, "KNOWLEDGE_BASE_DIR", "/app/docs"))
    registry = SourceRegistry(source_root, _registry_path(wiki_root))
    store = WikiStore(wiki_root)
    linter = WikiLinter(store, registry)
    index_store = WikiIndexStore(meta)
    index = WikiIndex(index_store.load())
    compiler = PageCompiler()
    publisher = WikiPublisher(
        store,
        linter,
        meta_dir=meta,
        source_registry=registry,
        require_grounding=bool(getattr(settings, "WIKI_REQUIRE_SOURCE_GROUNDING", True)),
        compiler_version=getattr(compiler, "version", "deterministic-1"),
        schema_version=getattr(settings, "WIKI_SCHEMA_VERSION", 2),
        source_snapshot=registry.active_snapshot(),
        parent_wiki_version=index.wiki_version,
    )
    return WikiMaintainer(
        store,
        registry,
        index=index,
        compiler=compiler,
        publisher=publisher,
        compiler_version=getattr(compiler, "version", "deterministic-1"),
        schema_version=getattr(settings, "WIKI_SCHEMA_VERSION", 2),
    )


async def _cmd_bootstrap(settings: Any, db: Any | None = None) -> int:
    maintainer = _wire_maintainer(settings, db)
    t0 = time.perf_counter()
    result = await maintainer.run_transaction(auto_publish=True)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    state = result.get("state")
    ok = state in {"COMMITTED", "PUBLISHED"}
    logger.info(
        "bootstrap done state=%s build=%s pages=%d published=%s latency_ms=%.1f",
        state,
        result.get("build_id"),
        len(result.get("rebuilt_page_ids", [])),
        result.get("published"),
        elapsed_ms,
    )
    print(
        f"bootstrap state={state} build_id={result.get('build_id')} "
        f"wiki_version={result.get('wiki_version') or ''} "
        f"published={result.get('published')} "
        f"publish_errors={result.get('publish_errors') or []}"
    )
    return 0 if ok else 1


async def _cmd_validate(settings: Any, db: Any | None = None) -> int:
    from agent.wiki.index import WikiIndexStore
    from agent.wiki.manifest import ManifestStore
    from agent.wiki.runtime_factory import (
        _meta_dir,
        _registry_path,
        resolve_wiki_root,
        validate_production_gates,
    )
    from agent.wiki.source_registry import SourceRegistry

    wiki_root = resolve_wiki_root(settings)
    meta = _meta_dir(wiki_root)
    manifest_store = ManifestStore(meta / "versions")
    index_store = WikiIndexStore(meta)
    source_root = Path(getattr(settings, "KNOWLEDGE_BASE_DIR", "/app/docs"))
    registry = SourceRegistry(source_root, _registry_path(wiki_root))

    gates = validate_production_gates(
        settings,
        mode=getattr(settings, "KNOWLEDGE_BACKEND", "wiki"),
        wiki_root=wiki_root,
        manifest_store=manifest_store,
        index_store=index_store,
        registry=registry,
    )
    if not gates.get("enforced"):
        print("validate: 非 wiki 模式，未强制生产门禁")
        return 0
    for name, ok in gates.items():
        if name == "enforced":
            continue
        print(f"  [{('PASS' if ok else 'FAIL')}] {name}")
    return 0


def _load_settings() -> Any:
    # 惰性加载，允许在无完整环境时以轻量 settings 运行
    try:
        from backend.config import load_settings

        return load_settings()
    except Exception:
        import os
        from types import SimpleNamespace

        return SimpleNamespace(
            KNOWLEDGE_BACKEND=os.environ.get("KNOWLEDGE_BACKEND", "wiki"),
            KNOWLEDGE_BASE_DIR=os.environ.get("KNOWLEDGE_BASE_DIR", "/app/docs"),
            WIKI_ROOT_DIR=os.environ.get("WIKI_ROOT_DIR", ""),
            WIKI_REQUIRE_SOURCE_GROUNDING=os.environ.get(
                "WIKI_REQUIRE_SOURCE_GROUNDING", "true"
            ).lower()
            in {"1", "true", "yes"},
            WIKI_SCHEMA_VERSION=2,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.wiki.cli", description="LLM Wiki CLI")
    parser.add_argument(
        "command", nargs="?", default="bootstrap", choices=["bootstrap", "validate"]
    )
    parser.add_argument("--debug", action="store_true", help="输出 debug 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    settings = _load_settings()
    handler: dict[str, Any] = {
        "bootstrap": _cmd_bootstrap,
        "validate": _cmd_validate,
    }
    return asyncio.run(handler[args.command](settings, None))


if __name__ == "__main__":
    sys.exit(main())
