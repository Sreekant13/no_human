"""The MULTI-REPO tier: one ticket that cannot be finished in a single repo.

`Task.linked_repos` and `core/multi_repo.py` have supported multi-repo tasks
for a while — a branch and a PR per repo, and the task reaches the human gate
only once EVERY repo has a PR — but no benchmark could express it, so the
capability went unmeasured. `BenchTask.linked_repos` plus
`_setup_linked_sandboxes` close that, with every linked repo behind the same
push-proof guard as the primary.

The project is deliberately the smallest thing that is genuinely two repos:
`parcelo-core` owns pricing, `parcelo-api` imports it and serves customers. The
ticket needs a rule change in core AND a response-shape change in api, so a
coder that only touches one repo cannot pass — which is the whole point.

The holdout runs in the PRIMARY sandbox and reaches the linked one through the
workdir layout the runner creates, asserting it found it. Without that
assertion a missing linked sandbox would look like an import error and be read
as a capability failure — the instrument-vs-capability confusion this corpus
exists to avoid.

Usage:  python build_multirepo.py <core-repo> <api-repo> <out-specs-dir>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HOLDOUT = '''\
import pathlib
import sys

# The primary sandbox is <workdir>/work; the runner puts every linked repo under
# <workdir>/linked/<n>-<name>. Reach the api repo through that layout, and ASSERT
# it — a linked sandbox that never got created would otherwise surface as a bare
# ImportError and read as the agent's failure rather than the harness's.
_WORK = pathlib.Path(__file__).resolve().parents[2]
_LINKED = sorted((_WORK.parent / "linked").glob("*-*"))
assert _LINKED, f"no linked sandbox under {_WORK.parent / 'linked'}"
sys.path.insert(0, str(_LINKED[0]))
sys.path.insert(0, str(_WORK))

from parcelo_api.service import handle
from parcelo_core.pricing import quote


def test_the_linked_repo_really_is_the_api_repo():
    """The control for everything below: if this fails, the rest is measuring
    an absent repo, not the agent."""
    assert (_LINKED[0] / "parcelo_api" / "service.py").exists(), list(_LINKED)


def test_core_charges_the_zone_surcharge_once():
    # (3.20 + 2.20) * 1.5 = 8.10, then the zone B surcharge once.
    assert quote(2.0, "B", express=True) == 9.60
    assert quote(2.0, "C", express=True) == 11.10
    # Inside zone A there is no surcharge, so nothing moves.
    assert quote(2.0, "A", express=True) == 8.10
    # No standard price changes.
    assert quote(2.0, "A") == 5.40
    assert quote(2.0, "B") == 6.90


def test_the_api_exposes_the_express_uplift():
    r = handle({"op": "quote", "weight_kg": 2.0, "zone": "B", "express": True})
    assert r["ok"] is True
    assert r["total"] == 9.60
    # (3.20 + 2.20) * 0.5 = 2.70
    assert r["express_uplift"] == 2.70


def test_a_standard_quote_reports_no_uplift():
    r = handle({"op": "quote", "weight_kg": 2.0, "zone": "B"})
    assert r["total"] == 6.90
    assert r["express_uplift"] == 0.0


def test_the_unknown_op_path_is_untouched():
    assert handle({"op": "nope"})["ok"] is False
'''

REQUEST = (
    "Express cross-zone quotes are overcharging: a 2kg express parcel to zone B "
    "quotes 11.85 and our rate card says 9.60, because the zone surcharge is "
    "applied inside the express multiplier AND again on top. Two things have to "
    "land together.\n\n"
    "In parcelo-core: the express multiplier applies to the base fee and the "
    "weight charge only, and the zone surcharge is added exactly once, after "
    "the multiplier. Express inside zone A must not change, and no standard "
    "price may change.\n\n"
    "In parcelo-api: support cannot explain the new prices, so the quote "
    "response must also carry the express uplift under an \"express_uplift\" "
    "key — the amount the multiplier added, which is 0.0 for a standard quote. "
    "Nothing else about the response changes.\n\n"
    "Both repos need a PR; neither change is any use on its own.")

CRITERIA = [
    "in parcelo-core, the express multiplier applies to the base fee and weight "
    "charge only",
    "in parcelo-core, the zone surcharge is added exactly once, after the multiplier",
    "no standard price changes and express within zone A is unchanged",
    "in parcelo-api, the quote response carries an 'express_uplift' key",
    "express_uplift is 0.0 for a standard quote",
    "the existing tests in both repos still pass",
]

REF_CORE = ('''    total = BASE_FEE + weight_cost + surcharge
    if express:
        total = total * EXPRESS_MULTIPLIER + surcharge
    return round(total, 2)''',
            '''    total = BASE_FEE + weight_cost
    if express:
        total = total * EXPRESS_MULTIPLIER
    return round(total + surcharge, 2)''')

REF_API = ('''    total = quote(request["weight_kg"], request["zone"],
                  express=bool(request.get("express", False)))
    return {"ok": True, "total": total}''',
           '''    express = bool(request.get("express", False))
    total = quote(request["weight_kg"], request["zone"], express=express)
    uplift = round(total - quote(request["weight_kg"], request["zone"]), 2) \\
        if express else 0.0
    return {"ok": True, "total": total, "express_uplift": uplift}''')


def _stage(core: Path, api: Path) -> Path:
    """A workdir shaped exactly like the runner's: work/ + linked/0-<name>."""
    root = Path(tempfile.mkdtemp(prefix="mr-"))
    ign = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")
    shutil.copytree(core, root / "work", ignore=ign)
    shutil.copytree(api, root / "linked" / f"0-{api.name}", ignore=ign)
    return root


