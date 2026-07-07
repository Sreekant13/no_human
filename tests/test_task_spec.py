"""TaskSpec.from_plan: parsing the planner's markdown output into a spec."""
from __future__ import annotations

from no_human.core.task import TaskSpec


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
