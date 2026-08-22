"""Merge-ready policy — a declared, human-legible rule list evaluated
mechanically from gate outputs into a per-rule pass/fail verdict.

WHY. Today the only "is this safe to approve" signal is a human reading the
whole PR. This module lets an operator declare, in a reviewable file
(``<repo>/.no_human/merge_policy.yaml``), which mechanical facts must all
hold for a PR to be "merge-ready", and computes a per-rule pass/fail list
from those facts with NO model call.

This verdict is **advisory to the human**. No code path in this repo merges
on it, waits on it, or gates a push on it. ``nh approve`` remains the only
way a PR merges, and it is always a human invocation — nothing here changes
that. ``approval.auto_merge_on_approval`` (``config.py``) is not read by this
module and stays exactly as it is.

This module is deliberately un-wired: nothing imports it, there is no
``PrEvidence`` -> :class:`GateFacts` adapter (that is a later ticket), no
PR-body rendering, no board field, no API route, no DB column. It is the
schema, loader, and evaluator only.

A malformed or partially-invalid policy file never raises: it degrades to a
:class:`PolicyLoad` carrying diagnostic ``problems`` strings, and a verdict
built from a load with any problem is never "ready" (fail closed).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

# --- vocabulary ------------------------------------------------------------ #

RULE_NAMES: tuple[str, ...] = (
    "review_passed",
    "no_advisory_findings",
    "tests_ran_and_passed",
    "tamper_guard_clear",
    "repro_gate",
    "verifiers_all_satisfied",
    "ci",
    "paths_within",
    "max_changed_lines",
)

# Which rules take an argument, and what shape it must be. Loader validation
# and evaluator dispatch both read from this table so they cannot drift.
_NO_ARG_RULES = frozenset(
    {
        "review_passed",
        "no_advisory_findings",
        "tests_ran_and_passed",
        "tamper_guard_clear",
        "verifiers_all_satisfied",
    }
)
_ENUM_RULES: dict[str, frozenset[str]] = {
    "repro_gate": frozenset({"pass", "pass_or_not_required"}),
    "ci": frozenset({"success", "success_or_unknown"}),
}
_LIST_STR_RULES = frozenset({"paths_within"})
_INT_RULES = frozenset({"max_changed_lines"})

POLICY_RELPATH = ".no_human/merge_policy.yaml"
# Untrusted, hand-edited file: cap the size we'll even attempt to parse.
MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class Rule:
    name: str
    arg: Any = None


# The default, used when no policy file is present. `ci: success_or_unknown`
# is the default (not the strict `ci: success`) deliberately: forges report
# infra outages as CI failures, and a false "not ready" from an outage is
# worse than tolerating an unreported/unknown CI state here.
DEFAULT_POLICY: tuple[Rule, ...] = (
    Rule("review_passed"),
    Rule("tests_ran_and_passed"),
    Rule("tamper_guard_clear"),
    Rule("repro_gate", "pass_or_not_required"),
    Rule("verifiers_all_satisfied"),
    Rule("ci", "success_or_unknown"),
)


@dataclass
class PolicyLoad:
    rules: list[Rule]
    problems: list[str]
    source: str  # "file" or "default"


@dataclass(frozen=True)
class GateFacts:
    """Flat mechanical inputs. Deliberately flat so an adapter from the
    orchestrator's evidence object is trivial and testable — that adapter is
    a later ticket, not written here."""

    review_passed: bool | None
    review_advisory_count: int = 0
    tests_ran: bool = False
    tests_failed: int = 0
    tests_passed: int = 0
    tamper_fired: bool = False
    tamper_waived: bool = False
    repro_verdict: str | None = None  # pass/fail/waived/error/None
    repro_required: bool = False
    verifiers_ran: int = 0
    verifiers_failed: tuple[str, ...] = ()
    ci_state: str | None = None  # success/failure/pending/unknown/None
    changed_paths: tuple[str, ...] = ()
    changed_lines: int = 0


@dataclass(frozen=True)
class RuleVerdict:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PolicyVerdict:
    ready: bool
    rules: tuple[RuleVerdict, ...]
    source: str
    problems: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        if self.ready:
            return f"ready — {len(self.rules)} of {len(self.rules)} rules satisfied"
        failed = [v.name for v in self.rules if not v.passed]
        return (
            f"not ready — {len(failed)} of {len(self.rules)} rules failed: "
            f"{', '.join(failed)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "summary": self.summary,
            "source": self.source,
            "problems": list(self.problems),
            "rules": [
                {"name": v.name, "passed": v.passed, "detail": v.detail}
                for v in self.rules
            ],
        }


# --- loader ------------------------------------------------------------- #


def load_policy(repo_path: Path) -> PolicyLoad:
    """Read ``<repo_path>/.no_human/merge_policy.yaml``.

    Absent (or a directory at that path) ⇒ the clean default: DEFAULT_POLICY,
    no problems, source "default". Anything else that goes wrong (unreadable,
    oversized, invalid YAML, wrong shape, unknown rule name, bad argument)
    degrades to DEFAULT_POLICY as the rule list, source "file", with one
    problem string per issue found — never raises.
    """
    path = Path(repo_path).expanduser() / POLICY_RELPATH
    try:
        if not path.is_file():
            return PolicyLoad(rules=list(DEFAULT_POLICY), problems=[], source="default")
        if path.stat().st_size > MAX_BYTES:
            return PolicyLoad(
                rules=list(DEFAULT_POLICY),
                problems=[f"{path}: file exceeds {MAX_BYTES} bytes"],
                source="file",
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 — untrusted input never breaks a run
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: could not be read/parsed: {exc}"],
            source="file",
        )

    if raw is None:
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: empty document"],
            source="file",
        )
    if not isinstance(raw, dict):
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: top level is not a mapping"],
            source="file",
        )
    if "rules" not in raw:
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: missing 'rules' key"],
            source="file",
        )

    raw_rules = raw["rules"]
    if not isinstance(raw_rules, list):
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: 'rules' is not a list"],
            source="file",
        )

    parsed: list[Rule] = []
    problems: list[str] = []
    for item in raw_rules:
        rule, problem = _parse_rule_item(item)
        if rule is not None:
            parsed.append(rule)
        if problem is not None:
            problems.append(f"{path}: {problem}")

    return PolicyLoad(rules=parsed, problems=problems, source="file")


def _parse_rule_item(item: Any) -> tuple[Rule | None, str | None]:
    if isinstance(item, str):
        name, arg = item, None
    elif isinstance(item, dict):
        if len(item) != 1:
            return None, f"rule item has {len(item)} keys, expected exactly one: {item!r}"
        (name, arg), = item.items()
        if not isinstance(name, str):
            return None, f"rule name is not a string: {name!r}"
    else:
        return None, f"rule item is neither a string nor a single-key mapping: {item!r}"

    if name not in RULE_NAMES:
        return None, f"unknown rule name: {name!r}"

    if name in _NO_ARG_RULES:
        if arg is not None:
            return None, f"rule {name!r} takes no argument, got {arg!r}"
        return Rule(name), None

    if name in _ENUM_RULES:
        allowed = _ENUM_RULES[name]
        if not isinstance(arg, str) or arg not in allowed:
            return None, f"rule {name!r} needs one of {sorted(allowed)}, got {arg!r}"
        return Rule(name, arg), None

    if name in _LIST_STR_RULES:
        if (
            not isinstance(arg, list)
            or not arg
            or not all(isinstance(x, str) for x in arg)
        ):
            return None, f"rule {name!r} needs a non-empty list of strings, got {arg!r}"
        return Rule(name, list(arg)), None

    if name in _INT_RULES:
        # bool is a subclass of int in Python — reject it explicitly.
        if isinstance(arg, bool) or not isinstance(arg, int) or arg < 0:
            return None, f"rule {name!r} needs a non-negative int, got {arg!r}"
        return Rule(name, arg), None

    return None, f"rule {name!r} has no known argument contract"  # pragma: no cover


# --- glob matcher --------------------------------------------------------- #
#
# Bare fnmatch is wrong for `paths_within` in the exact direction the spec
# calls out: fnmatch's `*` crosses `/`, so `docs/*` would wrongly match
# `docs/a/b.md`. PurePath.match has no `**` support before Python 3.13. So we
# hand-translate the glob segment-by-segment into one anchored regex, mirroring
# the reasoning (not the code — duplication here is intended) already used by
# project_config._is_scoped_glob.


def _normalize_path(p: str) -> str:
    p = p.replace("\\", "/")
    p = p.removeprefix("./")
    p = p.lstrip("/")
    return PurePosixPath(p).as_posix()


def _translate_segment(seg: str) -> str:
    if seg == "**":
        return ""  # handled specially by the caller
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2)
            if j == -1:
                # unterminated class: fall back to a literal escape of the
                # whole segment rather than raising.
                return re.escape(seg)
            cls = seg[i:j + 1]
            if cls.startswith("[!"):
                cls = "[^" + cls[2:]
            out.append(cls)
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _glob_to_regex(pattern: str) -> str:
    segments = pattern.split("/")
    n = len(segments)
    pieces: list[str] = []
    for idx, seg in enumerate(segments):
        is_last = idx == n - 1
        if seg == "**":
            if is_last:
                # A trailing `**` gets an alternative consuming the
                # remainder: one or more path segments joined by "/", so
                # `docs/**` matches both `docs/x.md` and `docs/a/b.md`.
                pieces.append(r"[^/]+(?:/[^/]+)*")
            else:
                # A non-trailing `**` matches zero or more whole directory
                # segments, each already including its trailing "/".
                pieces.append(r"(?:[^/]+/)*")
        else:
            pieces.append(_translate_segment(seg))
            if not is_last:
                pieces.append("/")
    return "".join(pieces)


_COMPILE_CACHE: dict[str, re.Pattern[str] | None] = {}


def _compiled(pattern: str) -> re.Pattern[str] | None:
    if pattern not in _COMPILE_CACHE:
        try:
            _COMPILE_CACHE[pattern] = re.compile(_glob_to_regex(pattern))
        except re.error:
            _COMPILE_CACHE[pattern] = None
    return _COMPILE_CACHE[pattern]


def _matches_any(path: str, patterns: list[str]) -> bool:
    norm = _normalize_path(path)
    for pattern in patterns:
        rx = _compiled(pattern)
        if rx is not None and rx.fullmatch(norm):
            return True
    return False


# --- evaluator ------------------------------------------------------------ #


def _check_review_passed(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if facts.review_passed is True:
        return True, "review PASSED on head"
    if facts.review_passed is False:
        return False, "review verdict is FAIL on head"
    return False, "no round has judged this head"


def _check_no_advisory_findings(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    n = facts.review_advisory_count
    ok = n == 0
    return ok, f"{n} advisory finding{'s' if n != 1 else ''}"


def _check_tests_ran_and_passed(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if not facts.tests_ran:
        return False, "tests never ran"
    if facts.tests_failed:
        return False, f"tests: {facts.tests_failed} failed of {facts.tests_passed + facts.tests_failed} run"
    if facts.tests_passed < 1:
        return False, "tests ran but 0 passed"
    return True, f"tests: 0 failed of {facts.tests_passed} run"


def _check_tamper_guard_clear(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if not facts.tamper_fired:
        return True, "tamper guard did not fire"
    if facts.tamper_waived:
        return True, "tamper fire waived as legitimate"
    return False, "tamper guard fired, unwaived"


def _check_repro_gate(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    verdict = facts.repro_verdict
    if arg == "pass":
        if verdict == "pass":
            return True, "repro gate pass"
        return False, f"repro gate {verdict if verdict else 'not run'}"
    # pass_or_not_required
    if verdict == "pass":
        return True, "repro gate pass"
    if not facts.repro_required and verdict in (None, "waived"):
        return True, f"repro gate not required (verdict: {verdict if verdict else 'none'})"
    if verdict == "waived":
        return False, "repro gate waived but required"
    return False, f"repro gate {verdict if verdict else 'not run'}"


def _check_verifiers_all_satisfied(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if facts.verifiers_ran == 0:
        return True, "0 verifiers ran"
    if not facts.verifiers_failed:
        return True, f"{facts.verifiers_ran} verifiers, none failed"
    return False, f"{len(facts.verifiers_failed)} verifier{'s' if len(facts.verifiers_failed) != 1 else ''} failed: {', '.join(facts.verifiers_failed)}"


def _check_ci(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    state = facts.ci_state
    if arg == "success":
        if state == "success":
            return True, "ci: success"
        if state in (None,):
            return False, "ci: none reported (strict mode requires success)"
        if state == "unknown":
            return False, "ci: unknown (strict mode requires success)"
        return False, f"ci: {state}"
    # success_or_unknown
    if state == "success":
        return True, "ci: success"
    if state == "unknown":
        return True, "ci: unknown (tolerated)"
    if state is None:
        return True, "ci: none reported (tolerated)"
    return False, f"ci: {state}"


def _check_paths_within(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    patterns: list[str] = arg
    paths = facts.changed_paths
    if not paths:
        return True, "no changed paths"
    offenders = [p for p in paths if not _matches_any(p, patterns)]
    if not offenders:
        return True, f"all {len(paths)} changed paths within the allowed set"
    shown = offenders[:5]
    suffix = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    return False, f"{len(offenders)} paths outside the allowed set: {', '.join(shown)}{suffix}"


def _check_max_changed_lines(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    limit: int = arg
    n = facts.changed_lines
    if n <= limit:
        return True, f"{n} changed lines (limit {limit})"
    return False, f"{n} changed lines exceeds limit {limit}"


_CHECKS: dict[str, Callable[[GateFacts, Any], tuple[bool, str]]] = {
    "review_passed": _check_review_passed,
    "no_advisory_findings": _check_no_advisory_findings,
    "tests_ran_and_passed": _check_tests_ran_and_passed,
    "tamper_guard_clear": _check_tamper_guard_clear,
    "repro_gate": _check_repro_gate,
    "verifiers_all_satisfied": _check_verifiers_all_satisfied,
    "ci": _check_ci,
    "paths_within": _check_paths_within,
    "max_changed_lines": _check_max_changed_lines,
}


def _evaluate(rules: list[Rule], facts: GateFacts, problems: tuple[str, ...], source: str) -> PolicyVerdict:
    verdicts = []
    for rule in rules:
        check = _CHECKS[rule.name]
        passed, detail = check(facts, rule.arg)
        verdicts.append(RuleVerdict(name=rule.name, passed=passed, detail=detail))
    ready = bool(rules) and not problems and all(v.passed for v in verdicts)
    return PolicyVerdict(ready=ready, rules=tuple(verdicts), source=source, problems=problems)


def evaluate(rules: list[Rule], facts: GateFacts) -> PolicyVerdict:
    """Evaluate an explicit rule list against gate facts. Pure function; no
    file I/O. ``source`` is always "file" here — callers that loaded rules
    from disk should prefer :func:`evaluate_repo`, which carries the real
    load source through."""
    return _evaluate(rules, facts, (), source="file")


def evaluate_repo(repo_path: Path, facts: GateFacts) -> PolicyVerdict:
    """``load_policy`` + :func:`evaluate`, carrying ``source``/``problems``
    from the load into the verdict. A verdict built from a load with any
    problem is never "ready" — fail closed."""
    load = load_policy(repo_path)
    return _evaluate(load.rules, facts, tuple(load.problems), source=load.source)
