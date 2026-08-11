"""TaskSpec.from_plan: parsing the planner's markdown output into a spec."""
from __future__ import annotations

import pytest

from no_human.core.task import TaskSpec

# Exact FILES TO CHANGE/CREATE section from task 7a2160d3b91b49afb175483af9e1b544
# (read from the live DB's task_events, no_human.db) — the planner used an
# index-column table shape (`| # | Path | Change |`); `_table_files` used to
# take `cells[0]` unconditionally and capture the index ('#', '1'..'4')
# instead of the path.
_TASK_7A2160D3_PLAN = (
    "## FILES TO CHANGE/CREATE\n\n"
    "| # | Path | Change |\n"
    "|---|---|---|\n"
    "| 1 | `src/no_human/agent/venv_install_guard.py` | **NEW** — structural ... |\n"
    "| 2 | `src/no_human/agent/guard.py` | Wire it: `import`, ... |\n"
    "| 3 | `tests/test_venv_install_guard.py` | **NEW** — the three ... |\n"
    "| 4 | `EXPORT_CLASSIFICATION.txt` / `RELEASE_MANIFEST.txt` | Only if ... |\n"
)

_JUNK_ENTRIES = ["#", "1", "42", "", "   ", "-", "---", "N/A", "|"]

# Reviewer-cited counterexamples (attempt 4's review FAIL on 3be5cfec): prose
# tokens that merely contain a dot-suffix and used to be admitted as "paths"
# by the old bare `_EXTENSION_RE` search.
_PROSE_DOT_TOKENS = [
    "v2.1", "e.g.", "i.e.", "No.4", "1.2.3", "etc.", "vs.", "Ph.D.",
    "approx.", "and/or", "N/A",
]


