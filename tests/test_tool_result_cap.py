"""§7 item 0z / PR-024 lever 1: stop the conversation growing.

Measured foundation (an offline study of 224 transcripts / 4,775 tool results,
`LEVER1_TOOL_RESULT_DISTRIBUTION.md`): tool results are **71.9% of the conversation
body** — the payload re-read every turn, which PR-024 measured as ~99% of the bill.
Bash and Read are 99.6% of that text and their distributions differ by ~6x at the
median, so the thresholds are PER TOOL, each at roughly its own p90.
"""

import asyncio

import pytest

from no_human.agent.tool_result_cap import make_tool_result_cap_hook


def _run(hook, tool, output, **kw):
    # 🖐️ `asyncio.get_event_loop().run_until_complete(...)` here made these tests
    # ORDER-DEPENDENT: green alone, and 8 failures under the full suite with
    # `RuntimeError: There is no current event loop in thread 'MainThread'` once another
    # test had closed the loop. A test that only passes when run alone is a test that
    # will be "fixed" by deselecting it. `asyncio.run` owns its own loop.
    return asyncio.run(hook({"tool_name": tool, "tool_response": output, **kw}, None, None))


def test_an_oversized_bash_result_is_replaced_and_says_so():
    hook = make_tool_result_cap_hook({"Bash": 4000})
    out = _run(hook, "Bash", "x" * 10_000)
    replaced = out["hookSpecificOutput"]["updatedToolOutput"]
    assert len(replaced) < 10_000
    # Silent truncation makes the model treat a partial as complete — the defect the
    # dead `_TOOL_RESULT_CAP` comment already warned about. It must be TOLD.
    assert "truncated" in replaced.lower()
    assert "10,000" in replaced or "10000" in replaced, "must state the original size"


def test_a_result_under_its_threshold_is_left_completely_alone():
    """No `updatedToolOutput` at all — not an identical copy. Returning a copy would
    make every small result a rewrite, and would hide a broken threshold behind a
    no-op that looks like it ran."""
    hook = make_tool_result_cap_hook({"Bash": 4000})
    assert _run(hook, "Bash", "x" * 100) == {}


def test_thresholds_are_PER_TOOL_because_the_data_says_they_must_be():
    """Read's median (3,714) is close to Bash's p90 (4,172). A global cap either
    guts Read or lets Bash through; the measurement is what makes this per-tool."""
    hook = make_tool_result_cap_hook({"Bash": 4000, "Read": 16000})
    assert _run(hook, "Read", "x" * 10_000) == {}, "10k Read is ordinary, leave it"
    assert _run(hook, "Bash", "x" * 10_000) != {}, "10k Bash is a dump, cap it"


def test_a_tool_with_no_configured_threshold_is_never_touched():
    hook = make_tool_result_cap_hook({"Bash": 4000})
    assert _run(hook, "Write", "x" * 100_000) == {}


def test_subagent_results_are_left_alone_main_thread_only():
    """A subagent's transcript is not the main conversation and does not drive its
    growth; capping there costs a re-run for no saving."""
    hook = make_tool_result_cap_hook({"Bash": 4000})
    assert _run(hook, "Bash", "x" * 10_000, agent_id="agent_7") == {}
    # NEGATIVE CONTROL: the fictional key must NOT suppress. If this ever returns {},
    # someone has re-added a guard on a field the payload does not carry.
    assert _run(hook, "Bash", "x" * 10_000, parent_tool_use_id="toolu_x") != {}
    assert _run(hook, "Bash", "x" * 10_000) != {}   # main thread: still capped


def test_the_hook_never_breaks_the_session():
    """A cost optimisation must not be able to kill a run. Every prior hook in this
    backend swallows its own errors for the same reason."""
    hook = make_tool_result_cap_hook({"Bash": 4000})
    assert _run(hook, "Bash", None) == {}
    # 🔴 `{"unexpected": "shape"}` is an UNKNOWN dict, not the real Bash shape. The
    # earlier version of this assertion used a dict as a stand-in for "anything odd",
    # which quietly pinned EVERY dict — including the real Bash payload
    # {"stdout", "stderr", "interrupted", "isImage"} — as a permanent no-op, and so
    # locked in the defect that made this module inert. An unrecognised shape must
    # still be declined; a recognised one must not be.
    assert _run(hook, "Bash", {"totally": "unrecognised"}) == {}
    assert _run(hook, "Bash", {"stdout": "small", "stderr": ""}) == {}, (
        "a REAL Bash payload under the cap must be untouched"
    )


