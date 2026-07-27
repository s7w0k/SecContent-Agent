# 海外版能力裁剪说明

## 裁剪逻辑

海外版采用“AI-BOM 主产品 + 安全能力插件”的组合，不把国内完整智能体安全矩阵整体搬出海。核心原则是：先用 AI-BOM 建立 AI 资产可见性，再按客户成熟度叠加运行时防护、身份治理和安全审计。

## 能力拆分

| 来源产品 | 海外版保留能力 | 海外版不作为主线的能力 |
|---|---|---|
| AI-BOM | 资产发现、AI-BOM 台账、SPDX 3.0、依赖拓扑、影子 AI、生命周期管理、合规报告 | 国内监管专属口径、强绑定国内行业检查的表达 |
| 智能体安全 | MCP 风险检测、Skills 风险检测、RAG/知识库风险检测、提示词注入/越狱/数据泄露监测、事后审计 | 大型平台级安全运营全量能力作为可选，不作为首发必选 |
| 智能体身份安全 | Agent Card、身份目录、访问控制、IBAC、全链路审计 | TEE、区块链存证、司法级举证等重型能力先作为高级选项 |
| 智能体安全网关 | 统一入口、输入输出过滤、上下文限制、API 调用接入、自动日志审计与告警 | 国内专用网关合规叙事 |
| ANS | 统一运营视图、编排入口、跨模块协同 | 不作为海外版主推 SKU，适合大型客户后续扩展 |

## 推荐包装

### 基础版：AI-BOM Core

- 自动资产发现
- AI 资产台账
- SPDX 3.0 兼容字段
- 依赖拓扑
- Shadow AI 发现
- 资产报告导出

适合：客户刚开始做 AI governance，需要先摸清 AI 资产底数。

### 增强版：AI-BOM + Agent Security

- 包含 AI-BOM Core。
- 增加 MCP / Skills / RAG 风险检测。
- 增加提示词注入、越狱、敏感泄露、异常调用监测。
- 增加日志留存、告警溯源、审计报告。

适合：客户已有智能体平台或内部 AI 应用，需要上线前检测和运行时安全。

### 企业版：AI-BOM + Gateway + Identity

- 包含增强版。
- 增加智能体安全网关统一入口。
- 增加 Agent Card、身份目录、访问控制和链路审计。
- 支持按业务、模型、工具、API 维度做权限与审计闭环。

适合：大型集团、多云环境、跨部门 AI 平台、需要持续治理和审计的客户。

## 对外叙事

海外版不要讲成“国内智能体安全全家桶出海”，而要讲成：

> AI-BOM-first AI governance platform: discover every AI asset, map every dependency, detect Shadow AI, and provide audit-ready evidence. Runtime protection, agent identity, and gateway controls can be added as modular security layers.

中文内部口径：

> 海外版以 AI-BOM 为入口，解决 AI 资产可见性和供应链透明问题；智能体安全、身份安全和网关能力作为模块化增强，按客户成熟度逐步叠加。

---
## 原始来源

- [海外版总览](./overview.md)
- [AI-BOM 海外版](./AI-BOM/overview.md)
- [智能体安全平台 428 原文图表整理](../2-智能体安全/原始文档/亚信安全智能体安全平台解决方案%20428-原文图表整理.md)
- [智能体身份安全说明](../1-智能体身份安全/overview.md)
- [共享技术架构](../shared/architecture-brief.md)
