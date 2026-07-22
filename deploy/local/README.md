# 同机双 Project 验证

该目录用于在一台机器上模拟“主体服务器 + 海外新闻爬虫服务器”。两个 Compose Project 使用独立容器、网络和生命周期，主体通过宿主机端口 `18101` 访问爬虫。

## 1. 准备配置

```powershell
Copy-Item deploy/local/.env.crawler-local.example deploy/local/.env.crawler-local
Copy-Item deploy/local/.env.core-local.example deploy/local/.env.core-local
```

将两个文件中的 `MCP_CRAWL_API_KEY` 改为同一个、至少 32 字节的随机 Token。项目根目录 `.env` 仍负责 MongoDB、Redis、JWT、模型等主体配置。稿件生成、改稿和内容与宣传话术检查共用 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`，不需要新增审核变量；这些变量只属于项目主体，独立爬虫无需配置。

## 2. 启动独立爬虫

```powershell
docker compose -p pr-crawler `
  --env-file deploy/local/.env.crawler-local `
  -f deploy/crawler/docker-compose.yml `
  up -d
```

## 3. 启动主体

```powershell
docker compose -p pr-core `
  --env-file .env `
  --env-file deploy/local/.env.core-local `
  -f docker-compose.yml `
  -f deploy/core/docker-compose.remote-crawl.yml `
  -f deploy/local/docker-compose.core-local.yml `
  up -d
```

不要添加 `--profile embedded-crawl`，否则主体会同时启动内置爬虫。主体页面地址为 `http://127.0.0.1:18000`，主体 MongoDB 的 Compass 地址为 `mongodb://127.0.0.1:37017`。

## 4. 独立启停

```powershell
docker compose -p pr-crawler -f deploy/crawler/docker-compose.yml restart
docker compose -p pr-core -f docker-compose.yml -f deploy/core/docker-compose.remote-crawl.yml -f deploy/local/docker-compose.core-local.yml restart backend backend-worker
```

执行命令时需继续传入相同的 `--env-file` 参数。停止爬虫后，主体页面和非爬虫 API 应继续可用；恢复爬虫后，新建海外新闻任务应恢复执行。
