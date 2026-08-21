# no_human plugin for Claude Code

## Install

This repository is its own plugin marketplace, so Claude Code can install the
plugin straight from it:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Nothing is registered by cloning or opening the repo — a marketplace exists for
you only after you add it. The plugin needs no_human installed and running
(`uv tool install no-human`, then `nh start`); the two tools talk to that local
server and nothing else.


Two MCP tools, so you can hand work to no_human without leaving Claude Code:

| Tool | What it does |
|---|---|
| `task_add(title, description, repo_path)` | Files a task with your running no_human server (`POST /api/tasks`, source `mcp`). no_human then plans, codes, tests, has the work reviewed by a second model, and opens the pull request. |
| `task_status(task_id_or_external_id)` | Returns the task's current state as JSON — status, attempts, the PR link once there is one, and the per-task token totals. |

Both talk to the same HTTP API the board uses, at the address the bridge is fixed
to — `http://127.0.0.1:8420` — and nothing else. There is no auth: that address is
localhost, the same trust domain as the board itself.

## Requirements

- no_human installed, with `nh` on your `PATH` (desktop app or source install —
  see the [README](../../README.md#install)).
- The server running: `nh start` (the desktop app starts it for you).

## Develop against a checkout

```bash
claude --plugin-dir ./plugins/no-human
```

The plugin lives in its own directory on purpose: a `.mcp.json` at the
repository root would also be read as the project's own MCP config by anyone who
opens this repo in Claude Code — including no_human's coder sessions working in
this repo — and that is not what this is for.

## The skill

This plugin also ships an agent skill at
[`skills/file-a-task/SKILL.md`](skills/file-a-task/SKILL.md) teaching an
agent when and how to file work into no_human with `task_add` and
`task_status`. As it says there too: no_human opens the pull request and
stops — merge is always the human's action.

## Try it

In Claude Code with the plugin loaded:

> Use `task_add` to file "Rate-limit the login endpoint" against `/path/to/your/repo`, then check it with `task_status`.

The task appears on your board (`nh start` → http://127.0.0.1:8420) and runs
like any other.
