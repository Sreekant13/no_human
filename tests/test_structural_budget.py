"""A structural-size ratchet over `src/no_human`, in the repo's own guard idiom.

WHY THIS EXISTS. `src/no_human` is 106k LOC and growing; nothing looks at
function size, complexity, or file size, so a function can silently become
2,000+ lines / cc ~250 with no signal anywhere. This is not a lint config and
does not refactor anything — it freezes TODAY's offenders by name and value
and fails on any of three things: (1) a NEW offender that appears outside the
freeze, (2) an EXISTING frozen entry that has grown, (3) a frozen entry that
has shrunk below threshold or whose symbol vanished (renamed/deleted) without
its allow-list entry being deleted. Case (3) is the ratchet: the budget can
only move down.

THE FORMULA, as code-level truth over `ast.walk(fn)` for a function node:

    cc = 1
        + 1 for each If | For | AsyncFor | While | ExceptHandler | With
              | IfExp | match_case
        + (len(values) - 1) for each BoolOp
        + len(ifs) for each comprehension

`AsyncWith` is deliberately **not** counted — an intake-resolved asymmetry,
pinned by `test_cc_formula_on_a_hand_counted_snippet` below so a future edit
to this formula fails loudly instead of silently deflating every frozen
value. This is arbitrary-but-stable and NOT radon-comparable; its only job
is monotonicity. Nested function bodies count toward the enclosing function
too (`ast.walk` descends into them), so growth anywhere inside a function —
including a closure defined inside it — fails that function's budget. This
is intentional, not a bug.

WHAT THIS DOES NOT COVER. A size ratchet, not a design review: splitting a
2,000-line function into ten 200-line ones sharing mutable state passes
clean. `lambda` bodies are uncounted. Module-level code (outside any
function) is invisible except through the whole-file line-count rule.

SCOPE — a deliberate deviation from an intake answer, recorded so it reads as
a choice, not an oversight. Intake said to exclude `test_*.py` under `src/`.
The only match is `src/no_human/testing/test_layers.py`, which is production
code (the tamper guard's own layer classifier) despite its filename.
Excluding it would be exactly the "exclude a path to hide an offender" move
this ticket exists to forbid. It is not an offender today, so scanning it
changes nothing measurable, and the rule stays simple: every `.py` under
`src/no_human`, no exclusions.

`ast.parse` errors are never swallowed: a `SyntaxError` propagates with its
`filename` attribute set to the offending path (via the `filename=` kwarg to
`ast.parse`), rather than being caught and skipped. A file this scanner
cannot parse must fail loudly, not vanish from coverage.
"""

from __future__ import annotations

import ast
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "no_human"

MAX_FUNCTION_LINES = 300
MAX_FUNCTION_CC = 60
MAX_FILE_LINES = 2500

# Frozen at HEAD 2b2370f582f95465ba408c12224a511c6c74f692, 2026-08-26.
# Measured with the scanner below; see the PR body for the full table.
# 16 functions > 300 lines.
FROZEN_FUNCTION_LINES = {
    # 2099 -> 2108 (+9): PR #877 widens the tamper base to three-dot
    # origin/<base>...HEAD so a sanctioned merge isn't charged with main's own
    # landed test edits (attempt-authored gutting still fires). Re-anchored on merge.
    "core/orchestrator.py:Orchestrator._run_attempt": 2108,
    "core/orchestrator.py:Orchestrator._drive": 760,
    "core/db.py:Store._ensure_task_columns": 448,
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 429,
    "core/orchestrator.py:Orchestrator._finalize": 418,
    # Pre-existing on main (measured red at d3d7d3a82a, this session's start):
    # an earlier fleet land grew stream() +6 without re-freezing it on its
    # merge result — the same "landed without measuring the ratchet" failure
    # the FROZEN_FILE_LINES note below records. Re-anchored to the current
    # baseline so the full suite is green again; growth from here fails.
    "agent/claude_backend.py:ClaudeBackend.stream": 407,
    "core/orchestrator.py:Orchestrator._run_review": 386,
    "cli/commands.py:bench_run": 377,
    # Grew to 371 when the UI-evidence prompt block landed (task 389210fa):
    # an inline enable+glob gate + the ui_evidence_block call. Reviewed on
    # its merits (the block is inert until a profile opts in); frozen here.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 371,
    "eval/northstar_card.py:render_northstar_md": 332,
    "core/orchestrator.py:Orchestrator._reformat_summary_markdown": 327,
    "core/orchestrator.py:Orchestrator._generate_plan": 322,
    "core/orchestrator.py:Orchestrator._scan_leaf_blocks": 319,
    "core/orchestrator.py:Orchestrator._escalate_reviewer_unavailable": 317,
    # Grew to 314 (> 300) when the done_no_evidence repair shape landed
    # (task bf413cc6): two new refusal guards + the DONE branch. The growth
    # was reviewed on its merits; frozen here as its landing baseline.
    "blockers/landed_override.py:approve_landed_override": 315,
    "core/metrics.py:compute_metrics": 334,  # +21: PR #869 cost_usd_total server-side pricing
}

