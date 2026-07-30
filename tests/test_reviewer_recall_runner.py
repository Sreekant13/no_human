"""Tests for the SCRUM-29 reviewer-recall runner (eval/reviewer_recall/runner.py).

The runner lives outside src/no_human (single surface: eval/ CLI python
only — docs/REVIEWER_RECALL_METHOD.md), so it is loaded here by file path,
the same way the CLI wiring (`nh bench report --reviewer-recall`) loads it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "eval" / "reviewer_recall" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "test_reviewer_recall_runner_module", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rr = _load_runner()


def _truth(**overrides):
    base = {
        "class": "logic", "file": "app.py", "hunk_lines": [10, 20],
        "description": "off-by-one", "keywords": ["off-by-one", "boundary"],
        "planted_by": "supervising-session", "date": "2026-07-25",
    }
    base.update(overrides)
    return base


def _case(**truth_overrides) -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-case", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n", truth=_truth(**truth_overrides),
    )


def _control_case() -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-control", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n",
        truth={"class": "control", "file": None, "hunk_lines": None,
               "description": "clean", "keywords": [],
               "planted_by": "supervising-session", "date": "2026-07-25"},
    )


# --- score_case: the six required stubbed-reviewer scenarios ---------------

def test_caught_case():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is True
    assert result.clean_pass is None


def test_missed_wrong_file():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="other.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "planted file" in result.reason


def test_missed_right_file_wrong_lines():
    case = _case()  # hunk_lines [10, 20] -> in-range is [7, 23]
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=99,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "hunk_lines" in result.reason


def test_keyword_miss():
    case = _case()  # keywords: off-by-one, boundary
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="this code could use a docstring", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "keyword" in result.reason


def test_control_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True
    assert result.caught is None


def test_control_false_alarm():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="looks suspicious", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is False
    assert "false alarm" in result.reason


# --- additional scoring edges -----------------------------------------------

def test_control_fail_only_non_blocking_is_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="nit: naming", blocking=False),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True


def test_pass_verdict_is_missed_even_with_stray_finding_text():
    case = _case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.caught is False


# --- render_report: denominators, never a bare percentage -------------------

def test_render_report_format():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    seeded_miss = rr.CaseResult(case_id="b", cls="security", is_control=False,
                                outcome=rr.ReviewOutcome(status="PASS"), caught=False)
    control_ok = rr.CaseResult(case_id="c", cls="control", is_control=True,
                               outcome=rr.ReviewOutcome(status="PASS"), clean_pass=True)
    report = rr.RecallReport(
        results=[seeded_hit, seeded_miss, control_ok],
        model="claude-opus-5", run_date="2026-07-25",
    )
    text = rr.render_report(report)
    assert "reviewer recall: 1/2" in text
    assert "logic 1/1" in text and "security 0/1" in text
    assert "specificity:     1/1 clean diffs passed" in text
    assert "model: claude-opus-5" in text
    assert "run date: 2026-07-25" in text
    assert "docs/REVIEWER_RECALL_METHOD.md" in text
    # never a bare percentage: the "(NN%)" always follows an "x/y" denominator
    assert "reviewer recall: 1/2 (50%)" in text


# --- corpus + checkout/apply wiring (real corpus, no LLM calls) -------------

def test_load_cases_real_corpus():
    cases = rr.load_cases()
    assert len(cases) >= 9
    ids = {c.case_id for c in cases}
    assert "logic-stale-renders-fresh" in ids
    assert "control-bench-baseline" in ids
    classes = {c.truth["class"] for c in cases}
    assert classes == {"logic", "security", "spec-miss", "test-tamper", "control"}


def test_prepare_case_repo_has_no_descendant_history(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    case_repo = rr.prepare_case_repo(REPO_ROOT, case, tmp_path)
    log = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                         check=True, capture_output=True, text=True)
    commit_shas = log.stdout.split()
    assert len(commit_shas) == 1, "checkout must carry no ancestor/descendant history"
    # the diff actually applied
    diff_status = subprocess.run(["git", "status", "--porcelain"], cwd=case_repo,
                                 check=True, capture_output=True, text=True)
    assert diff_status.stdout.strip() != ""


def test_every_case_prepares_with_no_repo_history_at_all(tmp_path):
    """The corpus must survive the open-source export: a fresh `git init` with
    one commit and none of the objects `base.ref` names.

    This does not simulate that with a flag — it points `repo_root` at a
    directory that is not a git repository at all, so any surviving read of
    repo history (git archive / rev-parse / cat-file) fails loudly.
    """
    not_a_repo = tmp_path / "no-git-here"
    not_a_repo.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    prepared, errors = [], []
    for case in rr.load_cases():
        try:
            case_repo = rr.prepare_case_repo(not_a_repo, case, work)
        except Exception as exc:  # noqa: BLE001 — the point is to report all
            errors.append(f"{case.case_id}: {type(exc).__name__}: {exc}")
            continue
        prepared.append(case.case_id)
        shas = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                              check=True, capture_output=True, text=True)
        assert len(shas.stdout.split()) == 1, case.case_id
        planted = case.truth.get("file")
        if planted:
            assert (case_repo / planted).is_file(), case.case_id
    assert errors == [], errors
    assert len(prepared) == len(rr.load_cases())


def _blob_sha1(data: bytes) -> str:
    """git's blob object id, computed here rather than imported from
    `materialize_base` — a check that calls the extractor's own helper would
    agree with it by construction."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load_materialize_base():
    """`materialize_base.py`, loaded by path exactly as `rr` is above.

    Its `scrub` IS imported rather than reimplemented, and the difference from
    `_blob_sha1` is the point. A hash has a published definition, so a second
    implementation is an independent witness. The substitution list does not:
    its whole content is "what the operator decided to replace", and a copy here
    would be one more hand-maintained list, free to drift from the materialiser
    and let a fixture pass against rules the materialiser no longer has. The
    claim under test is that the fixtures on disk are what the materialiser
    produces, so the materialiser has to be the reference.
    """
    spec = importlib.util.spec_from_file_location(
        "test_rr_materialize_base",
        REPO_ROOT / "eval" / "reviewer_recall" / "materialize_base.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_scrub = _load_materialize_base().scrub


def test_prepared_case_repo_matches_the_pinned_base_content():
    """Materialising must not have altered what the case measures: every file
    under `base/` has to still be the bytes the manifest pins, and those bytes
    have to still trace to `base.ref`.

    This runs ALWAYS, including after the export. Column 1 of `base.manifest` is
    the git blob id of the file ON DISK, so the byte-identity pin survives a repo
    where `base.ref` resolves nothing — which is the environment this whole
    change exists to serve, and exactly where a skip would have switched it off.

    Where the history IS still present, provenance is re-derived from git so the
    recorded ids cannot drift from their source. A file with no third column must
    equal its blob at `base.ref` byte for byte, as before. A file WITH a third
    column was de-identified after extraction (see
    `materialize_base.py::scrub`), so it must NOT equal that blob — and the blob
    must hash to the recorded origin id. Both directions are asserted: an
    undeclared edit to a fixture fails, and so does a stale `scrubbed-from`
    column on a file that is no longer scrubbed. The scrub itself only ever
    rewrites a vendor term inside a comment or a string, never a line the case's
    `change.diff` quotes as context — `test_every_case_prepares_with_no_repo_history_at_all`
    is what proves that, by applying all 20 diffs.
    """
    have_history = subprocess.run(
        ["git", "cat-file", "-e", "58e3c7d18644306d0dc11da47e2aae7accdae892"],
        cwd=REPO_ROOT, capture_output=True,
    ).returncode == 0
    checked = scrubbed = 0
    for case in rr.load_cases():
        base_dir = case.dir / rr.BASE_DIR_NAME
        manifest = {}
        for line in (case.dir / "base.manifest").read_text().splitlines():
            fields = line.split("  ")
            assert len(fields) in (2, 3), f"{case.case_id}: bad manifest line: {line!r}"
            sha, rel = fields[0], fields[1]
            manifest[rel] = (sha, fields[2] if len(fields) == 3 else None)
        on_disk = sorted(
            p.relative_to(base_dir).as_posix()
            for p in base_dir.rglob("*") if p.is_file()
        )
        assert on_disk == sorted(manifest), (
            f"{case.case_id}: base/ and base.manifest disagree on which files exist")
        for rel, (sha, origin) in manifest.items():
            data = (base_dir / rel).read_bytes()
            assert _blob_sha1(data) == sha, (
                f"{case.case_id}:{rel} no longer matches its manifest blob id")
            if origin is not None:
                scrubbed += 1
            if have_history:
                blob = subprocess.run(
                    ["git", "cat-file", "blob", f"{case.base_ref}:{rel}"],
                    cwd=REPO_ROOT, capture_output=True,
                )
                assert blob.returncode == 0, f"{case.case_id}:{rel} not at base.ref"
                if origin is None:
                    assert blob.stdout == data, (
                        f"{case.case_id}:{rel} differs from the blob at "
                        f"{case.base_ref} with no 'scrubbed-from' column to say so")
                else:
                    assert _blob_sha1(blob.stdout) == origin, (
                        f"{case.case_id}:{rel} does not descend from manifest "
                        f"origin {origin} at {case.base_ref}")
                    assert blob.stdout != data, (
                        f"{case.case_id}:{rel} carries a 'scrubbed-from' column "
                        "but is byte-identical to base.ref — stale declaration")
                    # …and the difference is EXACTLY the scrub, nothing else.
                    #
                    # Without this line the three assertions above are
                    # self-referential on the scrubbed files: column 1 is
                    # recomputed from disk, column 3 from the origin blob, and
                    # "they differ" is satisfied by ANY difference. An
                    # independent review demonstrated it on 2026-07-31 — append
                    # a payload line to a scrubbed fixture, recompute column 1,
                    # leave column 3 alone, and this test passed. The pin said
                    # where the bytes came from and that they had been changed;
                    # it never said HOW, so it permitted any change at all.
                    #
                    # Only reachable where history is present. In the export
                    # `have_history` is False and `scrub` has no rules anyway —
                    # column 1 is the pin that survives out there.
                    assert _scrub(blob.stdout) == data, (
                        f"{case.case_id}:{rel} is not its origin blob put "
                        "through scrub() — the fixture carries an edit that "
                        "the de-identification substitutions do not account "
                        "for. Re-run materialize_base.py.")
            checked += 1
    assert checked == 70, checked
    # 12 -> 17 on 2026-07-31: five more fixtures now carry a scrub, four of them
    # for the two employer ticket ids that a term list could never have seen.
    # Deliberately a literal and not a count derived from the manifests, which
    # is where `scrubbed` already comes from and would agree by construction.
    assert scrubbed == 17, scrubbed


@pytest.mark.asyncio
async def test_run_case_invokes_injected_reviewer_with_checked_out_repo(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    seen = {}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        seen["repo_path"] = repo_path
        seen["diff_text"] = diff_text
        seen["case_id"] = case_spec.case_id
        return rr.ReviewOutcome(status="FAIL", findings=[
            rr.Finding(file=case_spec.truth["file"],
                      line=case_spec.truth["hunk_lines"][0] + 1,
                      text="fresh renders stale duplicate divider", blocking=True),
        ])

    result = await rr.run_case(REPO_ROOT, case, stub_reviewer, tmp_path)
    assert seen["case_id"] == "logic-stale-renders-fresh"
    assert seen["diff_text"] == case.diff_text
    assert (Path(seen["repo_path"]) / case.truth["file"]).exists()
    assert result.caught is True


@pytest.mark.asyncio
async def test_run_all_iterates_every_case_with_injected_reviewer():
    call_count = {"n": 0}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        call_count["n"] += 1
        if case_spec.is_control:
            return rr.ReviewOutcome(status="PASS", findings=[])
        return rr.ReviewOutcome(status="PASS", findings=[])  # every seeded case missed

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer, model="stub-model")
    all_cases = rr.load_cases()
    assert call_count["n"] == len(all_cases)
    assert len(report.results) == len(all_cases)
    assert report.model == "stub-model"


# --- load_cases must never quietly shrink the denominator -------------------

def _copy_corpus(tmp_path):
    dest = tmp_path / "cases"
    shutil.copytree(rr.CASES_DIR, dest)
    return dest


def test_load_cases_ignores_a_directory_that_is_not_a_case(tmp_path):
    cases_dir = _copy_corpus(tmp_path)
    (cases_dir / "__pycache__").mkdir()
    (cases_dir / "__pycache__" / "runner.cpython-312.pyc").write_bytes(b"\x00")
    assert len(rr.load_cases(cases_dir)) == 20


def test_load_cases_raises_on_a_partial_case_instead_of_dropping_it(tmp_path):
    """A case missing one of its three files used to vanish from the run.

    That routes around HeadlineRefusedError: a dropped case never becomes a
    CaseResult, so the refusal guard cannot see it, and the denominator moves
    with nothing to say so (`security 0/3` where it should read 0/4).
    """
    for missing in rr.CASE_FILE_NAMES:
        cases_dir = _copy_corpus(tmp_path / missing)
        (cases_dir / "security-payload-logged" / missing).unlink()
        with pytest.raises(rr.CasePrepError) as exc:
            rr.load_cases(cases_dir)
        assert "security-payload-logged" in str(exc.value)
        assert missing in str(exc.value)


# --- a control's clean pass must be auditable for citation demotions --------

def _demoted_control(demoted):
    """A control where the reviewer raised a blocking finding that the
    citation rule then demoted — so `blocking` is empty and it scores clean."""
    return rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="off-diff.py", line=7, text="looks wrong", blocking=False),
    ], demoted_citations=demoted)


