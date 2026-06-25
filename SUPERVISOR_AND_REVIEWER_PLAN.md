# Supervisor Agent + Staff Reviewer — Detailed Implementation Plan

> **Goal:** Close the two biggest gaps in no_human: (1) a Supervisor Agent that
> replaces the human in the loop during task execution, and (2) a Staff-level
> Code Reviewer that reviews like an experienced engineer. After this, the human
> only approves PRs, gives code review feedback, and handles unexpected blockers.

> **Verified against:** Claude Agent SDK hooks (introspected live — not assumed),
> the current orchestrator spine (1247 lines), the existing test suite (470 tests),
> and the blocker taxonomy.

---

## 0. What the SDK Actually Supports (Verified — Not Assumed)

**Evidence:** `uv run python3 -c "from claude_agent_sdk import ..."` on the live
SDK in this repo. Every claim below is backed by introspection output.

### Hook Types Available

| Hook | Fires When | Can Inject Context | Can Stop Agent | Can Modify I/O |
|---|---|---|---|---|
| `PreToolUse` | Before every tool call | ✅ `additionalContext` | ✅ `continue_: False` | ✅ `updatedInput` |
| `PostToolUse` | After every tool call | ✅ `additionalContext` | ✅ `continue_: False` | ✅ `updatedToolOutput` |
| `PostToolUseFailure` | After a tool fails | ✅ `additionalContext` | ✅ `continue_: False` | ❌ |
| `Stop` | When agent wants to stop | ❌ | ✅ `continue_: True` (keep going) | ❌ |
| `Notification` | Agent emits a notification | ✅ `additionalContext` | ❌ | ❌ |
| `SubagentStart` | Before a subagent starts | ✅ `additionalContext` | ❌ | ❌ |

### Key Mechanism: `additionalContext`

Every hook can return `additionalContext: str` in its output. This string is
**injected into the agent's conversation as a system-level message**, visible to
the agent on its next turn. This is the exact mechanism we need for the
Supervisor to course-correct the working agent mid-flight.

### Key Mechanism: `continue_: False`

Any hook can return `continue_: False` with a `stopReason` to force-stop the
agent session. This lets the Supervisor abort a doomed attempt early.

### Other Relevant SDK Features

- **`agents`** dict on `ClaudeAgentOptions` — defines subagent configurations
- **`fork_session(session_id)`** — fork a session for independent review
- **`get_session_messages(session_id)`** — read transcript of a session
- **`system_prompt`** — custom system prompt for each session
- **`mcp_servers`** — can register custom MCP tools

---

## 1. Architecture: Supervisor Agent

### What It Replaces

Today, when the working agent:
- **Drifts off-task** → burns the entire attempt, fails, retries (wastes turns)
- **Ignores a rule** → only caught post-hoc by the reviewer (if at all)
- **Asks a question** → must emit BLOCKER_JSON, task parks, human answers
- **Gets stuck in a loop** → StuckDetector catches after 2 repeats (too late)
- **Makes a wrong architectural choice** → only caught in review (wasted work)
- **Hallucinates a function/API** → tool call fails, agent retries blindly

After this change, the Supervisor intercepts ALL of these in real-time.

### Design: PostToolUse Hook with Periodic LLM Evaluation

The Supervisor is NOT a separate long-running agent. It is a **PostToolUse hook**
that fires after every N tool calls (configurable, default: every 5). On each
firing, it runs a fast, focused LLM evaluation with:

1. The task's acceptance criteria and rules
2. A sliding window of the last N tool calls + results (from the event stream)
3. The confirmed rules and skills from the learning queue
4. The project profile (ecosystem, conventions)

The LLM returns one of:
- **`CONTINUE`** — agent is on track, no action needed
- **`CORRECT`** — agent is drifting; inject `additionalContext` with correction
- **`ANSWER`** — agent asked a question the Supervisor can answer from context
- **`STOP`** — agent is doomed; abort with `continue_: False` + `stopReason`

