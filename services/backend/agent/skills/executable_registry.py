"""ExecutableSkillRegistry - 可执行 Skill 注册表（计划 §13）。

把"文件 Manifest"（skill_registry.SkillRegistry）与"可执行 Executor"
统一到一个权威注册表，并在注册时执行硬校验：
  - executor.name == manifest.name
  - manifest.required_tools ⊆ BusinessToolRegistry.names()
  - manifest.status == published
  - manifest.risk_level ∈ 允许集合
注册通过后，SkillRuntime 才能解析并执行该 Skill。
"""

from __future__ import annotations

from agent.skills.contracts import SkillExecutor, SkillManifest

_ALLOWED_RISK_LEVELS = {"low", "medium", "high"}


class SkillRegistrationError(RuntimeError):
    """Skill 注册不满足契约。"""


class SkillExecutionError(RuntimeError):
    """Skill 执行期错误（Runtime 包装/解包用）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ExecutableSkillRegistry:
    """维护 name → (manifest, executor) 的权威表。

    用法：
        registry = ExecutableSkillRegistry(business_tool_names=business_registry.names())
        registry.register(ProductScoringSkill())   # executor 自报 required_tools
    """

    def __init__(
        self,
        *,
        business_tool_names: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    ):
        self._business_tools = frozenset(business_tool_names or ())
        self._executors: dict[str, SkillExecutor] = {}
        self._manifests: dict[str, SkillManifest] = {}

    # ── 注册 ──────────────────────────────────────────────

    def register(
        self,
        executor: SkillExecutor,
        manifest: SkillManifest | None = None,
    ) -> None:
        """注册 executor 与可选 manifest。

        manifest 未传入时，使用 executor 自带的信息构建（executor 应实现
        `skill_manifest` 属性或 `required_tools`）。
        """
        name = getattr(executor, "name", "")
        if not name or not isinstance(name, str):
            raise SkillRegistrationError("executor.name 必须是非空字符串")

        if manifest is None:
            manifest = self._manifest_from_executor(executor)

        if manifest.name != name:
            raise SkillRegistrationError(f"executor.name({name}) != manifest.name({manifest.name})")
        if manifest.status != "published":
            raise SkillRegistrationError(f"Skill '{name}' 未发布(status={manifest.status})")
        if manifest.risk_level not in _ALLOWED_RISK_LEVELS:
            raise SkillRegistrationError(f"Skill '{name}' 非法 risk_level: {manifest.risk_level}")
        unknown = sorted(set(manifest.required_tools) - self._business_tools)
        if unknown:
            raise SkillRegistrationError(
                f"Skill '{name}' 引用未知 Tool: {unknown}；可用工具: {sorted(self._business_tools)}"
            )
        if name in self._executors:
            raise SkillRegistrationError(f"Skill '{name}' 重复注册")

        self._executors[name] = executor
        self._manifests[name] = manifest

    def _manifest_from_executor(self, executor: SkillExecutor) -> SkillManifest:
        name = executor.name
        required_tools = tuple(getattr(executor, "required_tools", ()))
        description = getattr(executor, "description", "") or name
        version = getattr(executor, "version", "1.0.0")
        risk_level = getattr(executor, "risk_level", "low")
        required_scopes = frozenset(getattr(executor, "required_scopes", ()))
        max_tool_calls = getattr(executor, "max_tool_calls", 20)
        output_artifact_type = getattr(executor, "output_artifact_type", "")
        return SkillManifest(
            name=name,
            version=version,
            description=description,
            required_tools=required_tools,
            risk_level=risk_level,
            required_scopes=required_scopes,
            max_tool_calls=max_tool_calls,
            output_artifact_type=output_artifact_type,
        )

    # ── 查询 ──────────────────────────────────────────────

    def get(self, name: str) -> SkillExecutor:
        try:
            return self._executors[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name}") from None

    def get_manifest(self, name: str) -> SkillManifest:
        try:
            return self._manifests[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name}") from None

    def names(self) -> list[str]:
        return sorted(self._executors)

    def __contains__(self, name: str) -> bool:
        return name in self._executors

    def __len__(self) -> int:
        return len(self._executors)

    def skill_snapshot_hash(self) -> str:
        """Skill 快照指纹（计划 §29 skill_snapshot_hash）。"""
        import hashlib
        import json

        payload = {
            "skills": {
                name: {
                    "version": m.version,
                    "risk": m.risk_level,
                    "tools": sorted(m.required_tools),
                }
                for name, m in sorted(self._manifests.items())
            }
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
