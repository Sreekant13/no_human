"""Guards on the reviewer-recall instrument: its corpus, and its measurement.

Part 1 — the corpus is never read outside eval/ and the runner.
docs/REVIEWER_RECALL_METHOD.md, "Corpus design": nothing under
eval/reviewer_recall/ may ever be imported by prompt-construction, few-shot,
tuning, or intake code. src/no_human/cli/commands.py is the one explicit
exception (SCRUM-29 CLI wiring, `nh bench report --reviewer-recall`), and
even that file must only load/dispatch to the runner module — never read the
corpus files (base.ref/change.diff/truth.json) directly.

Part 2 (R9) — a catch-rate belongs to the model it was measured on. Nothing
forced anyone to notice when `llm.review_model` moved: the Opus tier changed on
2026-07-26, the last full measurement had run on the previous model, and the
only thing recording that mismatch was a paragraph of prose nobody had to read.
The tests below pin the CURRENT, TRUTHFUL state — measured model, shipping
model, and the fact that no catch-rate is published while they differ — and go
red on drift in EITHER file, so the next model change lands with this test's
failure message naming the run that has to happen.

They deliberately publish nothing. A test cannot green-light a number; if the
shipping model and the measured model agree again, that must happen because
somebody ran the tool.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILE = REPO_ROOT / "src" / "no_human" / "cli" / "commands.py"
NEEDLES = ("reviewer_recall", "reviewer-recall")
CORPUS_FILE_NAMES = ("base.ref", "change.diff", "truth.json")

METHOD_DOC = REPO_ROOT / "docs" / "REVIEWER_RECALL_METHOD.md"
# The two claims the doc makes about models, read as data rather than trusted as
# prose. Newlines are ordinary whitespace here: the sentence is hard-wrapped and
# the wrap point moves with any edit.
_MEASURED_RE = re.compile(r"last full measurement\s+ran on `([^`]+)`")
_SHIPPING_RE = re.compile(r"shipping reviewer has been `([^`]+)`")
_NO_NUMBER = "No catch-rate is currently published on any user-facing surface."
_NOT_REMEASURED = "has not been re-measured"
_REMEDY = (
    "Re-measure with `nh bench report --reviewer-recall` and publish the result "
    "in docs/REVIEWER_RECALL_METHOD.md with denominator, date and model id. "
    "Do NOT re-attribute the previous model's figure to the new one."
)


def _method_doc_text() -> str:
    return METHOD_DOC.read_text(encoding="utf-8")


def _shipped_review_model() -> str:
    """The review model a fresh install actually gets, read the way a user gets
    it — through ``load_config``'s generated default, not by importing a dict."""
    import tempfile

    from no_human.config import load_config

    with tempfile.TemporaryDirectory() as tmp:
        return load_config(Path(tmp) / "config.yaml").review_model


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


def test_cli_loader_actually_loads_the_runner():
    """The stubbed runner tests never exercised the CLI's file-path loader;
    the first live run died in dataclass processing because the module was
    never registered in sys.modules before exec. Load it for real."""
    from no_human.cli.commands import _load_reviewer_recall_runner

    module, repo_root = _load_reviewer_recall_runner()
    assert callable(module.run_and_report)
    assert (repo_root / "eval" / "reviewer_recall" / "cases").is_dir()


# --------------------------------------------------------------------------- #
# R9 — a model change must force a re-measure                                  #
# --------------------------------------------------------------------------- #

def test_the_method_doc_still_states_both_models_in_a_readable_form():
    """Non-vacuity, first. Every assertion below reads two model ids out of the
    doc with a regex; if a rewrite moves the sentence, those reads return None
    and the drift gate silently passes forever. This test is what fails
    instead."""
    text = _method_doc_text()
    measured = _MEASURED_RE.search(text)
    shipping = _SHIPPING_RE.search(text)
    assert measured and shipping, (
        "docs/REVIEWER_RECALL_METHOD.md no longer states, in the form this "
        "guard reads, which model the last full measurement ran on "
        f"(found: {bool(measured)}) and which model ships as the reviewer "
        f"(found: {bool(shipping)}). Restore those two statements or update "
        "the patterns in this file — a guard that cannot find its subject "
        "reports 'no drift' forever."
    )


def test_shipped_review_model_matches_the_one_the_method_doc_calls_shipping():
    """The doc's account of what ships must be the thing that ships."""
    documented = _SHIPPING_RE.search(_method_doc_text()).group(1)
    shipped = _shipped_review_model()
    assert documented == shipped, (
        f"llm.review_model ships as {shipped!r} but "
        f"docs/REVIEWER_RECALL_METHOD.md still calls {documented!r} the "
        "shipping reviewer. A catch-rate belongs to the model it was measured "
        f"on and does not travel to its successor. {_REMEDY}"
    )


def test_no_catch_rate_is_published_while_the_shipping_model_is_unmeasured():
    """The state this repo is actually in, pinned as the state it is in.

    Measured `claude-opus-4-8`, shipping `claude-opus-5`, no published number —
    and the doc has to keep saying both halves of that out loud. When somebody
    finally re-measures, the mismatch disclaimers become false and this test
    turns them into a failure rather than leaving them to rot.
    """
    text = _method_doc_text()
    measured = _MEASURED_RE.search(text).group(1)
    shipping = _SHIPPING_RE.search(text).group(1)

    if measured != shipping:
        assert _NO_NUMBER in text, (
            f"the reviewer ships as {shipping!r} but the last full measurement "
            f"ran on {measured!r}, and the doc no longer states that no "
            "catch-rate is published. An unmeasured model must not inherit its "
            f"predecessor's figure. {_REMEDY}"
        )
        assert _NOT_REMEASURED in text, (
            "the model mismatch is recorded but the doc no longer says the "
            "shipping reviewer has not been re-measured — that sentence is the "
            f"whole record of why there is no number. {_REMEDY}"
        )
    else:
        assert _NO_NUMBER not in text and _NOT_REMEASURED not in text, (
            f"the doc now says the measurement and the shipping reviewer are "
            f"both {shipping!r}, while still carrying the unmeasured-model "
            "disclaimers. One of the two is stale: either the re-measure "
            "happened and the disclaimers must go, or it did not and the "
            "measured-model line must not claim it did."
        )
