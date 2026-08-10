# 阶段一基线：版本、回归与旧端点样例

> 工作目录：`pr-agent-demo-v2/`
> 创建时间：2026-08-10
> 状态：已固化

---

## 1. 运行环境版本

| 组件 | 版本 |
|---|---|
| Python | 3.13.0 |
| langchain | 1.3.0 |
| langchain-core | 1.4.0 |
| langchain-openai | 1.2.1 |
| langgraph | 1.2.0 |
| langgraph-checkpoint-mongodb | 0.4.0 |
| pydantic | 2.12.5 |
| pydantic-settings | 2.12.0 |
| fastapi | 0.124.4 |
| motor | 3.7.1 |
| arq | 0.28.0 |
| openai | 2.36.0 |
| httpx | 0.28.1 |
| pytest | 9.0.3 |

## 2. 模型配置

| 配置项 | 值（来自 `.env.example`） |
|---|---|
| Provider | DeepSeek |
| Base URL | `https://api.deepseek.com/v1` |
| Model | `deepseek-chat` |
| API Key 来源 | 环境变量 `DEEPSEEK_API_KEY`（禁止硬编码） |

## 3. 回归测试基线

执行命令：

```powershell
python -m pytest tests/unit/test_draft_chat.py tests/unit/test_api_chat.py tests/unit/test_chat_multitenant.py tests/unit/test_sse_auth.py tests/unit/test_feedback_models.py -q
```

结果（2026-08-10）：

```
collected 80 items

tests\unit\test_draft_chat.py .............................              [ 36%]
tests\unit\test_api_chat.py ....................                         [ 61%]
tests\unit\test_chat_multitenant.py ..                                   [ 63%]
tests\unit\test_sse_auth.py ..                                           [ 66%]
tests\unit\test_feedback_models.py ...........................           [100%]

============================= 80 passed in 1.68s ==============================
```

**结论**：80/80 通过，0 失败，基线可复现。

## 4. 旧端点 JSON/SSE 样例固化

### 4.1 `POST /api/chat/ask`（非流式问答）

**请求**：

```json
{
  "message": "这篇稿子传播角度够强吗？",
  "article_url_hash": "abc123",
  "draft_index": 0,
  "history": []
}
```

**响应**：

```json
{
  "ok": true,
  "data": {
    "answer": "基于当前产品定位和事件影响面...",
    "references": ["article:abc123", "knowledge:智能体身份安全"]
  },
  "trace_id": "trace-xxxx"
}
```

### 4.2 `POST /api/chat/ask_stream`（流式问答 SSE）

**SSE 事件格式**（旧 v0，无 schema_version）：

```
data: {"chunk": "文本片段"}\n\n
data: {"chunk": "更多文本"}\n\n
data: {"done": true, "answer": "完整回答文本"}\n\n
```

**错误事件**：

```
data: {"error": "LLM 调用失败: ..."}\n\n
```

**特殊事件**（偏好学习触发时）：

```
data: {"chunk": "已从当前对话历史中提取偏好..."}\n\n
data: {"done": true, "answer": "已从当前对话历史中提取偏好...", "memory_learning": true}\n\n
```

### 4.3 `POST /api/articles/{url_hash}/drafts/{draft_index}/revise`（改稿）

**请求**：

```json
{
  "instruction": "标题更有冲击力",
  "save": true,
  "selected_text": null,
  "selected_range": null
}
```

**响应**：

```json
{
  "revision_id": "rev-xxxx",
  "revised_content_md": "## 修改摘要\n- 标题改写\n\n## 修订稿\n# 新标题\n\n正文...",
  "change_summary": ["标题改写", "减少技术细节"],
  "saved": true
}
```

## 5. 现有 DraftChatAgent 关键特征

| 特征 | 现状 |
|---|---|
| `answer()` | 单次 `llm.ainvoke([SystemMessage, HumanMessage])`，无工具 |
| `stream_answer()` | 单次 `llm.astream()`，逐 chunk yield str |
| `revise()` | 单次 `llm.ainvoke()`，解析 `## 修改摘要` + `## 修订稿` |
| `stream_revise()` | 单次 `llm.astream()`，逐 chunk yield str |
| 知识注入 | `_get_knowledge_prompt()` 全量加载（非按需） |
| 记忆 | API 层注入 `style_hints`，Agent 内部不主动检索 |
| 工具 | **无**（8 个 @tool 在 `tools.py` 但仅供流水线节点调用） |
| Feature flag | **无**（无开关，直接执行） |

## 6. 工作目录保护约束

- 所有新增、修改、测试、运行和验证均在 `pr-agent-demo-v2/` 内完成
- `pr-agent-demo/` 禁止写入，仅可作历史只读对照
- 脚本和测试中应断言当前工作目录包含 `pr-agent-demo-v2`