# 5 functions with estimated cyclomatic complexity > 60.
FROZEN_FUNCTION_CC = {
    "core/orchestrator.py:Orchestrator._run_attempt": 250,
    "core/orchestrator.py:Orchestrator._drive": 115,
    "agent/guard.py:_approve_denial": 81,
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 73,
    "core/orchestrator.py:Orchestrator._run_review": 73,
    # Crossed 60 (to 67) with the UI-evidence gate landed by task 389210fa.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 67,
}

# 9 files > 2,500 lines.
#
# RE-ANCHORED 2026-08-26: the ratchet landed as b30a292da at 19:51:28 and
# three more branches landed in the same batch — b516f9da7 19:51:40 (grew
# core/scheduler.py 2499 -> 2522, crossing the 2,500 threshold), 1bc8e1fc8
# 19:52:45 (grew core/db.py 4131 -> 4149 and core/orchestrator.py
# 19222 -> 19300) — none of them measured against the ratchet on its merge
# result, so every full suite on main failed these two tests from the moment
# the batch finished. The values below are the first baseline measured on a
# tree that actually contains the ratchet; growth from here fails again.
# RE-ANCHORED 2026-08-27 (supervising session, on main 866cc50c3e): as with
# the 2026-08-26 batch above, three file entries had drifted red on main
# WITHOUT being re-frozen on a merge result — core/db.py 4149 -> 4251 and
# config.py 2888 -> 2889 are pre-existing fleet growth this session never
# touched; api/app.py 5032 -> 5153 was already over (5141 at this session's
# start, d3d7d3a82a) plus +12 from the bf413cc6 landed-override repair. The
# full suite failed test_no_frozen_entry_has_grown from before this session
# began; re-anchored to the measured baseline so the ratchet gates growth
# from here again. (A red ratchet the fleet lands through protects nothing.)
FROZEN_FILE_LINES = {
    # 19354 -> 19387 (+33) with the UI-evidence gate landed by task 389210fa.
    # 19599 -> 19630 (+31): three `WorktreeCheckFailed` catch-site audit
    # comments in `_run_reviewer` (task reviewer-worktree-returncode-audit) —
    # comments only, no behaviour change.
    # 19630 -> 19650 (+20): PR #929's fail-closed .git/common/config allowlist
    # (a benign shared-config reserialization no longer discards the reviewer
    # verdict; execution-surface keys still discard). Re-anchored on merge.
    # 19650 -> 19659 (+9): PR #877's tamper-base widening in _run_attempt (same
    # +9 lines as the function bump above). Re-anchored on merge.
    # 19659 -> 19724 (+65): PR #867 threads a backend kwarg through the budget/
    # pricing sites (output_extra_weight/weighted_tokens/class_breakdown callers).
    "core/orchestrator.py": 19724,
    # +163: Codex account section in the Settings Account tab —
    # _codex_status_payload + endpoints (app.py) and the I4 AI-history repo
    # scoping filter in _gather_history.
    # cli/commands.py 7801 -> 8111: +59 the `_print_learning_harvest` helper
    # shared by `nh learnings --harvest` and the scheduled `HarvestJob` plus
    # the `--no-harvest` serve flag; +251 `nh approve --ready [--yes]` (batch
    # listing/landing) and the `approve` refactor into named top-level helpers
    # (_approve_find_ready / _approve_go_ready / _approve_go_landed /
    # _approve_go_single / _ready_batch_non_merge_message). Measured via
    # `len(Path(...).read_text().splitlines())` (the scanner's own metric) on
    # the combined tree.
    # cli/commands.py 8111 -> 8122 (+11): the reject/reply-dispatch budget-
    # floor warning (`check_budget_floor`) — a pre-dispatch advisory so a
    # reject/reply that would burn the rest of a task's lifetime budget and
    # die mid-attempt on BUDGET_EXHAUSTED warns instead of silently losing
    # the human's feedback. Each of `reject`/`reply` gets its own 2-import +
    # 4-line block, matching this file's existing per-command local-import
    # convention; a shared helper was evaluated and rejected — with only two
    # call sites, the helper's own def+docstring line cost exceeds what
    # de-duplicating the 4-line block would save.
    "cli/commands.py": 8122,
    # api/app.py 5338 -> 5346 (+8): same budget-floor warning surfaced by
    # `send-back`/`reply` as `budget_warning` in the JSON response. Net cost
    # was trimmed from a naive +14 to +8 by computing `Bounds.from_config(...)`
    # once in `reply_task` and reusing it for both the new warning check and
    # the pre-existing `apply_action` bounds argument (previously recomputed
    # inline), and by dropping a redundant function-local `Bounds` re-import
    # in `send_back` — `Bounds` is already imported at module level (line 53).
    # Grew to 5372 (+26) when the approve-refusal surfacing landed
    # (ee101fc460: every `nh approve` refusal path must surface its exact
    # refusal text in the UI, loudly — new refusal-detail plumbing in the
    # approve endpoints). Reviewed on its merits by that task's independent
    # review; the landing omitted this re-anchor, which turned main red at
    # the next full-suite run (2026-08-30). Frozen at its landing baseline;
    # growth from here fails.
    # Grew to 5471 (+99) with the 0.1.8 worker-count feature: the
    # GET/PUT /api/config/workers endpoints + their _workers_payload helper, so
    # the concurrent-worker count is configurable from the Settings Models pane
    # (config.set_concurrency). Re-anchored in the same session it landed.
    # Grew to 5488 (+17) when PR #913's _loaded_code_stale fix landed (a failed
    # head_sha() no longer clobbers a cached 'behind HEAD' verdict) — re-anchored
    # here on the next merge (#913 landed without re-measuring the ratchet).
    # 5488 -> 5489 (+1): PR #867 backend kwarg at the drawer pricing site.
    # 5489 -> 5572 (+83): POST /api/tasks/{id}/split — the 1-click feasibility
    # split creates the confirmed sub-task drafts as child tasks (feature #1).
    # 5572 -> 5596 (+24): the /split reservation race-fix (cancel-parent-first CAS).
    # 5596 -> 5615 (+19): create-time feasibility-hint wiring (feature #1).
    "api/app.py": 5615,
    # +51: W5 active-time phase writer (phase instrumentation).
    # +84: `list_escalations`/`list_review_fails`/`list_tamper_trips` — the
    # three new failure-signal sources the recurring learning harvest mines.
    # 4388 -> 4411 (+23): Store.done_rate_by_tier — per-tier done-rate
    # calibration for the feasibility hint (feature #1).
    "core/db.py": 4411,
    # +71: set_local_backend_fields — the config-write helper for the Settings
    # pane's local coder-backend fields (llm.local_model / llm.local_base_url).
    # +75: Codex account config helpers.
    # +27: the `harvest` config section (interval_hours, enabled) for the
    # recurring learning-harvest cadence.
    # +164: the 0.1.8 worker-count feature's concurrency support —
    # set_concurrency (validated max_workers 1..64 + .enabled write, with
    # reload-verify and restore-on-failure), the _CONCURRENCY_HEADER_RE /
    # _splice_concurrency_scalar plumbing, and the concurrency config section.
    # Re-anchored in the same session it landed.
    "config.py": 3240,
    # +61: the tamper-adjudication one-bounded-retry contract (mechanical-
    # failure classification + the extracted `_review_tamper_adjudication`
    # helper that keeps `AdversarialReviewer.review` itself under the
    # function-line threshold — see the FROZEN_FUNCTION_LINES deletion note).
    "review/reviewer.py": 2896,
    "blockers/wake.py": 2706,
    # +91: `_SCAN_WRAPPER_NAMES` + `_peel_scan_wrappers` — peels
    # timeout/xargs/nice/stdbuf (and siblings) for the scan-severity check
    # only, so a wrapped `find … -delete` in a denied compound classifies
    # DESTRUCTIVE instead of HYGIENE. Local sibling list, `_WRAPPERS` untouched.
    "agent/guard.py": 2892,
    # +44: idle-path recover_quota_cooldown gate in tick() and the
    # never-shorten-a-live-wall guard in _run — the quota-wall storm cost fix.
    # +129: `HarvestJob` — the cadence job (`due()`/`maybe_run()`) that runs
    # both harvest passes from inside `nh serve`'s existing wake-watcher loop.
    "core/scheduler.py": 2695,
}


