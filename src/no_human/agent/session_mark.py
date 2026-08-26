"""The agent-session MARK: enforcement moves from the verb's spelling to the
process's identity.

`guard.py`'s existing PreToolUse hook is lexical: it inspects the literal
argv/text of a tool call and blocks strings that look like a gate-ending verb
or route. That is necessary but not sufficient — any respelling the lexical
rules do not happen to recognize (a wrapper script, an unusual quoting, a
brand-new alias) slips through untouched, because the hook has no notion of
*what kind of process is asking*, only of what it typed.

This module is the other half. The two coding-backend "env funnels"
(`claude_backend.py`'s `_options()`, `codex_backend.py`'s `_child_env()`) stamp
every subprocess they launch with `mark_env(...)` — never into the parent's
own `os.environ`, only into the child's env mapping, so the mark propagates to
every descendant of an agent session regardless of how that descendant is
invoked. The gate-ending act sites (`cli/commands.py`'s `approve` and
`merge_stack_run`, and `api/app.py`'s gate-ending routes) then ask a single
question — "does the calling process carry the mark?" — via
`refuse_if_marked()` / the HTTP middleware, and refuse before any state
mutates if the answer is yes. The lexical guard and this act-level check are
ADDITIVE, not a replacement for one another: a marked session that manages to
dodge the lexical hook still hits this checkpoint, and a caller that somehow
lacks the mark still runs `guard.py`'s existing checks first (this module
changes none of them).

Fail-closed throughout, per this repo's standing rule: an unrecognized or
malformed mark value counts as MARKED, never as absent. Only the explicit
falsy spellings below count as "no mark".

WHAT THIS IS NOT: a closed door. It is a better-PLACED cost-raising layer —
one choke point at the act instead of a list of spellings on the way to it,
and the only layer that acts before the fact on the Codex backend. Two
residuals, stated here so the module is not read as more than it is: the mark
lives in the environment and an environment variable can be UNSET, so a
session that dodges the lexical guard (e.g. a script it wrote and then ran by
path) and drops the var in the same step is past both layers; and the HTTP
side is honour-system, because the server cannot see a caller's environment —
an unmarked-looking request that simply omits `AGENT_SESSION_HEADER` is not
refused by it. `docs/security.md` carries the long form.
"""

from __future__ import annotations

#: Env var name: presence (with a truthy value) means "this process is an
#: Agent-SDK or Codex coding session". Stamped into subprocess env mappings
#: only — never assigned into this process's own `os.environ`.
NO_HUMAN_AGENT_SESSION = "NO_HUMAN_AGENT_SESSION"

#: Env var name: which backend stamped the mark ("claude" | "codex"). Purely
#: informational — refusal does not depend on the kind, only on presence.
NO_HUMAN_AGENT_SESSION_KIND = "NO_HUMAN_AGENT_SESSION_KIND"

#: HTTP header a marked CLI client (`cli/api_client.py`) sends so a marked
#: session's HTTP calls are recognizable to `api/app.py`'s middleware even
#: when the server process itself is unmarked (the common case: `nh serve`
#: runs as a long-lived operator process, not inside an agent session).
AGENT_SESSION_HEADER = "X-No-Human-Agent-Session"

#: Values that count as "no mark", case-insensitively, after stripping
#: whitespace. Anything else — including unrecognized garbage — is MARKED.
_FALSY = frozenset({"", "0", "false", "none", "no"})


def _looks_marked(value: str | None) -> bool:
    """Fail-closed truthiness for a single mark-carrying value.

    `None` (var/header simply absent) is the one honest "not marked" case.
    Any present-but-unrecognized value refuses rather than passes through.
    """
    if value is None:
        return False
    return value.strip().lower() not in _FALSY


def mark_env(kind: str) -> dict[str, str]:
    """The env entries to merge into a coding-backend subprocess's env
    mapping — never into this process's own `os.environ`. `kind` is
    `"claude"` or `"codex"`, recorded for diagnostics only."""
    return {
        NO_HUMAN_AGENT_SESSION: "1",
        NO_HUMAN_AGENT_SESSION_KIND: kind or "unknown",
    }


def current_mark() -> str | None:
    """The mark kind if THIS process carries it, else `None`.

    Reads `os.environ` at call time (not cached), so tests that
    `monkeypatch.setenv`/`delenv` the two vars see the effect immediately.
    """
    import os

    if not _looks_marked(os.environ.get(NO_HUMAN_AGENT_SESSION)):
        return None
    kind = os.environ.get(NO_HUMAN_AGENT_SESSION_KIND)
    return kind if kind else "unknown"


def request_is_marked(header_value: str | None) -> bool:
    """Whether an inbound HTTP request should be treated as coming from (or
    running inside) a marked agent session — either because the SERVER
    process itself carries the mark, or because the CALLER said so via
    `AGENT_SESSION_HEADER`. Either signal alone is sufficient; fail closed."""
    return current_mark() is not None or _looks_marked(header_value)


class GateRefused(Exception):
    """Raised by `refuse_if_marked()` when a gate-ending act is attempted
    inside a marked agent session. `.act` and `.reason` are both plain
    strings so CLI/API callers can format them without inspecting `args`."""

    def __init__(self, act: str, reason: str | None = None) -> None:
        self.act = act
        self.reason = reason or (
            f"{act!r} refused: this process is a marked agent session; "
            "gate-ending actions are operator-only (see docs/security.md)."
        )
        super().__init__(self.reason)


def refuse_if_marked(act: str) -> None:
    """Raise `GateRefused` if THIS process carries the agent-session mark.

    Call this as the first statement of a gate-ending CLI command, before
    any state-mutating work (including opening the `Store`) happens."""
    mark = current_mark()
    if mark is not None:
        raise GateRefused(
            act,
            reason=(
                f"{act!r} refused: this process is a marked agent session "
                f"(kind={mark!r}); gate-ending actions are operator-only "
                "(see docs/security.md)."
            ),
        )


def mark_headers() -> dict[str, str]:
    """Headers to merge into every outbound `cli/api_client.py` request —
    empty if this process is unmarked, so an ordinary operator CLI sends no
    extra header at all."""
    mark = current_mark()
    if mark is None:
        return {}
    return {AGENT_SESSION_HEADER: mark}
