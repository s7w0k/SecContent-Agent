# MCP-WeWe Service Dockerfile
# Multi-stage build — 减小最终镜像体积
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── 运行阶段 ──
FROM python:3.12-slim

WORKDIR /app

# 从 builder 复制已安装的包
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# 复制应用代码
COPY . .

# 安全加固：非 root 用户运行
RUN groupadd -r mcpuser && useradd -r -g mcpuser mcpuser \
    && chown -R mcpuser:mcpuser /app
USER mcpuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8100/health').raise_for_status()"

EXPOSE 8100
CMD ["python", "http_mcp_bridge.py", "--port", "8100", "--host", "0.0.0.0"]
