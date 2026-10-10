FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# NOTE: /app/.venv below is created INSIDE this image by `uv sync` — the host
# .venv is never mounted or copied in (see .dockerignore).
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Python dependencies (cached layer; no project package to install)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Node.js lets mcp-server-fetch use Readability.js for much better page
# extraction (pure-Python fallback is low quality and warns on every fetch)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pre-install the MCP servers used by mcp.json so uvx needs no network at startup
RUN uv tool install searxng-http-mcp && uv tool install mcp-server-fetch

COPY scripts/ scripts/
COPY mcp.json netherlands-sponsors.csv ./

# census.csv lives in /app/data — mount it as a volume so progress survives restarts
CMD ["python", "scripts/census.py"]
