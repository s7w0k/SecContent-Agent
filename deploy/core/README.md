# 主体远程爬虫模式

## 模型与稿件检查配置

主体服务的草稿生成、对话改稿以及稿件内容与宣传话术检查共用根目录 `.env` 中的模型配置：

```ini
DEEPSEEK_API_KEY=sk-your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
```

`backend` 和 `backend-worker` 都通过同一 `.env` 读取这些值。本阶段不新增审核环境变量，不需要规则 YAML、公司敏感配置或独立审核服务；更新代码后需要同时重建这两个主体镜像。

## 远程爬虫配置

在主体服务器 `.env` 中配置远程爬虫地址和与爬虫节点一致的机器 Token：

```ini
MCP_CRAWL_URL=http://192.168.10.52:8101
MCP_CRAWL_API_KEY=replace-with-the-same-random-machine-token
MCP_CRAWL_CONNECT_TIMEOUT=5
MCP_CRAWL_READ_TIMEOUT=300
MCP_CRAWL_MAX_RETRIES=2
MCP_CRAWL_MAX_RESPONSE_MB=20
MCP_CRAWL_VERIFY_TLS=true
```

从仓库根目录启动：

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/core/docker-compose.remote-crawl.yml \
  up -d --build --remove-orphans
```

远程覆盖默认不启动内部 `mcp-crawl`；首次从基础模式切换时，`--remove-orphans` 会清理原有内部爬虫容器。Linux 同机测试可使用 `MCP_CRAWL_URL=http://host.docker.internal:18101`；覆盖文件已经为 API 和 Worker 配置 `host-gateway`。

检查最终配置：

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/core/docker-compose.remote-crawl.yml \
  config
```

远程爬虫不可用时，主体仍应正常启动并提供登录、文章查询、历史草稿和改稿功能；新的海外爬取任务会返回可解释的远程服务错误。