@pytest.mark.real_backend  # the hermetic stub replaces ClaudeBackend; this needs the REAL one
def test_the_hook_is_ACTUALLY_INSTALLED_in_the_backends_PostToolUse_chain():
    """🖐️ The wiring test the 0a branch did not have. There, an independent review
    severed THREE of four links with the full suite green, because every test called
    the helpers directly. A cap that is never installed is `_TOOL_RESULT_CAP` all over
    again — a constant that shipped, looked correct, and never executed once across
    19,440 tool_use events.

    This drives the real `_options()` and asserts the hook object reaches the SDK's
    PostToolUse chain, then proves it is the one that truncates.
    """
    import asyncio
    from pathlib import Path
    from no_human.agent.claude_backend import ClaudeBackend

    backend = ClaudeBackend(model="claude-sonnet-5")
    opts = backend._options(Path("/tmp"), max_turns=1)
    matchers = opts.hooks.get("PostToolUse") or []
    callbacks = [cb for m in matchers for cb in m.hooks]
    assert callbacks, "PostToolUse chain is empty — the cap hook was never installed"

    async def _first_truncating(cbs):
        for cb in cbs:
            out = await cb(
                {"tool_name": "Bash", "tool_response": "x" * 50_000}, None, None
            )
            if (out or {}).get("hookSpecificOutput", {}).get("updatedToolOutput"):
                return True
        return False

    assert asyncio.run(_first_truncating(callbacks)), (
        "no installed PostToolUse hook truncates an oversized Bash result — the cap is "
        "present in the module but dead in the product"
    )


@pytest.mark.real_backend
def test_the_caps_can_be_turned_OFF_explicitly():
    """`{}` must disable it. `None` must NOT — that is the defaults path, and a cost
    lever that silently defaults to off is the defect this replaces."""
    from pathlib import Path
    from no_human.agent.claude_backend import ClaudeBackend

    off = ClaudeBackend(model="claude-sonnet-5", tool_result_caps={})
    assert off.tool_result_caps == {}
    on = ClaudeBackend(model="claude-sonnet-5")
    assert on.tool_result_caps, "None must mean the measured defaults, never off"
    opts = off._options(Path("/tmp"), max_turns=1)
    cbs = [cb for m in (opts.hooks.get("PostToolUse") or []) for cb in m.hooks]
    assert not cbs, "with caps off and no other hooks, nothing should be installed"


def test_the_SHIPPED_DEFAULTS_are_per_tool_and_match_the_measurement():
    """🖐️ Found by my own mutation battery: `test_thresholds_are_PER_TOOL…` builds its
    OWN threshold dict, so changing the SHIPPED defaults to a single global number left
    all 8 tests green. The defaults were unpinned — the helper was tested, the artifact
    was not, for the fourth time in this session.

    Pins what actually ships, against the measured distribution
    (`LEVER1_TOOL_RESULT_DISTRIBUTION.md`): Bash median 607 / p90 4,172;
    Read median 3,714 / p90 17,023.
    """
    from no_human.agent.claude_backend import _TOOL_RESULT_CAPS

    assert set(_TOOL_RESULT_CAPS) == {"Bash", "Read"}, (
        "Bash and Read are 99.6% of all tool-result text; capping others buys ~0.4% "
        "and risks a re-run for nothing"
    )
    assert _TOOL_RESULT_CAPS["Read"] > _TOOL_RESULT_CAPS["Bash"], (
        "Read's MEDIAN (3,714) is close to Bash's P90 (4,172) — a Read cap at or below "
        "Bash's would gut ordinary file reads. This is the whole reason for per-tool."
    )
    # Each near its own tool's p90: touch ~10% of that tool's calls, not 26.6% globally.
    assert _TOOL_RESULT_CAPS["Bash"] >= 4000
    assert _TOOL_RESULT_CAPS["Read"] >= 16000


