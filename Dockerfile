FROM python:3.11-slim

# Pull uv binary from the official distroless image — no pip needed
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Layer-cache-friendly: install dependencies before copying source
# so a code change doesn't bust the dependency layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/
COPY main.py ./

# MCP communicates over stdio — no network port is exposed
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["uv", "run", "main.py"]
