"""Tests for the single shared glob matcher (`core/pathglob.py`).

Before this module there were three independent glob-to-regex translators
(`core/merge_policy.py`, `review/verifiers.py`, `core/project_config.
_is_scoped_glob`) that had drifted to slightly different semantics by
accident. `core/pathglob.py` is now the single implementation the first two
delegate to; `project_config._is_scoped_glob` stays separate on purpose (a
different question — "is this glob scoped under a subtree", not "does this
path match this glob"). This file locks in the matcher's semantics AND
proves (structurally, not just behaviourally) that no second translator has
grown back anywhere in `src/no_human`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import no_human.core.pathglob as pathglob
import no_human.core.merge_policy as merge_policy
import no_human.review.verifiers as verifiers
from no_human.core.pathglob import compile_glob, matches, normalize_path


# --------------------------------------------------------------------- #
# ** crosses directories, * does not
# --------------------------------------------------------------------- #


def test_star_does_not_cross_a_slash():
    assert not matches("docs/a/b.md", ["docs/*"])


def test_star_matches_within_one_segment():
    assert matches("docs/x.md", ["docs/*"])


def test_trailing_double_star_matches_the_directory_itself_file():
    assert matches("docs/x.md", ["docs/**"])


def test_trailing_double_star_matches_a_nested_file():
    assert matches("docs/a/b.md", ["docs/**"])


def test_non_trailing_double_star_matches_zero_segments():
    assert matches("src/main.py", ["src/**/main.py"])


def test_non_trailing_double_star_matches_many_segments():
    assert matches("src/a/b/main.py", ["src/**/main.py"])


def test_docs_star_does_not_match_a_nested_path():
    assert not matches("docs/a/b.md", ["docs/*.md"])


def test_docs_star_matches_a_direct_child():
    assert matches("docs/a.md", ["docs/*.md"])


# --------------------------------------------------------------------- #
# character classes
# --------------------------------------------------------------------- #


def test_character_class_matches_included_char():
    assert matches("file_a.txt", ["file_[ab].txt"])
    assert matches("file_b.txt", ["file_[ab].txt"])


def test_character_class_rejects_excluded_char():
    assert not matches("file_c.txt", ["file_[ab].txt"])


def test_negated_character_class_excludes_the_named_char():
    assert not matches("file_a.txt", ["file_[!a].txt"])


def test_negated_character_class_matches_other_chars():
    assert matches("file_z.txt", ["file_[!a].txt"])


# --------------------------------------------------------------------- #
# ? matches exactly one non-slash char
# --------------------------------------------------------------------- #


def test_question_mark_matches_exactly_one_char():
    assert matches("a.txt", ["?.txt"])


def test_question_mark_does_not_match_two_chars():
    assert not matches("ab.txt", ["?.txt"])


def test_question_mark_does_not_cross_a_slash():
    assert not matches("a/b.txt", ["a?b.txt"])


# --------------------------------------------------------------------- #
# normalize_path
# --------------------------------------------------------------------- #


def test_normalize_strips_leading_dot_slash():
    assert normalize_path("./a/b") == "a/b"


def test_normalize_strips_leading_slash():
    assert normalize_path("/a/b") == "a/b"


def test_normalize_collapses_double_slash():
    assert normalize_path("a//b") == "a/b"


def test_normalize_converts_backslashes():
    assert normalize_path("a\\b") == "a/b"


def test_normalize_is_idempotent_on_already_clean_path():
    assert normalize_path("a/b") == "a/b"


def test_matches_normalizes_the_candidate_path_before_comparing():
    assert matches("./docs/x.md", ["docs/*.md"])
    assert matches("/docs/x.md", ["docs/*.md"])
    assert matches("docs\\x.md", ["docs/*.md"])


# --------------------------------------------------------------------- #
# fail-soft: never raise
# --------------------------------------------------------------------- #


def test_unterminated_character_class_falls_back_to_a_literal_match():
    # `[ab` never closes; the whole segment becomes a literal, so it only
    # matches that exact literal string, never raises, and does not match
    # some unrelated path.
    rx = compile_glob("file_[ab.txt")
    assert rx is not None
    assert rx.fullmatch("file_[ab.txt")
    assert not matches("file_a.txt", ["file_[ab.txt"])


def test_unterminated_character_class_does_not_raise_via_matches():
    assert matches("file_[ab.txt", ["file_[ab.txt"])


def test_uncompilable_pattern_matches_nothing_and_does_not_raise():
    # `[z-a]` is a backwards character range: `_translate_segment` passes
    # the class through verbatim (it only rewrites a leading `!`), so it
    # reaches `re.compile` and raises `re.error: bad character range`
    # there. `compile_glob` catches that and caches `None`; `matches` must
    # then report "no match" rather than propagating the exception — an
    # untrusted, hand-edited glob list must never crash a gate.
    assert compile_glob("file_[z-a].txt") is None
    assert matches("file_x.txt", ["file_[z-a].txt"]) is False


def test_compile_glob_caches_by_pattern_string():
    a = compile_glob("docs/*.md")
    b = compile_glob("docs/*.md")
    assert a is b


# --------------------------------------------------------------------- #
# delegation: both call sites use pathglob.matches, not a private copy
# --------------------------------------------------------------------- #


def test_merge_policy_paths_within_delegates_to_pathglob_matches(monkeypatch):
    calls = []
    real = pathglob.matches

    def spy(path, patterns):
        calls.append((path, tuple(patterns)))
        return real(path, patterns)

    monkeypatch.setattr(merge_policy.pathglob, "matches", spy)
    ok, _detail = merge_policy._check_paths_within(
        merge_policy.GateFacts(
            review_passed=True, changed_paths=("docs/x.md",)
        ),
        ["docs/**"],
    )
    assert ok is True
    assert calls, "merge_policy._check_paths_within did not call pathglob.matches"
    assert calls[0][0] == "docs/x.md"


def test_verifiers_matches_delegates_to_pathglob_matches(monkeypatch):
    calls = []
    real = pathglob.matches

    def spy(path, patterns):
        calls.append((path, tuple(patterns)))
        return real(path, patterns)

    monkeypatch.setattr(verifiers.pathglob, "matches", spy)
    result = verifiers._matches("docs/x.md", ["docs/**"])
    assert result is True
    assert calls, "verifiers._matches did not call pathglob.matches"
    assert calls[0][0] == "docs/x.md"


# --------------------------------------------------------------------- #
# structural guard: no second glob->regex translator exists in src/
# --------------------------------------------------------------------- #


def test_no_second_glob_translator_in_src():
    """`core/pathglob.py`'s within-segment translator emits the exact
    string literal ``"[^/]*"`` (the regex for "a run of non-slash chars",
    the signature output of a `*`-within-a-segment glob translator) — see
    `_translate_segment`. This scans every module under `src/no_human` for
    a string CONSTANT (not a substring of some larger regex, which would
    false-positive on e.g. a `Jenkinsfile[^/]*$` literal regex elsewhere)
    equal to exactly ``"[^/]*"``. Only `core/pathglob.py` may define one —
    a hit anywhere else means a second translator has grown back.

    `project_config._is_scoped_glob` is allowlisted by construction: it
    never builds a regex from a glob at all (it only checks whether the
    leading path segment is wildcard-free), so it structurally cannot
    contain this marker; no explicit exclusion is needed, and the scan
    below asserts that remains true rather than special-casing the file.

    MUTATION PROOF: reintroducing a hand-rolled `*` -> `[^/]*` translator
    in, say, `review/verifiers.py` (instead of delegating to
    `pathglob.matches`) makes this test fail.
    """
    src_root = Path(pathglob.__file__).resolve().parent.parent  # src/no_human
    assert src_root.name == "no_human"
    offenders = []
    for f in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "[^/]*"
            ):
                offenders.append(str(f.relative_to(src_root)))
    assert offenders == ["core/pathglob.py"], (
        "expected the glob->regex translator marker only in core/pathglob.py, "
        f"found it in: {offenders}"
    )