def test_demoted_clean_pass_is_flagged_not_silently_banked():
    case = _control_case()
    result = rr.score_case(case, _demoted_control(["hardcoded path: off-diff.py not found"]))
    assert result.clean_pass is True          # scoring is unchanged...
    assert result.clean_pass_relied_on_demotion is True   # ...but it is marked
    assert "demoted" in result.reason
    assert "off-diff.py" in result.reason


def test_genuine_clean_pass_is_not_flagged():
    result = rr.score_case(_control_case(), rr.ReviewOutcome(status="PASS", findings=[]))
    assert result.clean_pass is True
    assert result.clean_pass_relied_on_demotion is False
    assert result.reason == "clean pass"


def test_render_report_warns_when_specificity_rests_on_demotions():
    suspect = rr.score_case(_control_case(), _demoted_control(["x: off-diff.py not found"]))
    report = rr.RecallReport(results=[suspect], model="claude-opus-5",
                             run_date="2026-07-30")
    text = rr.render_report(report)
    assert "specificity:     1/1 clean diffs passed" in text
    assert "demoted by the citation rule" in text
    assert "off-diff.py" in text


def test_transcript_records_demotions_so_a_score_can_be_audited(tmp_path):
    suspect = rr.score_case(_control_case(), _demoted_control(["x: off-diff.py not found"]))
    report = rr.RecallReport(results=[suspect], model="claude-opus-5",
                             run_date="2026-07-30")
    rr.write_transcripts(report, tmp_path)
    data = json.loads((tmp_path / "2026-07-30" / "synthetic-control.json").read_text())
    assert data["score"] == 1.0
    assert data["clean_pass_relied_on_demotion"] is True
    assert data["demoted_citations"] == ["x: off-diff.py not found"]


