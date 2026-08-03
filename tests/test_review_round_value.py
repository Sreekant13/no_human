"""The R8 measurement script, on fixtures whose answer is known in advance.

``scripts/review_round_value.py`` exists to settle whether later review rounds
add signal. A measurement nobody can check is worse than no measurement, and
this file is the check: each fixture below is a database whose correct verdict
is decided by construction, so a script that reads the data wrongly cannot come
back with a comfortable answer.

The two that matter are the redundancy pair. A run whose later rounds re-raise
the SAME finding must read REDUNDANT — whether the reviewer repeats itself
verbatim (caught by exact label matching) or re-words every round (caught only
by the word-overlap column). The second case is the one the script got wrong
until it was fixed: novelty was consulted only after volume had already
collapsed, so a run that kept raising findings at full volume and re-worded the
same one every time was reported as "does not support capping rounds" — the
opposite of what that data says.

The script is loaded by path rather than imported: it lives in ``scripts/`` and
is not part of the ``no_human`` package.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "review_round_value.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_nh_review_round_value", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rrv = _load_script()


def _make_db(path: Path, histories: dict[str, list[dict]],
             events: list[tuple[str, float, str]] = ()) -> Path:
    """A database shaped like the product's, carrying only what R8 reads."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tasks (id TEXT, context TEXT)")
    con.execute("CREATE TABLE task_events (task_id TEXT, ts REAL, data TEXT)")
    for task_id, history in histories.items():
        con.execute("INSERT INTO tasks VALUES (?,?)",
                    (task_id, json.dumps({"review_history": history})))
    for task_id, ts, kind in events:
        con.execute("INSERT INTO task_events VALUES (?,?,?)",
                    (task_id, ts, json.dumps({"kind": kind})))
    con.commit()
    con.close()
    return path


def _round(n: int, blocking: list[str], *, passed: bool = False,
           advisory: list[str] | None = None) -> dict:
    return {"round": n, "passed": passed, "blocking": blocking,
            "advisory": advisory or []}


def _report(db: Path) -> str:
    con = rrv._connect(db)
    try:
        data = rrv.collect(con)
    finally:
        con.close()
    return rrv.render(data)


# --------------------------------------------------------------------------- #
# The redundancy pair — the verdict must flip on CONTENT, not on volume        #
# --------------------------------------------------------------------------- #

def test_verbatim_repeats_across_rounds_read_as_redundant(tmp_path):
    """Twelve tasks, three rounds each, the same finding re-raised word for
    word. Volume never thins out, so the only thing that can produce the right
    answer is the repeat column."""
    label = "Outer timeout undercuts inner waits — line 795"
    db = _make_db(tmp_path / "verbatim.db", {
        f"t{i}": [_round(1, [label]), _round(2, [label]), _round(3, [label])]
        for i in range(12)
    })
    out = _report(db)

    assert "REDUNDANT" in out, out
    assert "RARE" not in out, out
    # The counts, read off the data rather than pattern-matched in the rendered
    # table: 24 of the 36 findings are re-raises (rounds 2 and 3 of every task),
    # and a substring search for "24" would also match a coincidental 24.
    con = rrv._connect(db)
    data = rrv.collect(con)
    con.close()
    assert sum(r["raised"] for r in data["rounds"].values()) == 36
    assert sum(r["repeat"] for r in data["rounds"].values()) == 24
    assert sum(r["new"] for r in data["rounds"].values()) == 12


def test_reworded_repeats_across_rounds_also_read_as_redundant(tmp_path):
    """The same twelve tasks, except the reviewer re-words the finding every
    round — which is what it actually does in the real database, where exact
    matching finds zero repeats across every multi-round task.

    Exact label matching scores all 36 as "new". If the verdict trusted that
    column alone it would report no support for capping rounds. The word-overlap
    column is what keeps the answer honest, and this test is the reason it
    exists.
    """
    wordings = [
        "Outer timeout undercuts the inner waits — line 795",
        "Outer timeout undercuts inner waits badly — line 800",
        "The outer timeout undercuts inner waits here — line 801",
    ]
    db = _make_db(tmp_path / "reworded.db", {
        f"t{i}": [_round(n, [wordings[n - 1]]) for n in (1, 2, 3)]
        for i in range(12)
    })
    out = _report(db)

    assert "REDUNDANT" in out, out
    assert "RARE" not in out, out
    # Exact matching is genuinely blind here — that is the premise of the test,
    # so it is asserted rather than assumed.
    con = rrv._connect(db)
    data = rrv.collect(con)
    con.close()
    assert sum(r.get("repeat", 0) for r in data["rounds"].values()) == 0
    assert sum(r.get("similar", 0) for r in data["rounds"].values()) == 24


def test_genuinely_new_findings_in_late_rounds_do_not_read_as_redundant(tmp_path):
    """The control. Same shape, distinct findings every round — the verdict
    must NOT call this redundant, or the two tests above prove nothing."""
    db = _make_db(tmp_path / "novel.db", {
        f"t{i}": [
            _round(1, [f"Race condition in scheduler {i} — line 10"]),
            _round(2, [f"Unclosed file handle in reporter {i} — line 20"]),
            _round(3, [f"Missing rollback on partial write {i} — line 30"]),
        ]
        for i in range(12)
    })
    out = _report(db)

    assert "RARE" in out or "does not support capping" in out, out
    assert "REDUNDANT" not in out, out