def _run(root: Path) -> tuple[int, str]:
    held = root / "work" / "tests" / "bench_holdout"
    held.mkdir(parents=True, exist_ok=True)
    f = held / "test_bench_holdout.py"
    f.write_text(HOLDOUT)
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", str(f)],
                       cwd=root / "work", capture_output=True, text=True,
                       env={"PYTHONPATH": str(root / "work"),
                            "PATH": "/usr/bin:/bin"})
    shutil.rmtree(held, ignore_errors=True)
    return p.returncode, p.stdout


def _suite(tree: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"], cwd=tree,
        capture_output=True, text=True,
        env={"PYTHONPATH": str(tree), "PATH": "/usr/bin:/bin"}).returncode


def _patch(path: Path, pair) -> None:
    old, new = pair
    body = path.read_text()
    assert old in body, f"reference patch no longer applies to {path}"
    path.write_text(body.replace(old, new, 1))


def main() -> int:
    core, api, out = (Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(),
                      Path(sys.argv[3]).resolve())
    out.mkdir(parents=True, exist_ok=True)

    root = _stage(core, api)
    neg_rc, neg_out = _run(root)

    # Reference: BOTH repos. The half-fix control below proves both are needed.
    _patch(root / "work" / "parcelo_core" / "pricing.py", REF_CORE)
    core_only_rc, _ = _run(root)
    _patch(root / "linked" / f"0-{api.name}" / "parcelo_api" / "service.py", REF_API)
    pos_rc, pos_out = _run(root)
    suites = (_suite(root / "work"), _suite(root / "linked" / f"0-{api.name}"))

    print(f"{'multi-repo spec':46} base={'FAIL ok' if neg_rc else 'PASS ✗'} "
          f"ref-BOTH={'PASS ok' if pos_rc == 0 else 'FAIL ✗'} "
          f"suites={'green' if suites == (0, 0) else f'RED ✗ {suites}'}")
    print(f"{'one-repo-only control':46} core-fixed-but-not-api="
          f"{'STILL FAILS ok' if core_only_rc else 'PASSES ✗ — the ticket is not really multi-repo'}")
    if pos_rc:
        print(pos_out[-2000:])
    if neg_rc == 0:
        print(neg_out[-800:])
    shutil.rmtree(root, ignore_errors=True)

    pin = lambda d: subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                                   capture_output=True, text=True).stdout.strip()
    (out / "mr-01-express-surcharge-across-two-repos.yaml").write_text(yaml.safe_dump({
        "id": "mr-01-express-surcharge-across-two-repos",
        "title": "MR-51 Express quotes overcharge, and support cannot explain them",
        "request": REQUEST,
        "source": {"kind": "authored", "session": "personal2-2026-08-08",
                   "label": "multi-repo tier"},
        "repo": {"path": str(core), "pin": pin(core), "branch": "main"},
        "linked_repos": [{"path": str(api), "pin": pin(api), "branch": "main"}],
        "original": {}, "acceptance_criteria": CRITERIA, "judge_rubric": [],
        "holdout": HOLDOUT, "subset": "multirepo", "runnable": True,
        "skip_reason": "", "escalation_reason": "", "dirty_seed": {},
        "expect_escalation": False,
    }, sort_keys=False, allow_unicode=True, width=100))

    ok = neg_rc != 0 and pos_rc == 0 and suites == (0, 0) and core_only_rc != 0
    print(f"\nspec written to {out}")
    print("controls PASSED" if ok else "CONTROLS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