```
┌─────────────────────────────────────────────────┐
│                  Orchestrator                    │
│  ┌───────────────────────────────────────────┐  │
│  │           ClaudeBackend.run()              │  │
│  │  ┌─────────────────────────────────────┐  │  │
│  │  │        Working Agent Session        │  │  │
│  │  │  (implements the task)              │  │  │
│  │  └──────────┬──────────────────────────┘  │  │
│  │             │ PostToolUse hook fires       │  │
│  │             ▼                              │  │
│  │  ┌─────────────────────────────────────┐  │  │
│  │  │       SupervisorHook                │  │  │
│  │  │  - Accumulates tool call window     │  │  │
│  │  │  - Every N calls: evaluate via LLM  │  │  │
│  │  │  - Returns additionalContext or     │  │  │
│  │  │    continue_: False                 │  │  │
│  │  └─────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────┐  │
│  │  After attempt completes:                 │  │
│  │  StaffReviewer (fork_session + review)    │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### Why PostToolUse (Not a Separate Parallel Agent)

**Devil's advocate: "Why not run a separate agent in parallel watching the
transcript?"**

1. **SDK doesn't support concurrent sessions writing to the same conversation.**
   The hooks are the designed interception point. A parallel agent would need to
   `fork_session` → read → inject, which is racey and fragile.
2. **Hooks are synchronous gates.** The working agent WAITS for the hook to
   return before proceeding. This guarantees the correction is seen before the
   next action.
3. **Cost control.** A parallel agent burns tokens continuously. A periodic hook
   only invokes the LLM every N calls (default: 5), keeping costs bounded.
4. **Simplicity.** One process, one event loop, deterministic ordering.

### Why Not PreToolUse Only

PreToolUse fires BEFORE the tool runs — you don't yet know the RESULT. Many
corrections need the output ("that grep returned nothing because you searched the
wrong directory"). PostToolUse has both `tool_input` AND `tool_response`.

We DO also use PreToolUse for:
- Blocking dangerous commands (existing guard — unchanged)
- Detecting about-to-hallucinate patterns (e.g., agent tries to import a
  non-existent module — detectable from the input alone)

### Supervisor Evaluation Prompt

```
You are the Supervisor of an autonomous coding agent. Your job is to ensure the
working agent stays on-task, follows the rules, and produces correct work.

Task: {task.title}
Acceptance criteria: {criteria}
Rules: {rules + confirmed_memories}
Project profile: {profile}

Recent tool calls (last {window_size}):
{tool_call_window}

Current state: The agent has made {total_calls} tool calls so far this attempt.

Evaluate the agent's progress. Respond with EXACTLY ONE of:

SUPERVISOR_CONTINUE
(Agent is on track. No action needed.)

SUPERVISOR_CORRECT
<your correction message — be specific, cite what went wrong and what to do>

SUPERVISOR_ANSWER
<answer to the agent's question, derived from the rules/skills/context>

SUPERVISOR_STOP
<reason why this attempt is doomed and should be aborted>
```

### What the Supervisor Can Answer (Replacing the Human)

Based on the conversations you've shown me, the human (you) does these things:

1. **"Why is the description field tiny?"** → UI/UX correction (Supervisor sees
   the CSS edit result and course-corrects)
2. **"Did you complete the work?"** → Progress check (Supervisor tracks
   acceptance criteria completion)
3. **"You're going down the wrong path"** → Architectural redirect (Supervisor
   compares approach to rules/skills)
4. **"Use sed not python3 in CI"** → Rule enforcement (Supervisor has the rules)
5. **"That scored 9/10, stop"** → Quality gate (Supervisor enforces the
   never-ship-9/10 rule)
6. **Domain questions** → Supervisor has the confirmed skills/learnings

What the Supervisor CANNOT answer (stays with human):
- Permission grants (API keys, access tokens)
- Business decisions ("should we split this task?")
- Final PR approval
- Unexpected infrastructure issues

---

## 2. Architecture: Staff-Level Code Reviewer

### What Changes from the Current AdversarialReviewer

| Aspect | Current | After |
|---|---|---|
| **Prompt** | Generic "find faults" | Multi-pass with codebase-aware criteria |
| **Context** | Diff + test output only | Diff + tests + profile + conventions + architecture |
| **Review type** | Single pass | 3-pass: correctness → architecture → edge cases |
| **Model** | claude-sonnet-4-6 | claude-sonnet-4-6 (same — the prompt is the upgrade) |
| **Held-out tests** | ✅ Already exists | ✅ Keep |
| **Security check** | ❌ None | ✅ Explicit security checklist item |
| **Performance** | ❌ None | ✅ Checks for O(n²), unbounded allocations |
| **Style/conventions** | ❌ None | ✅ Uses profile + confirmed rules |
| **Fail-closed** | ✅ Already exists | ✅ Keep |

### Enhanced Review Prompt (3-Pass Structure)

```
You are a Staff Software Engineer performing an independent code review.
You are adversarial: your job is to find every flaw and prove this change is NOT
ready for production. Never trust the implementer's claims.