# --------------------------------------------------------------------------- #
# Columns, attribution, and the empty case                                    #
# --------------------------------------------------------------------------- #

def test_findings_raised_together_are_not_counted_as_repeats(tmp_path):
    """"Earlier" means an earlier ROUND. Two findings raised in the same round
    are two findings, and an off-by-one here would manufacture redundancy that
    is not in the data."""
    db = _make_db(tmp_path / "same_round.db", {
        "t1": [_round(1, ["Timeout undercuts waits — line 1",
                          "Timeout undercuts waits — line 1"])],
    })
    con = rrv._connect(db)
    data = rrv.collect(con)
    con.close()

    assert data["rounds"][1]["raised"] == 2
    assert data["rounds"][1]["new"] == 2
    assert data["rounds"][1]["repeat"] == 0
    assert data["rounds"][1]["similar"] == 0


def test_a_demotion_is_attributed_to_the_round_whose_review_event_follows_it(tmp_path):
    """``review_citation_demoted`` carries no round. It is recovered from event
    order, and getting that backwards would credit round 1's reviewer false
    positive to round 2."""
    db = _make_db(
        tmp_path / "demote.db",
        {"t1": [_round(1, ["A — x"]), _round(2, ["B — y"])]},
        events=[("t1", 1.0, "review_citation_demoted"), ("t1", 2.0, "review"),
                ("t1", 3.0, "review")],
    )
    con = rrv._connect(db)
    data = rrv.collect(con)
    con.close()

    assert data["rounds"][1]["demoted"] == 1
    assert data["rounds"][2]["demoted"] == 0
    assert data["demotions_total"] == 1


def test_an_empty_database_says_nothing_measured_rather_than_reporting_a_result(tmp_path):
    """A measurement with no data must be loud about it. The failure this
    guards is a green-looking table over zero rows."""
    db = _make_db(tmp_path / "empty.db", {})
    out = _report(db)

    assert "no review rounds in this database — nothing measured" in out, out
    assert "REDUNDANT" not in out and "RARE" not in out, out


def test_the_absence_of_repeats_is_reported_as_an_instrument_limit(tmp_path):
    """Two rounds of findings that share no wording at all: both matchers come
    back empty. That must read as "this instrument sees no repetition", never as
    "the reviewer never repeats itself"."""
    db = _make_db(tmp_path / "no_repeats.db", {
        "t1": [_round(1, ["Race condition in scheduler — line 10"]),
               _round(2, ["Unclosed handle in reporter — line 20"])],
    })
    out = _report(db)

    assert "this instrument sees" in out, out


# --------------------------------------------------------------------------- #
# The property the whole script rests on                                      #
# --------------------------------------------------------------------------- #

def test_the_database_is_opened_read_only(tmp_path):
    """R8 measures; it never writes. A measurement that can modify the
    operator's database is not a measurement anyone should run twice."""
    db = _make_db(tmp_path / "ro.db", {"t1": [_round(1, ["A — x"])]})
    con = rrv._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            con.execute("INSERT INTO tasks VALUES ('t2', '{}')")
    finally:
        con.close()


def test_a_missing_database_refuses_instead_of_reporting_zeroes(tmp_path):
    with pytest.raises(SystemExit):
        rrv._connect(tmp_path / "does_not_exist.db")


def test_the_script_imports_nothing_from_the_product():
    """It is a standalone reader of a database any version may have written.
    An import of ``no_human`` would tie the measurement to the code under
    measurement — and would break the moment it is run against an older DB."""
    source = SCRIPT.read_text(encoding="utf-8")
    offenders = [line for line in source.splitlines()
                 if line.startswith(("import ", "from "))
                 and "no_human" in line]
    assert offenders == [], offenders


def test_the_script_only_ever_reads(tmp_path):
    """R8 may only ever REDUCE rounds, and even that is a separate decision
    taken after reading the measurement. The measuring script must not be the
    thing that changes anything — not the round budget, not the database.

    Parsed, not grepped: the script's prose explains the orchestrator's
    ``_REVIEW_HISTORY_ROUNDS`` cap, and a substring search would flag its own
    documentation. What must be absent is a WRITE.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert not any("REVIEW_HISTORY" in name for name in assigned), sorted(assigned)

    # Every statement the script executes, read off the `.execute(...)` calls
    # themselves rather than by scanning prose for verbs — "drop below 25%" in
    # a verdict string is not a DROP TABLE, and a guard that cannot tell the
    # difference gets switched off.
    statements = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
        and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert statements, "no SQL found — this guard is reading the wrong thing"
    non_select = [s for s in statements if not s.strip().upper().startswith("SELECT")]
    assert non_select == [], f"the measurement script runs write SQL: {non_select}"
