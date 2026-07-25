"""Guard: eval/reviewer_recall/ is never read outside eval/ and the runner.

docs/REVIEWER_RECALL_METHOD.md, "Corpus design": nothing under
eval/reviewer_recall/ may ever be imported by prompt-construction, few-shot,
tuning, or intake code. src/no_human/cli/commands.py is the one explicit
exception (SCRUM-29 CLI wiring, `nh bench report --reviewer-recall`), and
even that file must only load/dispatch to the runner module — never read the
corpus files (base.ref/change.diff/truth.json) directly.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILE = REPO_ROOT / "src" / "no_human" / "cli" / "commands.py"
NEEDLES = ("reviewer_recall", "reviewer-recall")
CORPUS_FILE_NAMES = ("base.ref", "change.diff", "truth.json")


def _all_src_py_files():
    return sorted((REPO_ROOT / "src").rglob("*.py"))


def test_no_module_outside_eval_references_reviewer_recall():
    offenders = []
    for path in _all_src_py_files():
        if path == ALLOWED_FILE:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(n in text for n in NEEDLES):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "these src/no_human modules reference eval/reviewer_recall — only "
        f"the CLI wiring file may: {offenders}"
    )


def test_cli_wiring_never_reads_corpus_files_directly():
    text = ALLOWED_FILE.read_text(encoding="utf-8", errors="ignore")
    offenders = [n for n in CORPUS_FILE_NAMES if n in text]
    assert offenders == [], (
        "the CLI wiring file must only load the runner module and never "
        f"read corpus files directly, but references: {offenders}"
    )


def test_cli_wiring_does_reference_the_runner():
    text = ALLOWED_FILE.read_text(encoding="utf-8", errors="ignore")
    assert "reviewer_recall" in text and "--reviewer-recall" in text, (
        "the required wiring (flag + runner path) is missing from the CLI file"
    )