== PASS 1: CORRECTNESS ==
For each acceptance criterion:
  - Read the ACTUAL changed code (not the commit message)
  - Verify the logic handles the stated requirement
  - Check that tests genuinely exercise the changed code (not vacuous passes)
  - Verify no test was weakened, deleted, or made to pass trivially

== PASS 2: ARCHITECTURE & DESIGN ==
  - Is this the right approach, or a workaround/hack?
  - Does it follow existing patterns in this codebase? (Profile: {ecosystem})
  - Are there simpler alternatives?
  - Is the change appropriately scoped (not too big, not too small)?
  - Are imports correct and at the top of the file?

== PASS 3: EDGE CASES, SECURITY & PERFORMANCE ==
  - What inputs/states would break this?
  - Are error paths handled (not just the happy path)?
  - Any injection, path traversal, or credential exposure risks?
  - Any O(n²) loops, unbounded allocations, or blocking I/O in async code?
  - What happens in a different environment (CI vs local, Linux vs macOS)?

== CONFIRMED RULES (from project experience) ==
{confirmed_rules}

== PROJECT CONVENTIONS ==
{profile_context}
```

### Why This Is Better Than Current

The current reviewer gets a bare diff and "find faults." It has no idea what the
team's conventions are, what patterns the codebase uses, or what past mistakes
to watch for. It's like asking a random contractor to review code for a project
they've never seen.

The enhanced reviewer gets:
1. The project profile (ecosystem, test cmd, lint cmd)
2. Confirmed rules from past failures (the learning queue)
3. Explicit multi-pass structure so it doesn't skip categories
4. The full acceptance criteria (already exists, but now weighted per-pass)

---

## 3. Implementation Plan — File-by-File

### Phase A: SupervisorHook (3 files, ~400 lines)

**File 1: `src/no_human/agent/supervisor.py`** (~200 lines)

```python
# Pure policy + LLM evaluation for the PostToolUse hook.
# No I/O except the LLM call. Fully testable with a FakeLLM.

class SupervisorHook:
    """PostToolUse hook that evaluates the working agent's progress."""

    def __init__(self, *, task, rules, profile, backend, check_every=5):
        ...

    def record_tool_call(self, tool_name, tool_input, tool_response):
        """Accumulate a tool call in the sliding window."""
        ...

    async def evaluate(self) -> SupervisorDecision:
        """Run the LLM evaluation on the current window."""
        ...

    async def hook(self, input_data, tool_use_id, context):
        """The actual hook callback registered with the SDK."""
        ...