def test_from_plan_parses_bullet_list_sections():
    plan = (
        "## FILES TO CHANGE/CREATE\n"
        "- src/foo.py\n"
        "- src/bar.py\n\n"
        "## APPROACH\n"
        "Do the thing.\n\n"
        "## TEST PLAN\n"
        "Run pytest.\n\n"
        "## OUT OF SCOPE\n"
        "- src/baz.py\n\n"
        "## VERIFICATION\n"
        "pytest -x\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert spec.files_to_change == ["src/foo.py", "src/bar.py"]
    assert spec.approach == "Do the thing."
    assert spec.test_plan == "Run pytest."
    assert spec.out_of_scope == ["src/baz.py"]
    assert spec.verification == "pytest -x"


def test_from_plan_parses_markdown_table_files_section():
    """The planner sometimes emits FILES TO CHANGE/CREATE as a markdown table
    instead of a bullet list — the parser must extract file paths from the
    first column, and must not mistake a "---" separator line (or the table's
    own header-separator row) for an (empty) bullet item."""
    plan = (
        "## FILES TO CHANGE/CREATE\n\n"
        "| File | Change |\n"
        "|------|--------|\n"
        "| `Jenkinsfile` | Add a new stage |\n"
        "| `.no_human/project.yml` | Add a credential |\n\n"
        "---\n\n"
        "## APPROACH\n"
        "Do the thing.\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert spec.files_to_change == ["Jenkinsfile", ".no_human/project.yml"]


def test_from_plan_hr_separator_does_not_produce_empty_bullet():
    """A bare '---' line inside a bullet-list section must not be parsed as
    an empty bullet item."""
    plan = (
        "## OUT OF SCOPE\n"
        "- src/baz.py\n"
        "---\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert spec.out_of_scope == ["src/baz.py"]


def test_from_plan_missing_sections_default_empty():
    spec = TaskSpec.from_plan("no headings here")
    assert spec.files_to_change == []
    assert spec.approach == ""
    assert spec.test_plan == ""
    assert spec.out_of_scope == []
    assert spec.verification == ""


def test_from_plan_index_column_table_yields_paths_not_row_numbers():
    """Live repro, task 7a2160d3: an index-column table (`| # | Path |
    Change |`) must yield the real paths, never the row markers '#'/'1'..'4'.
    """
    spec = TaskSpec.from_plan(_TASK_7A2160D3_PLAN)
    assert "#" not in spec.files_to_change
    for marker in ("1", "2", "3", "4"):
        assert marker not in spec.files_to_change
    assert "src/no_human/agent/venv_install_guard.py" in spec.files_to_change
    assert "src/no_human/agent/guard.py" in spec.files_to_change
    assert "tests/test_venv_install_guard.py" in spec.files_to_change
    assert any(
        "EXPORT_CLASSIFICATION.txt" in f and "RELEASE_MANIFEST.txt" in f
        for f in spec.files_to_change
    )
    assert len(spec.files_to_change) == 4


@pytest.mark.parametrize("junk", _JUNK_ENTRIES)
def test_junk_entries_never_enter_files_to_change(junk):
    """Row markers, bare integers, blank cells and stray punctuation must
    never enter files_to_change — as a bullet item or as a table cell."""
    bullet_plan = f"## FILES TO CHANGE/CREATE\n- {junk}\n"
    assert junk not in TaskSpec.from_plan(bullet_plan).files_to_change

    table_plan = (
        "## FILES TO CHANGE/CREATE\n\n"
        "| Col | Desc |\n"
        "|---|---|\n"
        f"| {junk} | description |\n"
    )
    assert junk not in TaskSpec.from_plan(table_plan).files_to_change


def test_table_with_no_path_shaped_cells_yields_empty_list():
    """A table of pure prose (no path-shaped cell anywhere) must yield an
    empty list — junk in files_to_change is worse than an empty field."""
    plan = (
        "## FILES TO CHANGE/CREATE\n\n"
        "| # | Step |\n"
        "|---|---|\n"
        "| 1 | Update the changelog |\n"
        "| 2 | Notify the team |\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert spec.files_to_change == []


def test_bullet_items_with_descriptions_preserved_verbatim():
    """Bullet items with trailing descriptions round-trip unchanged — the
    filter only decides membership, it never rewrites the item text."""
    plan = (
        "## FILES TO CHANGE/CREATE\n"
        "- calc.py: add mul()\n"
        "- `src/no_human/x.py` — the fix\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert "calc.py: add mul()" in spec.files_to_change
    assert "`src/no_human/x.py` — the fix" in spec.files_to_change


@pytest.mark.parametrize("prose", _PROSE_DOT_TOKENS)
def test_prose_tokens_never_enter_files_to_change(prose):
    """Reviewer's cited counterexamples (attempt 4 review FAIL): a token that
    merely CONTAINS a dot-suffix — a version number, an abbreviation like
    'e.g.'/'i.e.'/'No.4', or a prose slashism like 'and/or'/'N/A' — is never
    a path, whether it arrives as a bullet item or an index-table cell."""
    bullet_plan = f"## FILES TO CHANGE/CREATE\n- {prose}\n"
    assert prose not in TaskSpec.from_plan(bullet_plan).files_to_change

    table_plan = f"## FILES TO CHANGE/CREATE\n\n| 1 | {prose} | desc |\n"
    assert prose not in TaskSpec.from_plan(table_plan).files_to_change


def test_prose_table_with_version_numbers_yields_empty_list():
    """A '| # | Step |' table whose rows are pure prose containing version
    numbers/abbreviations (no path-shaped cell anywhere) must yield an empty
    list, not the prose text."""
    plan = (
        "## FILES TO CHANGE/CREATE\n\n"
        "| # | Step |\n"
        "|---|---|\n"
        "| 1 | Bump the pin to v2.1 |\n"
        "| 2 | See §3.2, e.g. the retry path |\n"
    )
    spec = TaskSpec.from_plan(plan)
    assert spec.files_to_change == []


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.py",
        "tests/test_x.py",
        "Jenkinsfile",
        "Makefile",
        ".gitignore",
        ".no_human/project.yml",
        "EXPORT_CLASSIFICATION.txt",
        "web/src/eventRoles.js",
        "src/no_human/agent",
    ],
)
def test_real_paths_still_admitted(path):
    """Controls: real paths (with known extensions, extension-less known
    filenames, dotfiles, and slash-only directory paths) must still survive
    the tightened path-shape predicate."""
    plan = f"## FILES TO CHANGE/CREATE\n- {path}\n"
    assert path in TaskSpec.from_plan(plan).files_to_change