@pytest.mark.real_backend
def test_readonly_backends_are_exempt_from_the_cap_by_default():
    """The cap shrinks what the CODER accumulates; a readonly backend only reads.

    The reviewer is the case that matters: it is told it MAY read any file and is
    REQUIRED to cite file:line evidence. At Read's measured p90 (~17k) a 16,000-char
    cap truncates large files mid-read, so the gate deciding every merge would cite
    line numbers from the head of a file it only partly saw — with nothing marking
    the review degraded. An audit found the cap applied to reviewer, planner,
    distiller, supervisor and aggregator with no exemption and no test.

    Gated on `readonly`, NOT on a list of construction sites: enumerated lists have
    missed three separate surfaces in this codebase in a single day, and a list
    cannot cover a readonly backend added tomorrow.
    """
    from no_human.agent.claude_backend import ClaudeBackend, _TOOL_RESULT_CAPS

    coder = ClaudeBackend(model="claude-sonnet-5")
    assert coder.tool_result_caps == _TOOL_RESULT_CAPS, (
        "the coder is the whole point of the cap and must keep the measured defaults"
    )

    reviewer = ClaudeBackend(model="claude-opus-5", readonly=True)
    assert reviewer.tool_result_caps == {}, (
        "a readonly backend must not have its file reads truncated — the reviewer "
        "cites file:line and would cite from a file it only partly saw"
    )

    # An explicit caller value still wins in BOTH directions, so the exemption is a
    # default and not a policy the caller cannot override.
    assert ClaudeBackend(model="m", readonly=True,
                         tool_result_caps={"Read": 10}).tool_result_caps == {"Read": 10}
    assert ClaudeBackend(model="m", readonly=False,
                         tool_result_caps={}).tool_result_caps == {}

    # 🔴 THE ATTRIBUTE IS NOT THE ARTIFACT. A reviewer's mutation made `_options`
    # install the cap hook whenever `readonly` was set — leaving every assertion above
    # green while a readonly backend truncated a 50k Read at runtime. Assert the
    # INSTALLED HOOK CHAIN, the same way the wiring test above does, or the exemption
    # can be completely dead in production with this file passing.
    from pathlib import Path as _P
    opts = reviewer._options(_P("/tmp"), max_turns=1)
    installed = [cb for m in (opts.hooks.get("PostToolUse") or []) for cb in m.hooks]
    for cb in installed:
        out = asyncio.run(cb({"tool_name": "Read",
                              "tool_response": {"type": "text",
                                                "file": {"filePath": "/x", "content": "Y" * 50_000}}},
                             None, None))
        assert out == {}, (
            "a readonly backend must have NO hook that truncates a 50k Read — the "
            "exemption is a runtime property, not a constructor attribute"
        )


# --------------------------------------------------------------------------- #
# The shapes the SDK ACTUALLY sends. Everything above this line historically fed
# `tool_response` as a `str`, which the SDK never sends for Bash or Read — so the
# whole file proved the cap worked against a fiction while 0 of 1,165 real payloads
# were truncated. These two are the captured shapes.
# --------------------------------------------------------------------------- #

REAL_BASH = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False}
REAL_READ = {"type": "text", "file": {"filePath": "/x.py", "content": "", "numLines": 1}}


def test_a_REAL_bash_payload_is_truncated_and_keeps_its_shape():
    """The defect this file exists to prevent: `_as_text` declined every dict, so the
    hook never fired in production while every test here passed on a `str`."""
    hook = make_tool_result_cap_hook({"Bash": 4000})
    payload = {**REAL_BASH, "stdout": "X" * 50_000, "stderr": "boom"}
    out = _run(hook, "Bash", payload)
    assert out, "a 50k REAL Bash payload must be truncated — it was not, for the module's whole life"
    up = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(up, dict), (
        "a bare str is rejected by the SDK's output-schema check, which keeps the "
        "original AND appends hook_error_during_execution — the lever would LENGTHEN "
        "the conversation"
    )
    assert sorted(up) == sorted(payload), "the tool's own shape must survive"
    assert len(up["stdout"]) < 50_000 and "truncated" in up["stdout"]


def test_a_REAL_read_payload_is_truncated_and_keeps_its_nested_shape():
    hook = make_tool_result_cap_hook({"Read": 16000})
    payload = {"type": "text",
               "file": {**REAL_READ["file"], "content": "Y" * 50_000}}
    out = _run(hook, "Read", payload)
    assert out, "a 50k REAL Read payload must be truncated"
    up = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(up, dict) and sorted(up) == ["file", "type"]
    assert up["file"]["filePath"] == "/x.py", "sibling fields must be preserved"
    assert len(up["file"]["content"]) < 50_000
    assert "truncated" in up["file"]["content"]
