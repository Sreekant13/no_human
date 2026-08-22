"""Tests for `no_human.core.merge_policy` — the merge-ready policy schema,
loader, and evaluator. Pure unit tests: no DB, no network, no async."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from no_human.core.merge_policy import (
    DEFAULT_POLICY,
    RULE_NAMES,
    GateFacts,
    PolicyLoad,
    PolicyVerdict,
    Rule,
    RuleVerdict,
    evaluate,
    evaluate_repo,
    load_policy,
)


def _facts(**kw) -> GateFacts:
    base = {"review_passed": True}
    base.update(kw)
    return GateFacts(**base)


def _policy(tmp_path: Path, text: str) -> Path:
    d = tmp_path / ".no_human"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "merge_policy.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------- #
# Loader (AC 2)
# --------------------------------------------------------------------- #


def test_absent_file_returns_default_policy_and_source_default(tmp_path):
    load = load_policy(tmp_path)
    assert load.rules == list(DEFAULT_POLICY)
    assert load.problems == []
    assert load.source == "default"


def test_absent_no_human_dir_does_not_raise(tmp_path):
    # no .no_human directory at all
    load = load_policy(tmp_path)
    assert load.source == "default"
    assert load.problems == []


def test_default_policy_is_the_six_documented_rules():
    names = [r.name for r in DEFAULT_POLICY]
    assert names == [
        "review_passed",
        "tests_ran_and_passed",
        "tamper_guard_clear",
        "repro_gate",
        "verifiers_all_satisfied",
        "ci",
    ]
    by_name = {r.name: r.arg for r in DEFAULT_POLICY}
    assert by_name["repro_gate"] == "pass_or_not_required"
    assert by_name["ci"] == "success_or_unknown"


def test_valid_file_parses_all_nine_rules_source_file(tmp_path):
    _policy(
        tmp_path,
        """
rules:
  - review_passed
  - no_advisory_findings
  - tests_ran_and_passed
  - tamper_guard_clear
  - repro_gate: pass_or_not_required
  - verifiers_all_satisfied
  - ci: success_or_unknown
  - paths_within: ["docs/**", "tests/**", "src/no_human/cli/**"]
  - max_changed_lines: 400
