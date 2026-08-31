"""D1.2: `config.ui_evidence_should_run` — the diff-aware default that decides
whether the harness runs the UI-evidence browser walk for one attempt.

Three states of `config["ui_evidence"]["enabled"]`:
  * ``None`` (the shipped default) — follow the diff: ON for `web/**`/
    `desktop/**` (plus any repo-declared `ui_paths` extras), OFF otherwise.
  * ``True``/``False`` — an operator override, forcing the same answer for
    EVERY attempt regardless of the diff (the master kill switch).
"""
from __future__ import annotations

import copy

from no_human.config import DEFAULT_CONFIG, ui_evidence_should_run


def _config(**overrides) -> dict:
    data = copy.deepcopy(DEFAULT_CONFIG)
    if overrides:
        data.setdefault("ui_evidence", {}).update(overrides)
    return data


def test_default_is_off_for_a_diff_that_never_touches_ui():
    cfg = _config()
    assert ui_evidence_should_run(cfg, ["src/no_human/core/widget.py"]) is False


def test_default_is_on_for_a_diff_touching_web():
    cfg = _config()
    assert ui_evidence_should_run(cfg, ["web/src/App.jsx"]) is True


def test_default_is_on_for_a_diff_touching_desktop():
    cfg = _config()
    assert ui_evidence_should_run(cfg, ["desktop/main.js"]) is True


def test_default_is_off_for_an_empty_or_missing_diff():
    cfg = _config()
    assert ui_evidence_should_run(cfg, []) is False
    assert ui_evidence_should_run(cfg, None) is False


def test_a_mixed_diff_with_one_ui_path_is_enough():
    cfg = _config()
    changed = ["src/no_human/core/widget.py", "web/src/App.jsx"]
    assert ui_evidence_should_run(cfg, changed) is True


def test_repo_declared_ui_paths_extend_the_default_globs():
    cfg = _config()
    changed = ["frontend/Widget.vue"]
    assert ui_evidence_should_run(cfg, changed) is False
    assert ui_evidence_should_run(
        cfg, changed, extra_globs=["frontend/**"]) is True


def test_explicit_true_forces_on_regardless_of_the_diff():
    cfg = _config(enabled=True)
    assert ui_evidence_should_run(cfg, []) is True
    assert ui_evidence_should_run(cfg, ["src/no_human/core/widget.py"]) is True


def test_explicit_false_forces_off_regardless_of_a_ui_touching_diff():
    cfg = _config(enabled=False)
    assert ui_evidence_should_run(cfg, ["web/src/App.jsx"]) is False
    assert ui_evidence_should_run(cfg, ["desktop/main.js"]) is False


def test_a_lookalike_path_that_is_not_actually_under_web_or_desktop_stays_off():
    """`fnmatch` globs, not a substring test — `webapp/` is not `web/`."""
    cfg = _config()
    assert ui_evidence_should_run(cfg, ["webapp/src/App.jsx"]) is False
    assert ui_evidence_should_run(cfg, ["not-desktop/main.js"]) is False


def test_default_config_ships_the_none_sentinel_not_a_bare_false():
    """Regression pin: the sentinel must be `None`, not `False` — a bare
    `False` here would make `ui_evidence_should_run` always read it as an
    explicit override and never fall through to the diff-aware default."""
    assert DEFAULT_CONFIG["ui_evidence"]["enabled"] is None
