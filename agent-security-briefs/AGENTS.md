# 智能体安全产品速查

本文件是给不识别 `CLAUDE.md` 的 AI 工具看的入口副本。

请先阅读本目录下的 `CLAUDE.md`。它现在只负责入口路由，具体身份规则已经拆到：

- `skills/common-rules.md`
- `skills/presales.md`
- `skills/architect.md`
- `skills/market-pr.md`

工作方式：

1. 根据用户问题判断身份：售前、技术方案、市场 PR。
2. 先读 `_index/folder-routing.md`，判断应该进入哪个文件夹。
3. 再读 `skills/common-rules.md`。
4. 再读对应身份 skill。
5. 最后按 skill 要求读取产品目录下的 brief。

注意：`skills/` 只写回答流程，不是产品事实来源。产品事实必须来自产品目录、`shared/`、`0-产品全景/` 或 `原始文档/`。
