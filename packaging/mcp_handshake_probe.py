#!/usr/bin/env python3
"""Drive a real MCP handshake against the MCP server and check what it answers.

This is what an MCP registry does to decide whether a server is healthy: start
it, send `initialize`, then ask for `tools/list`. Running it ourselves — in CI,
against the image built from `Dockerfile.mcp` — is the difference between
learning the image is broken from our own red check and learning it from a
third party's "unhealthy" badge weeks later.

Usage:
    python3 packaging/mcp_handshake_probe.py <docker-image>       # in a container
    python3 packaging/mcp_handshake_probe.py --command nh mcp-serve   # directly

STDOUT IS THE PROTOCOL. The server speaks JSON-RPC over stdin/stdout, so this
probe writes its own diagnostics to stderr and only the verdict to stdout, and
runs `docker run -i` without a TTY. Exit status is the verdict: 0 when both the
handshake and the tool list are what the bridge promises, 1 otherwise.

It calls no tool. `task_add` would file real work; a health check must not.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

EXPECTED_TOOLS = ["task_add", "task_status"]
PROTOCOL = "2025-06-18"


def _send(proc: subprocess.Popen, obj: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen, timeout: float) -> dict | None:
    """First complete JSON line, or None if the server dies or goes quiet."""
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.strip():
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                # A stray non-JSON line on stdout is itself a defect (it would
                # corrupt a client's stream), so report it rather than skip it.
                print(f"non-JSON line on stdout: {line!r}", file=sys.stderr)
                return None
        if proc.poll() is not None:
            return None
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--command":
        cmd = argv[1:]
        if not cmd:
            print("--command needs a command", file=sys.stderr)
            return 2
    else:
        cmd = ["docker", "run", "-i", "--rm", argv[0]]

    print(f"probing: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "no_human-handshake-probe", "version": "1"},
            },
        })
        # Generous: the container has to bring the API up before the bridge
        # starts speaking at all.
        init = _read(proc, timeout=180.0)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_msg = _read(proc, timeout=60.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        assert proc.stderr is not None
        tail = (proc.stderr.read() or "")[-2000:]
        if tail:
            print("--- server stderr (tail) ---", file=sys.stderr)
            print(tail, file=sys.stderr)

    problems: list[str] = []
    if not (init and init.get("result")):
        problems.append(f"no initialize result: {json.dumps(init)[:300] if init else 'no response'}")
    else:
        server = init["result"].get("serverInfo", {})
        print(f"initialize ok: {server.get('name')} {server.get('version')}"
              f" (protocol {init['result'].get('protocolVersion')})")

    names = sorted(t.get("name") for t in (tools_msg or {}).get("result", {}).get("tools", []))
    if names != EXPECTED_TOOLS:
        problems.append(f"tools/list returned {names}, expected {EXPECTED_TOOLS}")
    else:
        print(f"tools/list ok: {names}")

    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print("HANDSHAKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
