# MCP-WeWe Service Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd -r mcpuser && useradd -r -g mcpuser mcpuser \
    && chown -R mcpuser:mcpuser /app
USER mcpuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8100/health').raise_for_status()"

EXPOSE 8100
CMD ["python", "http_mcp_bridge.py", "--port", "8100", "--host", "0.0.0.0"]
