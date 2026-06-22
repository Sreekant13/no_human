# syntax=docker/dockerfile:1

# ── Stage 1: build the React board (web/dist) ───────────────────────────────
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# git is required (deterministic VCS); gh/glab enable PR/MR + CI on real hosts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY eval/ ./eval/
# The built SPA, served by the API at /.
COPY --from=web /web/dist ./web/dist

RUN uv sync --frozen --no-dev

# Subscription token + config are mounted at runtime, never baked in:
#   docker run -v ~/.no_human:/root/.no_human -p 8420:8420 no_human
# The image NEVER contains ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.
EXPOSE 8420
ENTRYPOINT ["uv", "run", "nh"]
CMD ["dashboard", "--host", "0.0.0.0", "--no-open"]
