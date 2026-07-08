"""Tests for lenient JSON parsing of LLM output (no_human.core.jsonparse)."""

import json

import pytest

from no_human.core.jsonparse import loads_lenient, repair_json_escapes


def test_valid_json_unchanged():
    src = '{"a": 1, "b": "hello\\nworld", "c": [true, null]}'
    assert loads_lenient(src) == json.loads(src)


def test_repair_leaves_valid_escapes_intact():
    # Contains a valid \n and a valid \\ and a valid \uXXXX — must be untouched.
    src = '{"s": "line1\\nline2 \\\\ end \\u00e9"}'
    assert repair_json_escapes(src) == src
    assert loads_lenient(src) == {"s": "line1\nline2 \\ end \u00e9"}


def test_invalid_escape_from_path_is_recovered():
    # The exact failure class: a Jenkins/URL path with a stray backslash that
    # is not a valid JSON escape ("\j", "\e"). Strict json.loads rejects this.
    bad = '{"evidence": "fetches from \\job\\enrichment\\1\\artifact"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    data = loads_lenient(bad)
    assert data["evidence"] == r"fetches from \job\enrichment\1\artifact"


def test_invalid_regex_escape_is_recovered():
    bad = '{"pattern": "\\d+ and \\w*"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    assert loads_lenient(bad) == {"pattern": r"\d+ and \w*"}


def test_bad_unicode_escape_is_recovered():
    # \u not followed by 4 hex digits is invalid and must be repaired.
    bad = '{"s": "temp \\unit test"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    assert loads_lenient(bad) == {"s": r"temp \unit test"}


def test_unrepairable_still_raises():
    # Missing closing brace is a structural error repair can't fix.
    with pytest.raises(json.JSONDecodeError):
        loads_lenient('{"a": 1')
