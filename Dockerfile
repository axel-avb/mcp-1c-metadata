# 1C Configuration MCP server
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY src ./src
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Data dir (SQLite graph + optional Qdrant local mode)
RUN mkdir -p /data

EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