```

**File 2: Update `src/no_human/agent/claude_backend.py`** (~30 lines changed)

- Add a `supervisor_hook` parameter to `ClaudeBackend.__init__`
- Register it as a `PostToolUse` hook alongside the existing `PreToolUse` guard
- The supervisor hook is optional — when None, behavior is unchanged

**File 3: Update `src/no_human/core/orchestrator.py`** (~20 lines changed)

- In `_run_attempt`, construct a `SupervisorHook` with the task's rules, profile,
  and a lightweight LLM backend
- Pass it to `ClaudeBackend.run()`
- Emit supervisor events to the event sink (for `nh watch` visibility)

### Phase B: StaffReviewer (2 files, ~150 lines)

**File 1: Update `src/no_human/review/reviewer.py`** (~80 lines changed)

- Replace `_build_review_prompt` with the 3-pass prompt
- Add `profile` and `confirmed_rules` parameters
- Add a `_build_staff_review_prompt` function

**File 2: Update `src/no_human/core/orchestrator.py`** (~10 lines changed)

- Pass the profile and confirmed rules to the reviewer

### Phase C: Tests (2 files, ~300 lines)

**File 1: `tests/test_supervisor.py`** (~200 lines)

- Unit tests for `SupervisorHook` with a fake LLM
- Test CONTINUE/CORRECT/ANSWER/STOP parsing
- Test sliding window accumulation
- Test check_every throttling (doesn't fire every call)
- Test that corrections produce valid hook output format
- E2E test with FakeBackend: supervisor injects correction → agent sees it

**File 2: `tests/test_reviewer.py`** (~100 lines)

- Update existing tests for the new 3-pass prompt
- Test that profile and rules are included in the prompt
- Test that the 3-pass structure produces valid checklist items

---

## 4. Devil's Advocate — What Can Go Wrong

### Risk 1: Supervisor LLM cost explosion
**Problem:** If the Supervisor fires on every tool call and each evaluation
costs tokens, a 40-turn attempt could 8x the token cost.
**Mitigation:** `check_every=5` (default). 40 turns ≈ 8 evaluations. Each
evaluation uses a short prompt (~2k tokens) with `effort="low"`. Cost is ~15%
overhead, not 8x.
**Verification:** We can measure this in tests by counting backend calls.

### Risk 2: Supervisor correction confuses the working agent
**Problem:** `additionalContext` injections might derail the agent by adding
contradictory instructions.
**Mitigation:** Corrections are short, specific, and framed as "system notices"
— not new task instructions. The prompt explicitly says "do not change the task,
only correct the approach." We test this in E2E.
**Verification:** E2E test with FakeBackend that verifies the correction is
received without breaking the agent's flow.

### Risk 3: Supervisor hallucination (wrong correction)
**Problem:** The Supervisor LLM might give a wrong correction, making the agent
do something worse.
**Mitigation:** (1) Supervisor only fires with high confidence — if unsure, it
returns CONTINUE. (2) The existing reviewer + tamper guard + tests still gate
the final output. (3) Supervisor corrections are logged in the event stream for
post-hoc audit.
**Verification:** The multi-layer gate (tests + tamper + review) catches any
damage a bad correction might cause.

### Risk 4: Hook output format wrong → SDK crash
**Problem:** If our hook returns malformed JSON, the SDK might crash the session.
**Mitigation:** Pure unit tests for every hook output format. The `SyncHookJSONOutput`
TypedDict is the contract — we test against it.
**Verification:** Unit tests for each decision type's hook output.

### Risk 5: Reviewer prompt too long → context window overflow
**Problem:** The 3-pass prompt + diff + rules + profile might exceed limits.
**Mitigation:** Existing `_DIFF_CAP = 8000` and `_OUTPUT_CAP = 4000` stay.
Rules are bounded to 20 entries (existing `_format_active_memories`). Profile
is ~5 lines. Total ≈ 15k chars, well within limits.

### Risk 6: Breaking existing 470 tests
**Problem:** Changing the orchestrator or backend could break existing tests.
**Mitigation:** (1) SupervisorHook is optional — None means no change. (2) All
existing tests pass None for the supervisor (default behavior). (3) We run the
full suite after every change.
**Verification:** `uv run pytest -q` must show 470+ passed.

---

## 5. Scoring Checklist

Before shipping each phase:

- [ ] All 470+ existing tests pass
- [ ] New tests cover: CONTINUE, CORRECT, ANSWER, STOP decisions
- [ ] New tests cover: hook output format matches SDK TypedDict
- [ ] New tests cover: check_every throttling
- [ ] New tests cover: sliding window bounded (doesn't grow unbounded)
- [ ] New tests cover: reviewer 3-pass prompt includes profile + rules
- [ ] E2E test: supervisor correction visible in event stream
- [ ] E2E test: supervisor STOP aborts attempt cleanly
- [ ] No test weakened or deleted (tamper guard self-check)
- [ ] Devil's advocate: "What input breaks this?" for every new function

---

## 6. What This Does NOT Change

- **Task lifecycle states** — unchanged
- **Blocker taxonomy** — unchanged
- **Tamper guard** — unchanged
- **Safety guard (PreToolUse)** — unchanged
- **PR workflow (never merge)** — unchanged
- **Human approval requirement** — unchanged
- **Learning queue** — unchanged (but Supervisor reads confirmed memories)

---

## 7. Execution Order

1. **Phase A1:** `supervisor.py` — pure policy, no wiring
2. **Phase C1:** `test_supervisor.py` — tests for A1
3. **Run full suite** — verify 470+ pass
4. **Phase A2:** Wire supervisor into backend + orchestrator
5. **Phase C1 update:** E2E test with FakeBackend
6. **Run full suite** — verify 470+ pass
7. **Phase B:** Staff reviewer prompt upgrade
8. **Phase C2:** `test_reviewer.py` updates
9. **Run full suite** — verify 470+ pass
10. **Final verification** — all tests green, no tests weakened
