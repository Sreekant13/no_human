#!/bin/sh
# Start no_human's local API, wait for it, then become the MCP bridge.
#
# The bridge (`nh mcp-serve`) refuses to start when the API is unreachable, and
# it speaks MCP over STDIN/STDOUT — so this script must (a) put the API up
# first, (b) never write anything to stdout itself, or it corrupts the JSON-RPC
# stream a client is reading, and (c) `exec` the bridge so signals and the exit
# status belong to it rather than to a shell wrapper. Every diagnostic here goes
# to stderr for exactly that reason.
#
# WHY `--no-dev` ON EVERY `uv run`. The image is built with
# `uv sync --frozen --no-dev`, so the dev group is absent from both the venv and
# the image's uv cache. `uv run` re-syncs before executing, and its default group
# set INCLUDES dev — so a bare `uv run` here would try to fetch pytest,
# pyinstaller and eleven more wheels from PyPI at container start. An MCP
# registry probing an untrusted image with no egress would get a container that
# never serves: exactly the "unhealthy" verdict this image exists to avoid.
# Measured: 14 packages, none of them in the image.
#
# The port is a literal, not a variable, because `BASE_URL` in
# `intake/mcp_bridge.py` is a module constant — a configurable port here could
# only ever bind the API somewhere the bridge will not look.
set -eu

PORT=8420

# uvicorn on the ASGI app, not `nh start`: the CLI asserts a usable coding
# backend (credential + `claude` CLI) before serving, which a registry sandbox
# cannot satisfy and does not need in order to answer `tools/list`.
uv run --frozen --no-dev uvicorn no_human.api.app:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning >&2 2>&1 &
API_PID=$!

# Wait for the API to answer, and fail loudly if it never does — a bridge
# started against a dead API would exit with a message about the API instead of
# about the real problem, which is this container.
i=0
until curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/api/tasks" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "mcp-entrypoint: API did not come up on 127.0.0.1:${PORT} in 60s" >&2
        kill "$API_PID" 2>/dev/null || true
        exit 1
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "mcp-entrypoint: the API process exited before it served" >&2
        exit 1
    fi
    sleep 1
done

echo "mcp-entrypoint: API up on 127.0.0.1:${PORT}; starting the MCP bridge" >&2
exec uv run --frozen --no-dev nh mcp-serve
