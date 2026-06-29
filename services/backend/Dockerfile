# Backend Service Dockerfile
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
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/static /app/docs \
    && chown -R appuser:appuser /app
USER appuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health').raise_for_status()"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
