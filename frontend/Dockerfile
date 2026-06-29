# Frontend Production Dockerfile — 静态构建产物
# Multi-stage: build → nginx
FROM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

# ── 运行阶段：Nginx 托管静态文件 ──
FROM nginx:1.27-alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# Nginx 配置（API 代理到 backend）
COPY nginx.conf /etc/nginx/conf.d/default.conf

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://localhost:80/ || exit 1

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