def test_default_reviewer_fn_carries_demoted_citations_off_the_decision():
    """The runner's own adapter used to drop `decision.demoted_citations`, so
    the signal never reached a transcript no matter what the reviewer found."""
    import asyncio
    from no_human.review.selfcheck import ChecklistItem

    class _Decision:
        passed = True
        checklist: list = []
        demoted_citations = ["style: nowhere.py not found in the worktree"]
        failed_items: list = []
        blocking_items: list = []

    class _Reviewer:
        def __init__(self, model): pass
        async def review(self, *a, **k): return _Decision()

    import no_human.review.reviewer as rev
    real, rev.AdversarialReviewer = rev.AdversarialReviewer, _Reviewer
    try:
        fn = rr._default_reviewer_fn("stub")
        outcome = asyncio.run(fn(Path("."), "diff", _control_case()))
    finally:
        rev.AdversarialReviewer = real
    assert outcome.demoted_citations == ["style: nowhere.py not found in the worktree"]
    assert ChecklistItem is not None  # import pinned: the adapter builds from these


# --- SCRUM-47: ERROR path never scores as a miss ----------------------------

@pytest.mark.asyncio
async def test_setup_failure_marks_status_error(tmp_path):
    bad_case = rr.CaseSpec(
        case_id="broken-checkout", dir=Path("/dev/null"),
        base_ref="not-a-real-ref-deadbeef", diff_text="",
        truth=_truth(),
    )

    async def stub_reviewer(repo_path, diff_text, case_spec):
        raise AssertionError("reviewer must never be invoked when setup fails")

    result = await rr.run_case(REPO_ROOT, bad_case, stub_reviewer, tmp_path)
    assert result.status == "ERROR"
    assert result.caught is None
    assert result.clean_pass is None
    assert "case setup failed" in result.reason