""",
    )
    load = load_policy(tmp_path)
    assert load.problems == []
    assert load.source == "file"
    assert [r.name for r in load.rules] == list(RULE_NAMES)
    by_name = {r.name: r.arg for r in load.rules}
    assert by_name["repro_gate"] == "pass_or_not_required"
    assert by_name["ci"] == "success_or_unknown"
    assert by_name["paths_within"] == ["docs/**", "tests/**", "src/no_human/cli/**"]
    assert by_name["max_changed_lines"] == 400


def test_invalid_yaml_records_problem_and_never_raises(tmp_path):
    _policy(tmp_path, "rules: [this is not: valid: yaml: at all")
    load = load_policy(tmp_path)
    assert load.source == "file"
    assert load.rules == list(DEFAULT_POLICY)
    assert len(load.problems) == 1


def test_top_level_not_mapping_records_problem(tmp_path):
    _policy(tmp_path, "- a\n- b\n")
    load = load_policy(tmp_path)
    assert load.source == "file"
    assert load.rules == list(DEFAULT_POLICY)
    assert any("mapping" in p for p in load.problems)


def test_missing_rules_key_records_problem(tmp_path):
    _policy(tmp_path, "other_key: 1\n")
    load = load_policy(tmp_path)
    assert load.rules == list(DEFAULT_POLICY)
    assert any("rules" in p for p in load.problems)


@pytest.mark.parametrize("body", ["rules: not-a-list\n", "rules:\n  a: 1\n  b: 2\n"])
def test_rules_not_a_list_records_problem(tmp_path, body):
    _policy(tmp_path, body)
    load = load_policy(tmp_path)
    assert load.rules == list(DEFAULT_POLICY)
    assert any("list" in p for p in load.problems)


def test_unknown_rule_name_records_problem(tmp_path):
    _policy(tmp_path, "rules:\n  - review_passed\n  - totally_bogus_rule\n")
    load = load_policy(tmp_path)
    assert load.source == "file"
    names = [r.name for r in load.rules]
    assert names == ["review_passed"]
    assert any("totally_bogus_rule" in p for p in load.problems)


@pytest.mark.parametrize(
    "body",
    [
        "rules:\n  - 5\n",
        "rules:\n  - null\n",
        "rules:\n  - review_passed: pass\n    extra: 1\n",
        "rules:\n  - {}\n",
    ],
)
def test_item_neither_string_nor_single_key_mapping_records_problem(tmp_path, body):
    _policy(tmp_path, body)
    load = load_policy(tmp_path)
    assert load.rules == []
    assert len(load.problems) == 1


def test_bare_name_for_rule_requiring_argument_records_problem(tmp_path):
    _policy(tmp_path, "rules:\n  - repro_gate\n")
    load = load_policy(tmp_path)
    assert load.rules == []
    assert any("repro_gate" in p for p in load.problems)


def test_argument_supplied_to_argumentless_rule_records_problem(tmp_path):
    _policy(tmp_path, "rules:\n  - review_passed: yes\n")
    load = load_policy(tmp_path)
    assert load.rules == []
    assert any("review_passed" in p for p in load.problems)


@pytest.mark.parametrize(
    "body",
    [
        'rules:\n  - max_changed_lines: "400"\n',
        "rules:\n  - max_changed_lines: true\n",
        'rules:\n  - paths_within: "docs/**"\n',
        "rules:\n  - paths_within: []\n",
    ],
)
def test_wrong_argument_type_records_problem(tmp_path, body):
    _policy(tmp_path, body)
    load = load_policy(tmp_path)
    assert load.rules == []
    assert len(load.problems) == 1


@pytest.mark.parametrize(
    "body",
    ["rules:\n  - ci: maybe\n", "rules:\n  - repro_gate: yes\n"],
)
def test_unknown_enum_argument_records_problem(tmp_path, body):
    _policy(tmp_path, body)
    load = load_policy(tmp_path)
    assert load.rules == []
    assert len(load.problems) == 1


def test_oversized_and_empty_file_record_problems(tmp_path):
    _policy(tmp_path, "rules: []\n" + ("# padding\n" * 20000))
    load = load_policy(tmp_path)
    assert load.rules == list(DEFAULT_POLICY)
    assert any("bytes" in p for p in load.problems)

    _policy(tmp_path, "")
    load2 = load_policy(tmp_path)
    assert load2.rules == list(DEFAULT_POLICY)
    assert any("empty" in p for p in load2.problems)


def test_load_policy_never_raises_on_directory_at_path(tmp_path):
    d = tmp_path / ".no_human" / "merge_policy.yaml"
    d.mkdir(parents=True)
    load = load_policy(tmp_path)
    assert load.source == "default"
    assert load.problems == []
    assert load.rules == list(DEFAULT_POLICY)


# --------------------------------------------------------------------- #
# Per-rule pass + fail (AC 3)
# --------------------------------------------------------------------- #


def _one(rule: Rule, facts: GateFacts) -> RuleVerdict:
    return evaluate([rule], facts).rules[0]


def test_review_passed_pass():
    v = _one(Rule("review_passed"), _facts(review_passed=True))
    assert v.passed


def test_review_passed_fail_on_false():
    v = _one(Rule("review_passed"), _facts(review_passed=False))
    assert not v.passed


def test_review_passed_fail_on_none():
    v = _one(Rule("review_passed"), _facts(review_passed=None))
    assert not v.passed


def test_no_advisory_findings_pass():
    v = _one(Rule("no_advisory_findings"), _facts(review_advisory_count=0))
    assert v.passed


def test_no_advisory_findings_fail():
    v = _one(Rule("no_advisory_findings"), _facts(review_advisory_count=2))
    assert not v.passed


def test_tests_ran_and_passed_pass():
    v = _one(
        Rule("tests_ran_and_passed"),
        _facts(tests_ran=True, tests_failed=0, tests_passed=312),
    )
    assert v.passed
    assert "312" in v.detail


def test_tests_ran_and_passed_fail_never_ran():
    v = _one(Rule("tests_ran_and_passed"), _facts(tests_ran=False))
    assert not v.passed


def test_tests_ran_and_passed_fail_on_failures():
    v = _one(
        Rule("tests_ran_and_passed"),
        _facts(tests_ran=True, tests_failed=1, tests_passed=311),
    )
    assert not v.passed


def test_tests_ran_and_passed_fail_zero_passed():
    v = _one(
        Rule("tests_ran_and_passed"),
        _facts(tests_ran=True, tests_failed=0, tests_passed=0),
    )
    assert not v.passed


def test_tamper_guard_clear_pass_not_fired():
    v = _one(Rule("tamper_guard_clear"), _facts(tamper_fired=False))
    assert v.passed


def test_tamper_guard_clear_pass_fired_waived():
    v = _one(
        Rule("tamper_guard_clear"), _facts(tamper_fired=True, tamper_waived=True)
    )
    assert v.passed


def test_tamper_guard_clear_fail_fired_unwaived():
    v = _one(
        Rule("tamper_guard_clear"), _facts(tamper_fired=True, tamper_waived=False)
    )
    assert not v.passed


def test_verifiers_all_satisfied_pass_zero_ran():
    v = _one(Rule("verifiers_all_satisfied"), _facts(verifiers_ran=0))
    assert v.passed
    assert v.detail == "0 verifiers ran"


def test_verifiers_all_satisfied_pass_none_failed():
    v = _one(
        Rule("verifiers_all_satisfied"), _facts(verifiers_ran=4, verifiers_failed=())
    )
    assert v.passed


def test_verifiers_all_satisfied_fail_names_verifier():
    v = _one(
        Rule("verifiers_all_satisfied"),
        _facts(verifiers_ran=1, verifiers_failed=("v1",)),
    )
    assert not v.passed
    assert "v1" in v.detail


# --------------------------------------------------------------------- #
# repro_gate, both modes
# --------------------------------------------------------------------- #


def test_repro_gate_pass_mode_passes_on_pass():
    v = _one(Rule("repro_gate", "pass"), _facts(repro_verdict="pass"))
    assert v.passed


def test_repro_gate_pass_mode_fails_on_none():
    v = _one(Rule("repro_gate", "pass"), _facts(repro_verdict=None))
    assert not v.passed


def test_repro_gate_pass_or_not_required_passes_on_pass():
    v = _one(
        Rule("repro_gate", "pass_or_not_required"), _facts(repro_verdict="pass")
    )
    assert v.passed


def test_repro_gate_pass_or_not_required_passes_not_required_none():
    v = _one(
        Rule("repro_gate", "pass_or_not_required"),
        _facts(repro_verdict=None, repro_required=False),
    )
    assert v.passed


def test_repro_gate_pass_or_not_required_passes_not_required_waived():
    v = _one(
        Rule("repro_gate", "pass_or_not_required"),
        _facts(repro_verdict="waived", repro_required=False),
    )
    assert v.passed


def test_repro_gate_pass_or_not_required_fails_required_waived():
    v = _one(
        Rule("repro_gate", "pass_or_not_required"),
        _facts(repro_verdict="waived", repro_required=True),
    )
    assert not v.passed


@pytest.mark.parametrize("verdict", ["fail", "error"])
def test_repro_gate_pass_or_not_required_fails_fail_and_error(verdict):
    v = _one(
        Rule("repro_gate", "pass_or_not_required"), _facts(repro_verdict=verdict)
    )
    assert not v.passed


# --------------------------------------------------------------------- #
# ci, both modes
# --------------------------------------------------------------------- #


def test_ci_success_mode_passes_on_success():
    v = _one(Rule("ci", "success"), _facts(ci_state="success"))
    assert v.passed


def test_ci_success_mode_fails_on_unknown():
    v = _one(Rule("ci", "success"), _facts(ci_state="unknown"))
    assert not v.passed


def test_ci_success_mode_fails_on_none():
    v = _one(Rule("ci", "success"), _facts(ci_state=None))
    assert not v.passed


@pytest.mark.parametrize("state", ["success", "unknown", None])
def test_ci_success_or_unknown_mode_passes(state):
    v = _one(Rule("ci", "success_or_unknown"), _facts(ci_state=state))
    assert v.passed


@pytest.mark.parametrize("state", ["failure", "pending"])
def test_ci_success_or_unknown_mode_fails(state):
    v = _one(Rule("ci", "success_or_unknown"), _facts(ci_state=state))
    assert not v.passed


# --------------------------------------------------------------------- #
# paths_within
# --------------------------------------------------------------------- #


def test_docs_star_does_not_match_nested_docs_a_b_md():
    v = _one(
        Rule("paths_within", ["docs/*"]), _facts(changed_paths=("docs/a/b.md",))
    )
    assert not v.passed


def test_src_double_star_matches_src_a_b_c_py():
    v = _one(
        Rule("paths_within", ["src/**"]), _facts(changed_paths=("src/a/b/c.py",))
    )
    assert v.passed


def test_paths_within_positive_all_match():
    v = _one(
        Rule("paths_within", ["docs/**", "tests/**"]),
        _facts(changed_paths=("docs/x.md", "tests/test_a.py")),
    )
    assert v.passed
    assert "2" in v.detail


def test_paths_within_negative_names_offenders():
    v = _one(
        Rule("paths_within", ["docs/**"]),
        _facts(changed_paths=("docs/x.md", "src/x.py", "web/y.jsx")),
    )
    assert not v.passed
    assert "2 paths outside the allowed set" in v.detail
    assert "src/x.py" in v.detail
    assert "web/y.jsx" in v.detail


def test_paths_within_empty_changed_paths_passes():
    v = _one(Rule("paths_within", ["docs/**"]), _facts(changed_paths=()))
    assert v.passed
    assert v.detail == "no changed paths"


# --------------------------------------------------------------------- #
# max_changed_lines boundary
# --------------------------------------------------------------------- #


def test_max_changed_lines_equal_arg_passes():
    v = _one(Rule("max_changed_lines", 400), _facts(changed_lines=400))
    assert v.passed


def test_max_changed_lines_arg_plus_one_fails():
    v = _one(Rule("max_changed_lines", 400), _facts(changed_lines=401))
    assert not v.passed


def test_max_changed_lines_zero_lines_passes():
    v = _one(Rule("max_changed_lines", 400), _facts(changed_lines=0))
    assert v.passed


# --------------------------------------------------------------------- #
# ready / fail-closed (AC 4)
# --------------------------------------------------------------------- #


def test_ready_false_when_any_rule_fails():
    verdict = evaluate(
        [Rule("review_passed"), Rule("tests_ran_and_passed")],
        _facts(review_passed=True, tests_ran=False),
    )
    assert not verdict.ready


def test_evaluate_repo_unknown_rule_name_never_ready(tmp_path):
    _policy(tmp_path, "rules:\n  - review_passed\n  - bogus_rule\n")
    verdict = evaluate_repo(tmp_path, _facts(review_passed=True))
    assert not verdict.ready
    assert verdict.problems


def test_ready_false_when_rules_empty():
    verdict = evaluate([], _facts(review_passed=True))
    assert not verdict.ready


def test_ready_true_when_all_pass_and_no_problems():
    verdict = evaluate(
        [Rule("review_passed"), Rule("tests_ran_and_passed")],
        _facts(review_passed=True, tests_ran=True, tests_failed=0, tests_passed=5),
    )
    assert verdict.ready


# --------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------- #


def test_summary_ready_shape():
    facts = _facts(
        review_passed=True,
        tests_ran=True,
        tests_failed=0,
        tests_passed=1,
        repro_verdict="pass",
        ci_state="success",
    )
    verdict = evaluate(list(DEFAULT_POLICY), facts)
    assert verdict.ready
    assert verdict.summary == "ready — 6 of 6 rules satisfied"


def test_summary_not_ready_names_failed_rules_in_order():
    facts = _facts(
        review_passed=True,
        tests_ran=False,
        repro_verdict="pass",
        ci_state="failure",
    )
    verdict = evaluate(list(DEFAULT_POLICY), facts)
    assert not verdict.ready
    assert verdict.summary == (
        "not ready — 2 of 6 rules failed: tests_ran_and_passed, ci"
    )


def test_as_dict_round_trips_through_json_dumps():
    facts = _facts(review_passed=True)
    verdict = evaluate([Rule("review_passed")], facts)
    d = verdict.as_dict()
    assert json.loads(json.dumps(d)) == d
    assert set(d.keys()) == {"ready", "summary", "source", "problems", "rules"}
    for rule_dict in d["rules"]:
        assert set(rule_dict.keys()) == {"name", "passed", "detail"}


def test_evaluate_repo_carries_source_and_problems_through(tmp_path):
    default_verdict = evaluate_repo(tmp_path, _facts(review_passed=True))
    assert default_verdict.source == "default"
    assert default_verdict.problems == ()

    _policy(tmp_path, "rules:\n  - review_passed\n  - bogus\n")
    file_verdict = evaluate_repo(tmp_path, _facts(review_passed=True))
    assert file_verdict.source == "file"
    assert file_verdict.problems


# --------------------------------------------------------------------- #
# Contract guards
# --------------------------------------------------------------------- #


def test_import_surface():
    import no_human.core.merge_policy as mp

    for name in (
        "Rule",
        "RULE_NAMES",
        "DEFAULT_POLICY",
        "PolicyLoad",
        "GateFacts",
        "RuleVerdict",
        "PolicyVerdict",
        "load_policy",
        "evaluate",
        "evaluate_repo",
    ):
        assert hasattr(mp, name), name


def test_rule_names_has_nine_entries_no_duplicates_is_a_tuple():
    assert isinstance(RULE_NAMES, tuple)
    assert len(RULE_NAMES) == 9
    assert len(set(RULE_NAMES)) == 9
    assert isinstance(DEFAULT_POLICY, tuple)
    for r in DEFAULT_POLICY:
        assert r.name in RULE_NAMES


_VALID_ARG_FOR = {
    "repro_gate": "pass",
    "ci": "success",
    "paths_within": ["**"],
    "max_changed_lines": 100,
}


def test_every_rule_name_is_evaluable():
    facts = _facts(
        review_passed=True,
        tests_ran=True,
        tests_passed=1,
        repro_verdict="pass",
        ci_state="success",
        changed_paths=("a.py",),
        changed_lines=5,
    )
    for name in RULE_NAMES:
        rule = Rule(name, _VALID_ARG_FOR.get(name))
        verdict = evaluate([rule], facts).rules[0]
        assert isinstance(verdict, RuleVerdict)
        assert verdict.detail


def test_docstring_states_no_code_path_merges():
    import no_human.core.merge_policy as mp

    doc = mp.__doc__ or ""
    assert "nh approve" in doc
    assert "merge" in doc.lower()
    assert "advisory" in doc.lower()


def test_policy_load_and_gate_facts_dataclasses_smoke():
    load = PolicyLoad(rules=[Rule("review_passed")], problems=[], source="default")
    assert isinstance(load, PolicyLoad)
    facts = GateFacts(review_passed=None)
    assert facts.tests_ran is False
    assert isinstance(evaluate([], facts), PolicyVerdict)
