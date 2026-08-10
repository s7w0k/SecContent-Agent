# Provider 工具调用能力矩阵

> 探针脚本：`scripts/probe_chat_tool_calling.py`
> Provider：DeepSeek（`deepseek-chat`，`https://api.deepseek.com/v1`）
> langchain-openai 版本：1.2.1 / openai 版本：2.36.0
> 运行日期：2026-08-10
> 状态：✅ 已完成

---

## 运行方式

```powershell
cd D:\亚信安全工作\Project\智能体PR流水线\pr-agent-demo-v2
$env:DEEPSEEK_API_KEY = "<有效 API Key>"
python scripts/probe_chat_tool_calling.py
```

> 探针只在非生产环境执行。不记录 API Key 和用户内容。

## 测试项与结果

### 1. `bind_tools().ainvoke()` 与 `AIMessage.tool_calls`

| 项 | 预期 | 实际结果 | 通过 |
|---|---|---|---|
| 绑定 1 个工具后 ainvoke 返回 AIMessage | 返回 `AIMessage` 对象 | `AIMessage` | ✅ |
| `AIMessage.tool_calls` 存在且为 list | `list[dict]` | `len=1` | ✅ |
| tool call 含 `name` 字段 | 与工具名一致 | `search_knowledge` | ✅ |
| tool call 含 `id` 字段 | 非空字符串 | `call_00_YxqhCSMepo7C...` | ✅ |
| tool call 含 `args` 字段 | dict，与入参 schema 一致 | `{'product_name': '智能体身份安全'}` | ✅ |
| tool call 含 `type` 字段 | `tool_call` | `tool_call` | ✅ |

### 2. 同轮多工具

| 项 | 预期 | 实际结果 | 通过 |
|---|---|---|---|
| 模型可在一轮返回多个 tool_calls | `len(tool_calls) >= 2` | `len=2` | ✅ |
| 每个 tool call 有独立 id | id 互不相同 | `True` | ✅ |
| tool names | | `['search_knowledge', 'get_article']` | ✅ |

### 3. `astream()` 的 tool call chunk

| 项 | 预期 | 实际结果 | 通过 |
|---|---|---|---|
| 流式模式可收到 tool call chunk | `AIMessageChunk.tool_call_chunks` 非空 | `len=14` | ✅ |
| chunk 含 name/id/index 字段 | 结构完整 | `{'name': 'search_knowledge', 'id': 'call_00_...', 'index': 0, 'type': 'tool_call_chunk'}` | ✅ |
| 拼接后 tool names 可还原 | name 可还原 | `{'search_knowledge'}` | ✅ |
| **最后 chunk 含完整 tool_calls** | 非流式等价 | **`False`** | ❌ |

> **关键判定**：流式 tool call chunk 可收到，但**最后 chunk 不含完整 tool_calls**。流式拼接不稳定。

### 4. `tool_choice` 参数

| 项 | 预期 | 实际结果 | 通过 |
|---|---|---|---|
| `tool_choice="auto"` | 模型自主决定是否调用工具 | 常识问题 `tool_calls=0`（正确不调） | ✅ |
| `tool_choice="none"` | 模型不调用工具，直接回答 | `tool_calls=0` | ✅ |

### 5. Usage Metadata

| 项 | 预期 | 实际结果 | 通过 |
|---|---|---|---|
| `response.usage_metadata` 存在 | 包含 `input_tokens`/`output_tokens` | `{'input_tokens': 462, 'output_tokens': 63, 'total_tokens': 525, 'input_token_details': {'cache_read': 384}, 'output_token_details': {}}` | ✅ |
| `response.response_metadata` 含 token usage | `token_usage` 或等价字段 | `{'completion_tokens': 63, 'prompt_tokens': 462, 'total_tokens': 525, 'prompt_cache_hit_tokens': 384, 'prompt_cache_miss_tokens': 78}` | ✅ |
| Prompt Cache 支持 | `cache_read` 或 `cached_tokens` | `cache_read: 384`（DeepSeek 原生 prompt cache 命中） | ✅ |

### 6. 异常类型

| 场景 | 预期异常类型 | 实际结果 | 通过 |
|---|---|---|---|
| 超时（timeout=0.001s） | `httpx.TimeoutException` 或等价 | **`openai.APITimeoutError`**（MRO: `APITimeoutError -> APIConnectionError -> APIError -> OpenAIError -> Exception`） | ✅ |
| 429 限流 | `openai.RateLimitError` | _待真实触发_ | ⏳ |
| 401 认证失败 | `openai.AuthenticationError` | 已验证（之前过期 Key 触发） | ✅ |

### 7. 无效 Tool Schema

| 场景 | 预期异常类型 | 实际结果 | 通过 |
|---|---|---|---|
| 无效 tool schema（参数无类型注解） | `ValueError` 或 `pydantic.ValidationError` | **未异常**（provider/langchain 容错，自动推断为 string） | ⚠️ |

> langchain-openai 1.x 对无类型注解的参数自动推断为 `string`，不抛异常。实际使用中所有工具参数必须显式类型注解（由代码规范保证）。

## 结论与决策

### 已确定

- **bind_tools + ainvoke**：✅ 稳定。tool_calls 格式完整（name/id/args/type）。
- **同轮多工具**：✅ 支持。一次可返回多个 tool_calls，id 唯一。
- **流式 tool call**：⚠️ chunk 可收到但不稳定。最后 chunk **不含**完整 tool_calls。
- **tool_choice**：✅ auto/none 均正常。
- **usage_metadata**：✅ 可直接读取。含 `input_tokens`/`output_tokens`/`total_tokens` + `cache_read`（prompt cache 命中信息）。
- **Prompt Cache**：✅ DeepSeek 原生支持 prompt caching，`prompt_cache_hit_tokens`/`prompt_cache_miss_tokens` 可读。
- **超时异常**：`openai.APITimeoutError`（继承 `APIConnectionError -> APIError`）。

### 1A 策略（最终决定）

| 策略 | 决定 | 原因 |
|---|---|---|
| **决策轮** | 非流式 `ainvoke` | 流式 tool call chunk 最后 chunk 不含完整 tool_calls，拼接不可靠 |
| **输出轮** | 流式 `astream` 输出 `text_delta` | 兼容旧 SSE `chunk` 字段，保持前端体验 |
| **token 计数** | 直接读 `usage_metadata` | provider 原生返回，无需估算 |
| **prompt cache** | 标记稳定前缀 | DeepSeek 原生支持，`cache_read` 可量化命中 |
| **retry 异常** | catch `APITimeoutError, APIConnectionError, RateLimitError` | 不重试 `AuthenticationError, BadRequestError` |
| **并行工具** | 支持 `asyncio.gather` | 同轮多 tool_calls 已验证 |

### retry.py 设计约束

```python
# 可重试异常
RETRYABLE_EXCEPTIONS = (
    openai.APITimeoutError,       # 超时
    openai.APIConnectionError,    # 连接错误（APITimeoutError 的基类）
    openai.RateLimitError,        # 429 限流
    openai.InternalServerError,   # 5xx
)

# 不可重试异常
NON_RETRYABLE_EXCEPTIONS = (
    openai.AuthenticationError,   # 401
    openai.BadRequestError,       # 400（含 schema 错误）
    openai.PermissionDeniedError, # 403
    openai.NotFoundError,         # 404
)
```
