# API 契约规范 — 阶段一

> **上游**: [基础架构搭建-精细化实施计划](./基础架构搭建-精细化实施计划.md)
> **用途**: 定义阶段一涉及的所有服务间 HTTP 接口契约，作为开发和测试的共同基准。
> **版本**: v1.0 | 2026-06-29

---

## 目录

1. [通用约定](#1-通用约定)
2. [mcp-wewe 服务接口](#2-mcp-wewe-服务接口)
3. [mcp-crawl 服务接口](#3-mcp-crawl-服务接口)
4. [backend 服务接口](#4-backend-服务接口)
5. [错误码规范](#5-错误码规范)

---

## 1. 通用约定

### 1.1 基础信息

| 属性 | 值 |
|------|-----|
| 协议 | HTTP/1.1 |
| 内容类型 | `application/json; charset=utf-8` |
| 字符编码 | UTF-8 |
| 日期格式 | ISO 8601 (`2026-06-29T14:30:00+08:00`) |

### 1.2 通用响应格式

```json
// 成功
{
  "ok": true,
  "data": { ... }
}

// 失败
{
  "ok": false,
  "error": {
    "code": "ARTICLE_NOT_FOUND",
    "message": "文章不存在: md5hash"
  }
}
```

### 1.3 通用 HTTP 状态码

| 状态码 | 语义 |
|--------|------|
| 200 | 成功 |
| 201 | 创建成功 |
| 400 | 请求参数错误 |
| 404 | 资源不存在 |
| 422 | 参数校验失败 |
| 429 | 请求频率超限 |
| 500 | 服务内部错误 |
| 503 | 服务不可用（依赖未就绪） |

### 1.4 分页规范

**请求参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page` | integer | 1 | 页码（从 1 开始） |
| `page_size` | integer | 20 | 每页大小（1-100） |
| `sort_by` | string | `added_at` | 排序字段 |
| `order` | string | `desc` | `asc` / `desc` |

**响应格式**:

```json
{
  "ok": true,
  "data": {
    "items": [...],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total": 150,
      "total_pages": 8
    }
  }
}
```

---

## 2. mcp-wewe 服务接口

> 基础路径: `http://mcp-wewe:8100`（内部网络）
> Swagger: `http://mcp-wewe:8100/docs`

### 2.1 健康检查

```
GET /health

Response 200:
{
  "ok": true,
  "status": "healthy",
  "mcp_connected": true,
  "tools_count": 8
}
```

### 2.2 工具列表

```
GET /tools

Response 200:
{
  "tools": {
    "check_accounts": {
      "description": "检测WeWe RSS所有账号状态",
      "inputSchema": { "type": "object", "properties": {} }
    },
    "fetch_yesterday_articles": {
      "description": "获取昨日公众号文章列表",
      "inputSchema": {
        "type": "object",
        "properties": {
          "rss_url": { "type": "string", "description": "RSS地址，留空使用默认" }
        }
      }
    },
    "fetch_article_fulltext": {
      "description": "抓取单篇文章全文(Markdown)",
      "inputSchema": {
        "type": "object",
        "properties": {
          "link": { "type": "string", "description": "文章链接" }
        },
        "required": ["link"]
      }
    },
    "analyze_article": {
      "description": "AI分析文章(抓全文+DeepSeek摘要)",
      "inputSchema": {
        "type": "object",
        "properties": {
          "link": { "type": "string" },
          "title": { "type": "string", "default": "" }
        },
        "required": ["link"]
      }
    },
    "create_login_qrcode": { ... },
    "poll_login_result": { ... },
    "save_account": { ... },
    "delete_account": { ... }
  }
}
```

### 2.3 通用工具调用

```
POST /call/{tool_name}
Content-Type: application/json

// Request body: 工具参数（JSON object）
{}

// Response 200: 工具返回结果（JSON）
{
  "articles": [...]
}
```

### 2.4 快捷端点

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| `POST` | `/check-accounts` | 检测账号状态 | `{}` |
| `POST` | `/fetch-yesterday` | 获取昨日文章 | `{"rss_url": ""}` (optional) |
| `POST` | `/fetch-article` | 抓取全文 | `{"link": "https://..."}` |
| `POST` | `/analyze-article` | AI 分析 | `{"link": "...", "title": "..."}` |

---

## 3. mcp-crawl 服务接口

> 基础路径: `http://mcp-crawl:8101`（内部网络）
> Swagger: `http://mcp-crawl:8101/docs`

### 3.1 健康检查

```
GET /health

Response 200:
{
  "ok": true,
  "status": "healthy",
  "mcp_connected": true,
  "tools_count": 5
}
```

### 3.2 工具列表

```
GET /tools

Response 200:
{
  "tools": {
    "crawl_news": {
      "description": "爬取海外安全新闻（支持天数参数）",
      "inputSchema": {
        "type": "object",
        "properties": {
          "days": { "type": "integer", "default": 1, "minimum": 1, "maximum": 30 }
        }
      }
    },
    "classify_articles": {
      "description": "AI分类文章（AI安全/Agent安全）",
      "inputSchema": {
        "type": "object",
        "properties": {
          "articles_json": { "type": "string", "description": "JSON序列化的文章数组" }
        },
        "required": ["articles_json"]
      }
    },
    "query_database": {
      "description": "查询已爬取的文章数据库",
      "inputSchema": {
        "type": "object",
        "properties": {
          "category": { "type": "string" },
          "days": { "type": "integer", "default": 7 }
        }
      }
    },
    "get_stats": {
      "description": "获取爬取统计信息",
      "inputSchema": { "type": "object", "properties": {} }
    },
    "export_csv": {
      "description": "导出AI安全文章CSV",
      "inputSchema": {
        "type": "object",
        "properties": {
          "category": { "type": "string", "default": "" }
        }
      }
    }
  }
}
```

### 3.3 快捷端点

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| `POST` | `/crawl-news` | 爬取新闻 | `{"days": 1}` |
| `POST` | `/classify` | AI 分类 | `{"articles_json": "[...]"}` |
| `POST` | `/query` | 查询文章 | `{"category": "AI安全", "days": 7}` |
| `GET` | `/stats` | 获取统计 | — |
| `POST` | `/export-csv` | 导出 CSV | `{"category": ""}` |

### 3.4 crawl_news 响应示例

```json
{
  "ok": true,
  "data": {
    "articles": [
      {
        "title": "Critical MCP Server Vulnerability Exposes Agent Authentication",
        "url": "https://thehackernews.com/2026/06/...",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "summary": "Researchers discovered a critical vulnerability in MCP server implementations...",
        "published_at": "2026-06-28",
        "content_md": "# Critical MCP...\n\nFull article text..."
      }
    ],
    "count": 15,
    "crawled_at": "2026-06-29T09:00:00+08:00"
  }
}
```

### 3.5 classify 响应示例

```json
{
  "ok": true,
  "data": {
    "classified": [
      {
        "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "is_ai_security": true,
        "is_agent_security": true,
        "category": "MCP协议漏洞",
        "summary_cn": "研究人员发现MCP服务器实现中存在严重漏洞，可导致Agent身份验证被绕过。"
      }
    ],
    "classified_at": "2026-06-29T09:05:00+08:00"
  }
}
```

---

## 4. backend 服务接口

> 基础路径: `http://backend:8000` / 外部 `http://localhost:8000`
> Swagger: `http://localhost:8000/docs`

### 4.1 健康检查

```
GET /api/health

Response 200:
{
  "ok": true,
  "status": "healthy",
  "mongodb": "connected",
  "mcp_wewe": "connected",
  "mcp_crawl": "connected"
}
```

### 4.2 文章列表（阶段一：骨架）

```
GET /api/articles?page=1&page_size=20&source_type=overseas_news&min_score=100&sort_by=added_at&order=desc

Response 200:
{
  "ok": true,
  "data": {
    "items": [
      {
        "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
        "title": "文章标题",
        "url": "https://...",
        "source": "The Hacker News",
        "source_type": "overseas_news",
        "published_at": "2026-06-28T10:00:00+08:00",
        "added_at": "2026-06-29T09:00:00+08:00",
        "summary_cn": "中文摘要",
        "is_ai_security": true,
        "is_agent_security": false,
        "category": "MCP协议漏洞",
        "ai_relevance_score": 85,
        "reportability_score": 72,
        "total_score": 157,
        "has_report": false
      }
    ],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total": 150,
      "total_pages": 8
    }
  }
}
```

### 4.3 文章详情（阶段一：骨架）

```
GET /api/articles/{url_hash}

Response 200:
{
  "ok": true,
  "data": {
    "url_hash": "d41d8cd98f00b204e9800998ecf8427e",
    "title": "...",
    "url": "...",
    "content_md": "# markdown content...",
    ...
  }
}

Response 404:
{
  "ok": false,
  "error": {
    "code": "ARTICLE_NOT_FOUND",
    "message": "文章不存在: d41d8cd9..."
  }
}
```

### 4.4 统计概览（阶段一：骨架）

```
GET /api/stats

Response 200:
{
  "ok": true,
  "data": {
    "total_articles": 150,
    "total_reports": 12,
    "by_source_type": {
      "overseas_news": 100,
      "wechat_mp": 40,
      "paper": 10
    },
    "by_category": {
      "MCP协议漏洞": 30,
      "提示注入": 25,
      ...
    },
    "score_distribution": {
      "0-50": 20,
      "51-100": 50,
      "101-150": 45,
      "151-200": 35
    }
  }
}
```

### 4.5 流水线触发（阶段一：骨架，阶段二完整实现）

```
POST /api/pipeline/run
Content-Type: application/json

{
  "crawl_days": 1,
  "phases": ["crawl", "classify", "score", "report"]
}

Response 202:
{
  "ok": true,
  "data": {
    "run_id": "run_20260629_090000",
    "status": "started",
    "phases": ["crawl", "classify", "score", "report"]
  }
}
```

### 4.6 流水线状态

```
GET /api/pipeline/status?run_id=run_20260629_090000

Response 200:
{
  "ok": true,
  "data": {
    "run_id": "run_20260629_090000",
    "status": "running",
    "current_phase": "classify",
    "phases": {
      "crawl": { "status": "completed", "articles_found": 25, "duration_ms": 3200 },
      "classify": { "status": "running", "articles_processed": 10, "articles_total": 25, "duration_ms": null },
      "score": { "status": "pending", "duration_ms": null },
      "report": { "status": "pending", "duration_ms": null }
    },
    "started_at": "2026-06-29T09:00:00+08:00"
  }
}
```

---

## 5. 错误码规范

### 5.1 通用错误码

| 错误码 | HTTP 状态 | 说明 |
|--------|-----------|------|
| `VALIDATION_ERROR` | 422 | 请求参数校验失败 |
| `RESOURCE_NOT_FOUND` | 404 | 通用资源不存在 |
| `ARTICLE_NOT_FOUND` | 404 | 文章不存在 |
| `REPORT_NOT_FOUND` | 404 | 报道不存在 |
| `PIPELINE_ALREADY_RUNNING` | 409 | 流水线已在运行 |
| `PIPELINE_INVALID_PHASE` | 400 | 无效的流水线阶段 |
| `RATE_LIMIT_EXCEEDED` | 429 | 请求频率超限 |
| `MCP_UNAVAILABLE` | 503 | MCP 服务不可用 |
| `MONGODB_UNAVAILABLE` | 503 | 数据库不可用 |
| `LLM_API_ERROR` | 502 | LLM API 调用失败 |
| `INTERNAL_ERROR` | 500 | 内部错误 |

### 5.2 错误响应示例

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "请求参数校验失败",
    "details": [
      {
        "field": "crawl_days",
        "error": "ensure this value is greater than or equal to 1"
      }
    ]
  }
}
```

---

## 附录：服务间依赖关系

```
backend ──HTTP──> mcp-wewe:8100
backend ──HTTP──> mcp-crawl:8101
backend ──TCP───> mongodb:27017
前端(browser) ──HTTP──> backend:8000
```

阶段一中各服务独立开发，依赖通过 Docker DNS 解析。接口契约保证并行开发不阻塞。

---

> **变更记录**:
> - v1.0 (2026-06-29): 初始版本，定义阶段一全部接口契约。
