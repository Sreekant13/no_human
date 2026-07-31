"""§7 item 0z / PR-024 lever 1 — cap the tool output the MODEL sees.

PR-024 measured the shape of the cost: ~99% of every bill is cache-read of a
conversation that only grows, and compaction never fires because the attempt dies on
the SPEND cap long before the CONTEXT fills. So the lever is to stop the conversation
growing in the first place.

WHY TOOL RESULTS, measured rather than assumed
(an offline study, `LEVER1_TOOL_RESULT_DISTRIBUTION.md`, over 224 transcripts and
4,775 real tool results):

    tool-result chars   9,207,127   71.9% of the conversation body
    everything else     3,598,336   28.1%

Standing rule 5 says name the refutation first: the refutation of "capping tool output
cuts cost" is "tool output is a small share of the conversation". It did not refute.

WHY PER TOOL, and not one global number

    Bash   n=4292   median   607   p90  4,172   max 28,500   76.2% of tool chars
    Read   n=282    median 3,714   p90 17,023   max 60,884   23.4%
    everything else — 0.4% combined.

Read's MEDIAN is close to Bash's P90. A single threshold either guts Read (whose large
results are ordinary file contents) or lets Bash through (whose large results are
usually dumps). Each default sits at roughly its own tool's p90, so ~10% of calls are
touched in each tail rather than 26.6% globally, and ~18% of the conversation body goes
away. Because cost is quadratic in conversation length, that compounds across turns —
which is exactly why the realised saving must be MEASURED after this lands and not
projected from the table above.

🖐️ WHAT THIS DOES NOT KNOW. The distribution was measured on THIS machine's agent
transcripts, not on no_human's coder sessions. The per-tool SPLIT is very likely to
hold; the exact percentiles are NOT transferable. `70e8243` made the real telemetry
reachable, so re-derive them from `task_events` once coder runs accumulate — which is
why the thresholds are configuration and not literals in this file.

TRUNCATION IS NOT FREE. A truncated result the model actually needed causes a re-run,
and turns are the cost driver. That is why the replacement text always states that it
truncated, states the original size, and says how to get the rest: the previous
(unreachable) implementation's own comment warned that "silent truncation makes the
model treat a partial as complete", and a model that does not know it was truncated
cannot decide to re-read.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

# Kept generous: the point is to remove the tail, not to summarise. A head+tail slice
# preserves both the command echo and the final lines, which is where the signal
# usually is in a long Bash result.
_HEAD_SHARE = 0.7


def _text_and_rebuild(response: Any) -> tuple[str | None, Any]:
    """`(text, rebuild)` for a tool response, or `(None, None)` to decline it.

    `rebuild(capped_text)` returns a replacement IN THE TOOL'S OWN SHAPE. Both halves
    are needed and returning only the text is what made this module inert.

    🔴 THE SHAPES BELOW WERE CAPTURED, NOT ASSUMED. The previous version returned the
    text alone and declined every `dict`, so it never truncated anything: the
    PostToolUse `tool_response` for both capped tools IS a dict —
        Bash -> {"stdout": …, "stderr": …, "interrupted": …, "isImage": …}
        Read -> {"type": "text", "file": {"filePath": …, "content": …}}
    Measured over 1,165 real payloads from local transcripts: 57 exceeded the shipped
    caps and 0 were truncated. That is the THIRD time in this subsystem that code which
    looked correct never ran because the shape crossing the SDK boundary was assumed —
    after `70e8243` (tool results arrive in a `UserMessage`) and `7077f00` (a guard
    reading a key the SDK never sends). Capture a real payload before editing this.

    🔴 AND THE REPLACEMENT MUST KEEP THE SHAPE. The SDK validates `updatedToolOutput`
    against the tool's own output schema; on a mismatch it KEEPS THE ORIGINAL and
    APPENDS a `hook_error_during_execution` message — so returning a bare string would
    make this cost lever LENGTHEN the conversation. Strictly worse than doing nothing.

    Anything whose shape cannot be rebuilt faithfully is DECLINED rather than guessed
    at, which is the same fail-safe the image-block branch has always used.
    """
    if isinstance(response, str):
        # Some tools do return plain text; identity is a faithful rebuild.
        return response, lambda capped: capped

    if isinstance(response, dict):
        # Bash: stdout + stderr are the two text channels the model sees.
        if "stdout" in response or "stderr" in response:
            text = f"{response.get('stdout') or ''}{response.get('stderr') or ''}"

            def _rebuild_bash(capped: str, _r: dict = response) -> dict:
                # The whole capped payload goes to stdout; stderr is emptied so the
                # combined length the model sees equals what we promised to cap.
                return {**_r, "stdout": capped, "stderr": ""}

            return text, _rebuild_bash

        # Read: the content lives one level down, under "file".
        f = response.get("file")
        if isinstance(f, dict) and "content" in f:
            text = str(f.get("content") or "")

            def _rebuild_read(capped: str, _r: dict = response, _f: dict = f) -> dict:
                nf = {**_f, "content": capped}
                if "numLines" in _f:
                    nf["numLines"] = capped.count("\n") + 1
                return {**_r, "file": nf}

            return text, _rebuild_read

        return None, None

    if isinstance(response, list):
        parts = []
        for block in response:
            if isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    parts.append(str(block.get("text") or ""))
                else:
                    # An image block is large and has no text. Truncating it as if it
                    # were text would corrupt it, so decline the whole response.
                    return None, None
            else:
                parts.append(str(block))
        text = "".join(parts)

        def _rebuild_blocks(capped: str) -> list:
            return [{"type": "text", "text": capped}]

        return text, _rebuild_blocks

    return None, None


def make_tool_result_cap_hook(
    thresholds: dict[str, int],
) -> Callable[..., Awaitable[dict]]:
    """Build a PostToolUse hook that replaces oversized tool output.

    ``thresholds`` maps tool name -> max chars. A tool absent from the map is never
    touched, so this can be rolled out one tool at a time and disabled entirely by
    passing ``{}``.
    """

    async def hook(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        try:
            data = input_data or {}
            # Main thread only. A subagent's transcript is its own conversation and
            # does not drive the parent's growth, so capping there buys nothing and
            # still risks a re-run.
            # 🔴 THE KEY IS `agent_id`, NOT `parent_tool_use_id`. The first version read
            # `parent_tool_use_id`, which the PostToolUse payload NEVER sends — so the
            # guard was DEAD and the cap fired on subagents anyway. The SDK is explicit
            # (`types.py` _SubagentContextMixin): agent_id is "present only when the hook
            # fires from inside a Task-spawned sub-agent; absent on the main thread".
            # My mutation M5 "proved" this guard by hand-building the fictional key, so it
            # verified a fiction — the same defect this module's docstring cites about
            # `_TOOL_RESULT_CAP`: a guard that ships, looks correct, and never runs.
            if data.get("agent_id"):
                return {}
            cap = thresholds.get(data.get("tool_name") or "")
            if not cap or cap <= 0:
                return {}
            text, rebuild = _text_and_rebuild(data.get("tool_response"))
            if text is None or rebuild is None or len(text) <= cap:
                return {}

            head = int(cap * _HEAD_SHARE)
            tail = cap - head
            replaced = (
                f"{text[:head]}\n\n"
                f"[... truncated: this result was {len(text):,} characters; "
                f"{cap:,} characters of it are kept (this notice is extra) to keep the "
                f"conversation short. The middle is omitted. "
                f"Re-run the command with a narrower scope (grep/head/tail, a specific "
                f"path, or an offset) if you need the part that is missing — do NOT "
                f"treat this as the complete output ...]\n\n"
                f"{text[-tail:] if tail > 0 else ''}"
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    # Rebuilt in the tool's own shape — a bare string is rejected by the
                    # SDK's output-schema check, which then keeps the original AND
                    # appends an error message.
                    "updatedToolOutput": rebuild(replaced),
                }
            }
        except Exception:  # noqa: BLE001 — a cost optimisation must never kill a run
            return {}

    return hook