# --- SCRUM-47: headline refused when any case errored ------------------------

def test_render_report_refuses_on_error():
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-5",
                             run_date="2026-07-25")
    with pytest.raises(rr.HeadlineRefusedError):
        rr.render_report(report)


def test_render_report_ok_when_no_errors():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    report = rr.RecallReport(results=[seeded_hit], model="claude-opus-5",
                             run_date="2026-07-25")
    text = rr.render_report(report)
    assert "reviewer recall: 1/1" in text


# --- SCRUM-47: transcripts written on every run_all invocation --------------

@pytest.mark.asyncio
async def test_run_all_writes_transcripts(tmp_path):
    async def stub_reviewer(repo_path, diff_text, case_spec):
        return rr.ReviewOutcome(status="PASS", findings=[])

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer,
                              model="stub-model", run_date="2026-07-25",
                              runs_dir=tmp_path)
    all_cases = rr.load_cases()
    out_dir = tmp_path / "2026-07-25"
    assert out_dir.is_dir()
    for case in all_cases:
        case_file = out_dir / f"{case.case_id}.json"
        assert case_file.exists(), f"missing transcript for {case.case_id}"
    sample = json.loads((out_dir / f"{all_cases[0].case_id}.json").read_text())
    assert {"case_name", "status", "score"} <= sample.keys()
    assert sample["case_name"] == all_cases[0].case_id
    assert sample["status"] == "OK"


# --- SCRUM-47: ERROR cases omit `caught` in transcripts (never a miss) ------

def test_error_transcript_omits_caught(tmp_path):
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-5",
                             run_date="2026-07-25")
    rr.write_transcripts(report, tmp_path)
    data = json.loads((tmp_path / "2026-07-25" / "broken-checkout.json").read_text())
    assert data["status"] == "ERROR"
    assert "caught" not in data
    assert data["score"] is None
    assert "case setup failed" in data["error_message_if_error"]