@dataclass(frozen=True)
class Entry:
    key: str
    lines: int
    cc: int


def _cyclomatic(fn: ast.AST) -> int:
    """Cyclomatic complexity estimate for a function node. See module
    docstring for the exact formula; `AsyncWith` is deliberately excluded."""
    cc = 1
    for node in ast.walk(fn):
        if isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.With,
                ast.IfExp,
                ast.match_case,
            ),
        ):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            cc += len(node.ifs)
    return cc


def _walk_defs(node: ast.AST, prefix: str, path_key: str, out: list[Entry]) -> None:
    """Explicit recursive descent through ClassDef/FunctionDef/AsyncFunctionDef,
    building dotted qualnames. NOT `ast.walk` — that loses nesting and would
    collide e.g. two different classes' `_run` methods under one key."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _walk_defs(child, f"{prefix}{child.name}.", path_key, out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}{child.name}"
            lines = child.end_lineno - child.lineno + 1
            out.append(Entry(f"{path_key}:{qualname}", lines, _cyclomatic(child)))
            _walk_defs(child, f"{qualname}.", path_key, out)


def scan_source(text: str, path: str) -> tuple[list[Entry], int]:
    """Parse `text` (as if it were `path`) and return (every function Entry,
    total line count of the file). Unfiltered — callers apply thresholds."""
    tree = ast.parse(text, filename=path)
    entries: list[Entry] = []
    _walk_defs(tree, "", path, entries)
    return entries, len(text.splitlines())


def scan_tree(root: Path) -> tuple[dict[str, int], dict[str, int], dict[str, int], int, int]:
    """Walk every `*.py` under `root` once. Returns three dicts of only the
    OFFENDING entries (over their respective threshold) — function line
    counts, function cc, file line counts — plus (total files scanned, total
    functions scanned) for the fail-closed floor check."""
    function_lines: dict[str, int] = {}
    function_cc: dict[str, int] = {}
    file_lines: dict[str, int] = {}
    total_functions = 0
    files = sorted(root.rglob("*.py"))
    for path in files:
        rel = path.relative_to(root).as_posix()
        entries, lines = scan_source(path.read_text(), rel)
        total_functions += len(entries)
        if lines > MAX_FILE_LINES:
            file_lines[rel] = lines
        for entry in entries:
            if entry.lines > MAX_FUNCTION_LINES:
                function_lines[entry.key] = entry.lines
            if entry.cc > MAX_FUNCTION_CC:
                function_cc[entry.key] = entry.cc
    return function_lines, function_cc, file_lines, len(files), total_functions


def offenders(
    measured: dict[str, int], frozen: dict[str, int], threshold: int, list_name: str
) -> tuple[list[str], list[str], list[str]]:
    """Pure comparison of a measured dict against a frozen allow-list.
    Returns (new, grown, stale) as rendered message strings, each naming the
    offending key so a failure is actionable without re-running the scan.

      new   -- measured[key] > threshold and key not in frozen
      grown -- key in frozen, measured[key] > frozen[key]
      stale -- key in frozen but either absent from measured (symbol
               renamed/deleted) or measured[key] <= threshold (shrunk below
               the line/cc it was frozen for) -- both causes emit the same
               "delete it" instruction, because both mean the entry no
               longer protects anything.
    """
    new: list[str] = []
    for key, value in measured.items():
        if value > threshold and key not in frozen:
            new.append(f"{list_name}: {key} is {value} (> {threshold}) and is not frozen")

    grown: list[str] = []
    stale: list[str] = []
    for key, frozen_value in frozen.items():
        current = measured.get(key)
        if current is None or current <= threshold:
            stale.append(f"delete `{key}` from {list_name} in tests/test_structural_budget.py")
        elif current > frozen_value:
            grown.append(
                f"{key}: frozen {frozen_value}, now {current} "
                f"(+{current - frozen_value}); this budget only ratchets down"
            )
    return new, grown, stale


@pytest.fixture(scope="session")
def scanned() -> tuple[dict[str, int], dict[str, int], dict[str, int], int, int]:
    return scan_tree(SRC)


# ── fail-closed floors + baseline sanity ──────────────────────────────── #


def test_the_scanner_sees_the_whole_package(scanned):
    _, _, _, total_files, total_functions = scanned
    assert SRC.is_dir()
    assert total_files >= 100, f"only found {total_files} files under {SRC} -- scanner is walking nothing"
    assert total_functions >= 300, f"only found {total_functions} functions -- scanner is walking nothing"


def test_frozen_lists_are_the_measured_baseline():
    assert FROZEN_FUNCTION_LINES
    assert FROZEN_FUNCTION_CC
    assert FROZEN_FILE_LINES
    assert "core/orchestrator.py:Orchestrator._run_attempt" in FROZEN_FUNCTION_LINES
    assert "core/orchestrator.py:Orchestrator._run_attempt" in FROZEN_FUNCTION_CC
    assert "core/orchestrator.py" in FROZEN_FILE_LINES


# ── no new offender ────────────────────────────────────────────────────── #


def test_no_new_oversized_functions(scanned):
    function_lines, _, _, _, _ = scanned
    new, _, _ = offenders(function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES")
    assert new == [], "\n".join(new)


def test_no_new_complex_functions(scanned):
    _, function_cc, _, _, _ = scanned
    new, _, _ = offenders(function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC")
    assert new == [], "\n".join(new)


def test_no_new_oversized_files(scanned):
    _, _, file_lines, _, _ = scanned
    new, _, _ = offenders(file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES")
    assert new == [], "\n".join(new)


# ── no growth ───────────────────────────────────────────────────────────── #


def test_no_frozen_entry_has_grown(scanned):
    function_lines, function_cc, file_lines, _, _ = scanned
    checks = [
        (function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES"),
        (function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC"),
        (file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES"),
    ]
    for measured, frozen, threshold, name in checks:
        _, grown, _ = offenders(measured, frozen, threshold, name)
        assert grown == [], "\n".join(grown)


# ── ratchet: shrunk-below-threshold / vanished entries must be deleted ──── #


def test_a_frozen_entry_that_dropped_below_threshold_must_be_deleted(scanned):
    """Real-tree enforcement: if any frozen entry has shrunk below its
    threshold on the live tree, this must fail naming it -- that is the
    instruction to delete it from the allow-list, not to widen anything."""
    function_lines, function_cc, file_lines, _, _ = scanned
    checks = [
        (function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES"),
        (function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC"),
        (file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES"),
    ]
    for measured, frozen, threshold, name in checks:
        _, _, stale = offenders(measured, frozen, threshold, name)
        assert stale == [], "\n".join(stale)


def test_a_frozen_entry_whose_symbol_vanished_must_be_deleted():
    """Mechanism proof: a frozen key entirely absent from the measured dict
    (function renamed or deleted) is reported stale with a delete
    instruction naming it -- distinct from, but handled the same as, a
    value that merely shrank below threshold (see the test above)."""
    measured = dict(FROZEN_FUNCTION_LINES)
    key = next(iter(FROZEN_FUNCTION_LINES))
    del measured[key]
    new, grown, stale = offenders(measured, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert grown == []
    assert stale == [f"delete `{key}` from FROZEN_FUNCTION_LINES in tests/test_structural_budget.py"]


# ── negative x3: offenders() on fully synthetic dicts ──────────────────── #


def test_offenders_reports_a_new_entry():
    measured = {"pkg/mod.py:foo": 350}
    new, grown, stale = offenders(measured, {}, 300, "FROZEN_FUNCTION_LINES")
    assert grown == []
    assert stale == []
    assert len(new) == 1
    assert "pkg/mod.py:foo" in new[0]


def test_offenders_reports_growth():
    measured = {"pkg/mod.py:foo": 320}
    frozen = {"pkg/mod.py:foo": 300}
    new, grown, stale = offenders(measured, frozen, 300, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert stale == []
    assert len(grown) == 1
    assert "pkg/mod.py:foo" in grown[0]


def test_offenders_reports_a_stale_entry():
    # `foo` vanished entirely; `bar` is still present but shrank <= threshold.
    frozen = {"pkg/mod.py:foo": 300, "pkg/mod.py:bar": 300}
    measured = {"pkg/mod.py:bar": 250}
    new, grown, stale = offenders(measured, frozen, 300, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert grown == []
    assert len(stale) == 2
    assert any("pkg/mod.py:foo" in msg for msg in stale)
    assert any("pkg/mod.py:bar" in msg for msg in stale)


# ── known-positive probes, each with a negative twin ────────────────────── #


def _function_source(body_lines: int) -> str:
    return "def f():\n" + "    pass\n" * body_lines


def test_a_301_line_function_is_flagged_a_299_line_function_is_not():
    positive, _ = scan_source(_function_source(300), "synthetic.py")
    negative, _ = scan_source(_function_source(298), "synthetic.py")
    assert positive[0].lines == 301
    assert positive[0].lines > MAX_FUNCTION_LINES
    assert negative[0].lines == 299
    assert negative[0].lines <= MAX_FUNCTION_LINES


def _cc_source(n_ifs: int) -> str:
    body = "\n".join("    if True:\n        pass" for _ in range(n_ifs))
    return "def f():\n" + body + "\n"


def test_a_cc_61_function_is_flagged_a_cc_59_function_is_not():
    positive, _ = scan_source(_cc_source(60), "synthetic.py")
    negative, _ = scan_source(_cc_source(58), "synthetic.py")
    assert positive[0].cc == 61
    assert positive[0].cc > MAX_FUNCTION_CC
    assert negative[0].cc == 59
    assert negative[0].cc <= MAX_FUNCTION_CC


def _file_source(n_lines: int) -> str:
    return "x = 1\n" * n_lines


def test_a_2501_line_file_is_flagged_a_2500_line_file_is_not():
    _, positive_lines = scan_source(_file_source(2501), "synthetic.py")
    _, negative_lines = scan_source(_file_source(2500), "synthetic.py")
    assert positive_lines == 2501
    assert positive_lines > MAX_FILE_LINES
    assert negative_lines == 2500
    assert negative_lines <= MAX_FILE_LINES


# ── formula pin ──────────────────────────────────────────────────────────── #


_CC_SNIPPET = textwrap.dedent(
    '''\
    def f(a, b, c, x):
        if a:
            pass
        for i in x:
            pass
        while b:
            pass
        try:
            pass
        except Exception:
            pass
        with open("f") as fh:
            pass
        z = a and b and c
        y = 1 if a else 2
        lst = [i for i in x if i]
        match a:
            case 1:
                pass
            case 2:
                pass

        async def g():
            async with open("f") as fh2:
                pass
    '''
)


def test_cc_formula_on_a_hand_counted_snippet():
    # if(+1) for(+1) while(+1) except(+1) with(+1) boolop-of-3(+2) ternary(+1)
    # comprehension-if(+1) match-2-cases(+2) = 11, plus the base 1 = 12.
    # The nested `async with` is walked (it is inside f's subtree) but must
    # NOT be counted -- that is the asymmetry this test exists to pin.
    entries, _ = scan_source(_CC_SNIPPET, "synthetic.py")
    outer = next(e for e in entries if e.key == "synthetic.py:f")
    assert outer.cc == 12, outer.cc


# ── qualname correctness ─────────────────────────────────────────────────── #


def test_qualnames_cover_methods_and_nested_functions():
    src = textwrap.dedent(
        """\
        class A:
            def m(self):
                def inner():
                    pass
                return inner
        """
    )
    entries, _ = scan_source(src, "mod.py")
    keys = {e.key for e in entries}
    assert "mod.py:A.m" in keys
    assert "mod.py:A.m.inner" in keys


# ── runtime bound ─────────────────────────────────────────────────────────── #


def test_the_whole_walk_finishes_under_five_seconds():
    start = time.perf_counter()
    scan_tree(SRC)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"scan_tree(SRC) took {elapsed:.2f}s (must be < 5.0s)"


# ── docs ─────────────────────────────────────────────────────────────────── #


def test_verification_doc_names_this_guard_and_its_thresholds():
    doc = (REPO_ROOT / "docs" / "verification.md").read_text()
    assert "test_structural_budget.py" in doc
    assert "300" in doc
    assert "60" in doc
    assert "2,500" in doc
