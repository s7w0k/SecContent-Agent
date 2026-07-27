# 智能体安全产品速查包

> 下载即用的 AI 可读产品资料包。无需安装或搭建平台，打开文件夹就可以问 AI。

## 这是什么

这里放公司智能体安全产品线的产品 brief、共享资料、原始文档和回答规则，方便售前、技术方案和市场 PR 快速查资料。`README.md` 是人类入口；AI 工具应优先读取 `CLAUDE.md`，不识别 `CLAUDE.md` 时使用 `AGENTS.md` 作为兼容入口。

## 适合谁用

- 售前：查产品定位、卖点、竞品和常见问题。
- 技术方案：查架构、部署、接口和边界。
- 市场 PR：查热点匹配、传播角度和可用素材。
- 维护者：按产品资料状态和未命中问题维护资料。

## 覆盖产品

| 目录 | 当前资料状态 | 用途 |
|---|---|---|
| `0-产品全景/` | 产品全景资料 | 产品矩阵、关系图和术语 |
| `1-智能体身份安全/` | 已开发完成 | 身份安全产品资料 |
| `2-智能体安全/` | 已开发完成 | 智能体检测、监测和审计资料 |
| `3-AI-BOM/` | 已开发完成 | AI 资产与供应链资料 |
| `4-智能体安全网关/` | 已开发完成 | 智能体调用入口与运行时控制资料 |
| `5-ANS/` | 已开发完成 | 智能体编排与总控资料 |
| `海外版/` | 已形成产品组合 | 海外版产品组合资料 |
| `shared/` | 共享资料 | 技术总图、竞品和热点匹配 |

产品均已开发完成，但资料覆盖程度和对外确认程度不同。具体能力、交付时间、性能指标、客户案例和竞品结论，都必须以文件中的明确内容为准。

## 怎么问 AI

1. 下载并解压，在资料包根目录打开 Claude Code、Codex 或其他 AI 工具。
2. 直接说明产品、问题和使用场景，例如“面向技术方案，说明智能体身份安全的架构和部署边界，并列出读取文件”。
3. 也可以问“根据现有资料比较 AI-BOM 与某方案的已知差异；没有依据的内容请标为资料未覆盖”。
4. 不确定产品时，让 AI 先读取 `_index/folder-routing.md`；不要要求它一次性读取整个资料包。

AI 应先按问题路由到 1-2 个产品目录，再读取相关 `overview.md` 和角色 brief。资料覆盖不足的能力不能自动推断，必须按规则回查原始文档或标记为“资料未覆盖”。

## 目录结构

```text
README.md                         人类入口
CLAUDE.md                         AI 入口与路由规则
AGENTS.md                         不识别 CLAUDE.md 时使用的兼容入口
qa-log.md                         未命中问题日志

_index/
├── folder-routing.md             按问题路由产品目录
├── 交接提示词.md                  维护交接说明
├── optimization-steps/            分步骤维护卡片
└── 原始文档结构化指南/             原始文档整理指南

skills/
├── common-rules.md                通用回答流程
├── presales.md                    售前回答流程
├── architect.md                   技术方案回答流程
└── market-pr.md                   市场 PR 回答流程

0-产品全景/
├── overview.md
├── product-map.md
├── glossary.md
└── 项目核心逻辑.md

1-智能体身份安全/
├── overview.md
├── sales-brief.md
├── architecture-brief.md
├── market-brief.md
├── tasks.md
└── 原始文档/

2-智能体安全/
├── overview.md
├── sales-brief.md
├── architecture-brief.md
├── market-brief.md
├── tasks.md
└── 原始文档/

3-AI-BOM/
├── overview.md
├── sales-brief.md
├── architecture-brief.md
├── market-brief.md
├── tasks.md
└── 原始文档/

4-智能体安全网关/
├── overview.md
└── tasks.md

5-ANS/
├── overview.md
├── tasks.md
└── 原始文档/

海外版/
├── overview.md
├── capability-slicing.md
└── AI-BOM/overview.md

shared/
├── architecture-brief.md
├── competitor-brief.md
└── hot-event-playbook.md
```

各产品目录中的 `原始文档/` 不是常规第一入口。它只在对应 brief 不足以回答问题时作为兜底；原始文档也没有的信息，应明确说“资料未覆盖”，不要自行补全。

## 常见维护动作

- 产品版本、待办或资料确认状态变了：修改对应产品目录的 `tasks.md`。
- 已确认的产品事实变了：修改对应产品 brief，不要写进 `skills/`。
- AI 答不上来的问题：查对应产品的 `原始文档/`；仍没有答案时记入 `qa-log.md`。
- 问题被路由到错误目录：修改 `_index/folder-routing.md`。
- 产品目录的资料覆盖范围变化：同步修改 `_index/folder-routing.md` 的资料边界说明。

维护时保持现有目录轻量，不新增大型索引、证据库或热点库。PPT 中的指标、客户案例和部署规格要标注“PPT 提及，需产品确认”。

## 资料边界

- `产品 brief` 是回答产品事实的常规来源。
- `原始文档/` 是 brief 不足时的兜底来源，不是常规第一入口。
- `skills/` 只写回答流程、读取顺序和表达要求，不是产品事实来源。
- `CLAUDE.md` 负责 AI 入口和路由；`AGENTS.md` 是兼容入口；`_index/folder-routing.md` 负责按问题选择产品目录。
- brief 和原始文档都没有的内容，写“资料未覆盖”或记入待确认，不编造功能、时间、指标、案例和竞品结论。

## 推荐入口文件

| 需要做什么 | 先看什么 |
|---|---|
| 人类了解资料包 | `README.md` |
| 支持 `CLAUDE.md` 的 AI 工具 | `CLAUDE.md` |
| 不识别 `CLAUDE.md` 的 AI 工具 | `AGENTS.md`，再按其中顺序读取 |
| 不确定该查哪个产品 | `_index/folder-routing.md` |
| 查看未命中问题 | `qa-log.md` |
