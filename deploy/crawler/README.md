# 海外新闻爬虫独立部署包

该目录只部署 `mcp-crawl`，不需要主体项目的 `.env`、MongoDB、Redis、JWT 或 DeepSeek Key。爬取与全文抓取可在未配置 DeepSeek 的情况下工作。

## 首次配置

```bash
cd deploy/crawler
cp .env.crawler.example .env.crawler
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

将生成值写入 `.env.crawler` 的 `MCP_CRAWL_API_KEY`。局域网部署还需将 `MCP_CRAWL_BIND_HOST` 改为 `0.0.0.0`，并在宿主机防火墙中只放行主体服务器 IP。

## 镜像运行

从镜像仓库拉取并启动：

```bash
./manage.sh upgrade
```

Windows PowerShell：

```powershell
.\manage.ps1 upgrade
```

若镜像通过离线文件交付，先执行 `docker load -i pr-agent-mcp-crawl.tar`，再运行 `up`。

## 源码构建

从仓库根目录保留 `services/mcp_crawl` 源码时：

```bash
./deploy/crawler/manage.sh build
./deploy/crawler/manage.sh up
```

`build` 优先复用本机基础镜像，适用于离线或镜像加速不稳定环境；需要更新 Python 基础镜像时先单独执行 `docker pull python:3.12-slim`。

## 升级与回滚

升级前在 `.env.crawler` 中设置新的不可变 `MCP_CRAWL_IMAGE_TAG`，然后运行 `upgrade`。回滚时指定旧版本：

```bash
./manage.sh rollback 2026.07.15-1
```

确认回滚后，应同步把 `.env.crawler` 中的版本改为该旧版本，避免下次 `up` 恢复到原版本。

常用命令：

```bash
./manage.sh status
./manage.sh logs
./manage.sh down
```

健康检查：`curl http://127.0.0.1:${MCP_CRAWL_PORT:-8101}/health`。业务接口必须携带 `Authorization: Bearer <MCP_CRAWL_API_KEY>`。
