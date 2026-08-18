# Backend Service Dockerfile
# Multi-stage: build frontend → include in backend image
FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm config set registry https://registry.npmmirror.com \
    && npm ci --cache /tmp/.npm --prefer-offline 2>&1 || npm install --cache /tmp/.npm
COPY frontend/ ./
RUN npx vite build

FROM python:3.12-slim
WORKDIR /app

COPY services/backend/requirements.txt .
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir -r requirements.txt

COPY services/backend/ .
COPY scripts/migrate_local_user.py ./scripts/migrate_local_user.py
COPY scripts/set_developer.py ./scripts/set_developer.py
COPY --from=frontend-builder /frontend/dist ./static

RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/health').raise_for_status()"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
