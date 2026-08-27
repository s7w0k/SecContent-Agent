# AGENTS.md — 持久化记忆 / Persistent Memory

> 本文件用于把跨会话容易丢失的项目约定永久化保存，供任何 Agent / Claude 后续读取。

## 核心记忆：谁说「仓库」，指的永远是 GitHub

**重要：用户说「仓库 / the repo」默认指 GitHub 仓库，不是 Gitee。**

本项目配置了两个远程（remote）：

| remote   | URL                                          | 角色                              |
|----------|----------------------------------------------|-----------------------------------|
| `github` | https://github.com/s7w0k/SecContent-Agent.git | **仓库（GitHub）—— 推送目标**    |
| `origin` | https://gitee.com/s7w0k/pr-agent-demo-v2.git  | Gitee 镜像（次要，勿默认推送）    |

### 规则
1. 当用户说「提交并推送 / 推送到仓库」时 → 默认 `git push github main`。
2. 除非用户明确指定 Gitee，否则不要只推 `origin`。
3. 每次 commit 后应把 main 推到 `github`。
4. 分支 `main` 已跟踪 `github/main`；`git pull` / `git branch -vv` 时也以 github 为准。

### 历史教训（勿重蹈覆辙）
- 曾因误用 `git push origin main` 推到 Gitee 而没推 GitHub，导致用户不满。
- 以后再收到「仓库」类指令，先确认走 `github` remote。

---

## 项目机密（quick reference，详细见 CLAUDE.md）

- 智能体安全 PR 情报 Agent 系统 Demo：爬取 → 入库 → LLM Agent 分析 → PR 报道 → 可视化。
- 部署：Docker Compose；技术栈：Python 3.12 / FastAPI / LangChain / MongoDB / React+AntD / DeepSeek。
- 提交格式：Conventional Commits；提交后必须 push。
- 测试：`pytest` 必须 0 FAILED；`ruff check services/ tests/` 必须通过。
- Two Hard Gates（more@ `docs/`，git-ignored）：
  - GOAL A：WikiNavigator 为 Live Evidence-driven Navigation。
  - GOAL B：默认 KNOWLEDGE_BACKEND=wiki，CI 门禁 `scripts/check_wiki_default.py`。