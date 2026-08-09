"""Build the Commit0-style bench corpus, with every control run.

This tier is the first drawn from a REAL external codebase, modelled on the
Commit0 benchmark ("implement a real library from a blanked skeleton"). It is
the complexity jump the synthetic parcelo tier lacks: genuine multi-file /
multi-class synthesis of a permissively-licensed, pure-Python library against
that library's OWN test suite.

HOW IT MAPS ONTO THE EXISTING HARNESS (northstar.py), unchanged:

  subject repo   the vendored library with the spec's TARGET module(s) blanked
                 to stubs — signatures and docstrings kept, bodies replaced by
                 `raise NotImplementedError`. This is what the coder starts from.
  request        "implement <module> so <lib>'s public API behaves as specified."
  holdout        the library's OWN real tests for the blanked module, bundled
                 into one self-contained file. The runner writes it into the
                 sandbox AFTER the agent is done (northstar.py `_holdout_ok`);
                 the coder never sees it. PASS is the holdout going green.
  reference      the library's real (un-blanked) module source. CONTROL ONLY —
                 it never enters a spec YAML, so nothing the agent reads has it.

Controls, all enforced here (a holdout without them is decoration):

  base=FAIL   the holdout must FAIL against the stubbed subject. A holdout that
              already passes on the stubs measures nothing (known-negative).
  ref=PASS    the holdout must PASS once the real source is restored. Without it
              an impossible holdout reads as a capability failure (known-positive).
  suite=green PASS_TO_PASS — a sibling module's tests, shipped inside the subject
              and run via the LANDED northstar mechanism (pass_to_pass_block),
              must still pass on the reference. Empty for a whole-library blank
              (Commit0 is FAIL_TO_PASS by nature — there is no pre-passing test
              on a fully-stubbed tree); present where a spec blanks one module of
              several and the siblings stay real.
  mutation    a defined bug injected into the reference must turn the holdout RED
              (CAUGHT) — proof the holdout has teeth.
  names       every identifier the holdout demands is stated by the ticket or
              already present in the base repo. For Commit0 the stubs KEEP every
              signature, so the public API the holdout exercises is in the base
              tree by construction; this control simply confirms that.

Everything is OFFLINE and deterministic: the library source is VENDORED under
`vendor/<lib>-<version>/` at a pinned version (no pip install, no network at
build or bench time). No model is needed to BUILD the corpus — only to RUN the
agent against it later.

Usage:  python build.py <subjects-dir> <out-specs-dir>
        <subjects-dir>   where the per-spec stubbed subject repos are materialised
        <out-specs-dir>  where the spec YAMLs are written (feed to `nh bench run
                         --specs-dir`)
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"

# Restrict every subprocess PATH to the system dirs, as the parcelo builder
# does — a bench control must not pick up a shim from the operator's PATH.
_ENV_PATH = "/usr/bin:/bin"


# --------------------------------------------------------------------------- #
# Stubbing: keep signatures + docstrings, remove bodies                        #
# --------------------------------------------------------------------------- #

class _BodyBlanker(ast.NodeTransformer):
    """Replace every function/method body with (its docstring, if any) followed
    by ``raise NotImplementedError``. Classes, class variables, imports,
    decorators, signatures and type annotations are all left intact — exactly
    the skeleton Commit0 hands a solver."""

    def _blank(self, node):
        self.generic_visit(node)
        doc = ast.get_docstring(node, clean=False)
        new_body: list[ast.stmt] = []
        if doc is not None:
            new_body.append(ast.Expr(value=ast.Constant(value=doc)))
        new_body.append(
            ast.Raise(exc=ast.Call(
                func=ast.Name(id="NotImplementedError", ctx=ast.Load()),
                args=[], keywords=[]), cause=None))
        node.body = new_body
        return node

    def visit_FunctionDef(self, node):        # noqa: N802
        return self._blank(node)

    def visit_AsyncFunctionDef(self, node):   # noqa: N802
        return self._blank(node)


def stub_source(source: str) -> str:
    """The blanked skeleton of *source*: every callable body removed."""
    tree = ast.parse(source)
    tree = _BodyBlanker().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


# --------------------------------------------------------------------------- #
# Test bundling: one self-contained holdout file from real upstream tests       #
# --------------------------------------------------------------------------- #

def _is_relative_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and (node.level or 0) > 0


def _strip_and_drop(source: str, drop_names: set[str]) -> tuple[str, bool]:
    """Return (rewritten source, saw_relative_import).

    Removes relative imports (``from . import X``) — the bundle is flat, so the
    package they reach for is inlined instead — and drops any top-level
    function/class named in *drop_names* (tests that need state the sandbox
    cannot provide, e.g. installed-package metadata)."""
    tree = ast.parse(source)
    saw_rel = False
    kept: list[ast.stmt] = []
    for node in tree.body:
        if _is_relative_import(node):
            saw_rel = True
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name in drop_names:
            continue
        kept.append(node)
    tree.body = kept
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n", saw_rel


def bundle_tests(lib_root: Path, test_files: list[str],
                 drop_names: set[str]) -> str:
    """One self-contained pytest module from real upstream test files.

    Relative imports are stripped; if any test file used one, the package's
    ``tests/__init__.py`` (which holds the shared test mixins) is inlined ahead
    of the tests so the names resolve. `unittest`-style TestCase classes and
    plain ``test_*`` functions both run under pytest unchanged."""
    parts: list[str] = []
    needs_pkg = False
    bodies: list[str] = []
    for rel in test_files:
        src = (lib_root / rel).read_text(encoding="utf-8")
        rewritten, saw_rel = _strip_and_drop(src, drop_names)
        needs_pkg = needs_pkg or saw_rel
        bodies.append(f"# --- {rel} ---\n{rewritten}")
    if needs_pkg:
        pkg_init = lib_root / "tests" / "__init__.py"
        if pkg_init.exists() and pkg_init.read_text().strip():
            init_src, _ = _strip_and_drop(
                pkg_init.read_text(encoding="utf-8"), drop_names)
            parts.append("# --- tests/__init__.py (shared mixins, inlined) ---\n"
                         + init_src)
    parts.extend(bodies)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# PASS_TO_PASS — the LANDED mechanism (northstar.py pass_to_pass_block clone)   #
# --------------------------------------------------------------------------- #
#
# Identical shape to eval/parcelo_corpus/build.py: the sibling tests shipped in
# the subject's tests/ are embedded at generation time and RUN from a temp
# directory against whatever the agent left behind. Temp, not in-tree, because
# the agent may have edited the in-tree copies and PASS_TO_PASS is a claim about
# the ORIGINAL sibling tests.
def pass_to_pass_block(subject: Path) -> str:
    tests = {p.name: p.read_text()
             for p in sorted((subject / "tests").glob("test_*.py"))}
    if not tests:
        return ""
    return f'''

_PIN_TESTS = {tests!r}


def test_the_sibling_modules_still_pass_against_this_change():
    """PASS_TO_PASS, run for real rather than counted."""
    import pathlib
    import subprocess
    import sys
    import tempfile

    work = pathlib.Path(__file__).resolve().parents[2]
    d = pathlib.Path(tempfile.mkdtemp(prefix="pass2pass-"))
    for name, body in _PIN_TESTS.items():
        (d / name).write_text(body)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d)],
        cwd=work, capture_output=True, text=True,
        env={{"PYTHONPATH": str(work), "PATH": "/usr/bin:/bin"}})
    assert proc.returncode == 0, (
        "a sibling module's tests no longer pass:\\n" + proc.stdout[-2000:])
'''


# --------------------------------------------------------------------------- #
# Specs                                                                        #
# --------------------------------------------------------------------------- #
#
# Each spec: a real library, the module(s) to blank, the held-out tests that
# PROVE the module (FAIL_TO_PASS), an optional sibling test file that must keep
# passing (PASS_TO_PASS), and one mutation that the holdout must catch.
#
# `mutation` is (old, new): a single verified substring in the reference module
# whose replacement introduces a behaviour bug. The build asserts the old text
# appears exactly once before applying it, so a probe can never silently no-op.

SPECS = [
    {
        "id": "c0-wcwidth-core",
        "title": "Commit0: implement wcwidth's terminal-width core",
        "lib": "wcwidth-0.2.13",
        "pkg": "wcwidth",
        "blank": ["wcwidth/wcwidth.py"],
        "request": (
            "This is the pure-Python `wcwidth` library (MIT). The module "
            "`wcwidth/wcwidth.py` has had every function body removed — the "
            "signatures and docstrings remain. Implement them so the library's "
            "public API behaves exactly as documented: `wcwidth(wc, "
            "unicode_version='auto')` returns the number of terminal cells a "
            "single character occupies (0 for zero-width combining marks, 2 "
            "for wide East-Asian characters, 1 for other printable characters, "
            "and -1 for C0/C1 control characters); `wcswidth(pwcs, n=None, "
            "unicode_version='auto')` sums that across a string and returns -1 "
            "if any character is unprintable; and the version helpers "
            "`list_versions()`, `_wcversion_value()` and `_wcmatch_version()` "
            "resolve a supported Unicode version. The Unicode data tables in "
            "the sibling `table_*.py` modules are complete and correct — read "
            "them, do not reinvent them. Do not change any module other than "
            "`wcwidth/wcwidth.py`."),
        "acceptance_criteria": [
            "wcwidth() returns 0 for zero-width, 2 for wide, 1 for other "
            "printable characters and -1 for C0/C1 control characters",
            "wcswidth() sums wcwidth() over a string and returns -1 when any "
            "character is unprintable",
            "list_versions(), _wcversion_value() and _wcmatch_version() resolve "
            "a supported Unicode version as documented",
            "the public API in wcwidth/__init__.py keeps working unchanged",
        ],
        "holdout_tests": ["tests/test_core.py", "tests/test_ucslevel.py"],
        # DISCLOSURE — the holdout is the library's own tests FOR THE BLANKED
        # MODULE, not every upstream test file. Beyond test_package_version
        # (dropped explicitly below), two more upstream files are deliberately
        # NOT held out and are not vendored: test_emojis.py (needs bundled emoji
        # data files that are not part of the blanked width logic) and
        # test_table_integrity.py (py3.12-only; it checks the generated Unicode
        # data tables, which this spec does not blank). So the curation is
        # width-logic-only by design — stated here so a reader sees it.
        # Needs the installed-package metadata (importlib.metadata.version),
        # which the sandbox does not have — it tests packaging, not width logic.
        "drop_tests": {"test_package_version"},
        "pass_to_pass": [],
        "mutation": ("return 1 + _bisearch", "return 2 + _bisearch"),
    },
    {
        "id": "c0-cachetools-keys",
        "title": "Commit0: implement cachetools' cache-key functions",
        "lib": "cachetools-5.5.2",
        "pkg": "cachetools",
        "blank": ["cachetools/keys.py"],
        "request": (
            "This is the pure-Python `cachetools` library (MIT). The module "
            "`cachetools/keys.py` has had every function body removed — "
            "signatures and docstrings remain. Implement it so its public "
            "functions behave as documented: `hashkey(*args, **kwargs)` returns "
            "a hashable cache key that is equal for equal arguments and "
            "independent of keyword-argument order; `typedkey(*args, **kwargs)` "
            "does the same but also distinguishes arguments of different types; "
            "and `methodkey`/`typedmethodkey` are the method variants that drop "
            "the leading `self`. Keys must be hashable and compare equal iff "
            "they represent the same call. Do not change any other module."),
        "acceptance_criteria": [
            "hashkey() returns equal, hashable keys for equal arguments, "
            "independent of keyword-argument order",
            "typedkey() additionally distinguishes arguments of different types",
            "methodkey()/typedmethodkey() ignore the leading self argument",
        ],
        "holdout_tests": ["tests/test_keys.py"],
        "drop_tests": set(),
        # LRUCache lives in cachetools/__init__.py, which is NOT blanked here and
        # does not depend on keys — a genuine sibling that must stay green.
        "pass_to_pass": ["tests/test_lru.py"],
        "mutation": ("return _HashedTuple(args)", "return _HashedTuple(())"),
    },
    {
        "id": "c0-cachetools-func",
        "title": "Commit0: implement cachetools' functools-style decorators",
        "lib": "cachetools-5.5.2",
        "pkg": "cachetools",
        "blank": ["cachetools/func.py"],
        "request": (
            "This is the pure-Python `cachetools` library (MIT). The module "
            "`cachetools/func.py` has had every function body removed — "
            "signatures and docstrings remain. Implement the `functools."
            "lru_cache`-compatible memoizing decorators it exposes "
            "(`fifo_cache`, `lfu_cache`, `lru_cache`, `mru_cache`, `rr_cache`, "
            "`ttl_cache`). Each wraps a function with the matching cache policy "
            "from the `cachetools` package, honours `maxsize` and `typed`, "
            "supports use both as `@lru_cache` and `@lru_cache(maxsize=...)`, "
            "and exposes `.cache_clear()` and `.cache_info()` like the standard "
            "library. The cache classes in `cachetools/__init__.py` are "
            "complete — build on them. Do not change any other module."),
        "acceptance_criteria": [
            "each decorator memoizes using the matching cachetools cache policy",
            "maxsize and typed are honoured, and bare/parametrised forms both work",
            "the wrapped function exposes cache_clear() and cache_info()",
        ],
        "holdout_tests": ["tests/test_func.py"],
        "drop_tests": set(),
        "pass_to_pass": ["tests/test_lru.py"],
        "mutation": ("key = keys.typedkey if typed else keys.hashkey",
                     "key = keys.hashkey"),
    },
    {
        "id": "c0-cachetools-caches",
        "title": "Commit0: implement cachetools' cache classes and decorators",
        "lib": "cachetools-5.5.2",
        "pkg": "cachetools",
        "blank": ["cachetools/__init__.py"],
        "request": (
            "This is the pure-Python `cachetools` library (MIT). The package "
            "module `cachetools/__init__.py` has had every function and method "
            "body removed — every class, signature and docstring remains. "
            "Implement it so the library's public API behaves as documented: "
            "the base `Cache` (a MutableMapping bounded by `maxsize`, evicting "
            "via `popitem()` when full, sizing values with `getsizeof`), and "
            "the eviction policies `FIFOCache`, `LRUCache`, `LFUCache`, "
            "`MRUCache`, `RRCache`, `TTLCache` and `TLRUCache`, plus the "
            "`cached` and `cachedmethod` decorators. The `keys` and "
            "`_decorators` sibling modules are complete — build on them. Do not "
            "change any module other than `cachetools/__init__.py`."),
        "acceptance_criteria": [
            "Cache is a maxsize-bounded MutableMapping that evicts via popitem()",
            "FIFOCache, LRUCache, LFUCache, MRUCache, RRCache, TTLCache and "
            "TLRUCache each evict by their documented policy",
            "the cached and cachedmethod decorators memoize through a supplied cache",
        ],
        "holdout_tests": [
            "tests/test_cache.py", "tests/test_fifo.py", "tests/test_lru.py",
            "tests/test_lfu.py", "tests/test_mru.py", "tests/test_rr.py",
            "tests/test_ttl.py", "tests/test_tlru.py",
            "tests/test_cached.py", "tests/test_cachedmethod.py",
        ],
        "drop_tests": set(),
        # keys.py is NOT blanked here and does not depend on __init__ — a
        # genuine sibling module that must stay green.
        "pass_to_pass": ["tests/test_keys.py"],
        "mutation": ("while self.__currsize + size > maxsize:",
                     "while self.__currsize + size > maxsize + 1:"),
    },
]


# --------------------------------------------------------------------------- #
# Materialising subjects and running controls                                  #
# --------------------------------------------------------------------------- #

def _copy_package(lib_root: Path, dest: Path, pkg: str,
                  blank: set[str]) -> None:
    """Copy the vendored package into *dest*, stubbing the blanked modules.

    Only the importable package is copied (plus an empty conftest.py so a bare
    `pytest`/PYTHONPATH import resolves it) — never the vendored tests/, so the
    held-out suite cannot leak into the coder's tree."""
    src_pkg = lib_root / pkg
    for p in sorted(src_pkg.rglob("*.py")):
        rel = p.relative_to(lib_root)                 # e.g. wcwidth/wcwidth.py
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        text = p.read_text(encoding="utf-8")
        out.write_text(stub_source(text) if str(rel) in blank else text,
                       encoding="utf-8")
    (dest / "conftest.py").write_text("")


def build_subject(spec: dict, subjects_dir: Path) -> Path:
    """Materialise the spec's stubbed subject as a committed git repo."""
    lib_root = VENDOR / spec["lib"]
    dest = subjects_dir / spec["id"]
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _copy_package(lib_root, dest, spec["pkg"], set(spec["blank"]))
    # Ship the vendored LICENSE alongside the code the coder works on.
    lic = lib_root / "LICENSE"
    if lic.exists():
        (dest / "LICENSE").write_text(lic.read_text(encoding="utf-8"))
    # PASS_TO_PASS sibling tests, bundled self-contained, shipped in tests/.
    if spec["pass_to_pass"]:
        (dest / "tests").mkdir(exist_ok=True)
        bundle = bundle_tests(lib_root, spec["pass_to_pass"], spec["drop_tests"])
        (dest / "tests" / "test_p2p.py").write_text(bundle, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.name=commit0", "-c", "user.email=commit0@example.invalid",
         "commit", "-q", "-m", f"Commit0 subject: {spec['id']}"],
        cwd=dest, check=True)
    return dest


def _run_pytest(tree: Path, holdout: str) -> tuple[int, str]:
    held = tree / "tests" / "bench_holdout"
    held.mkdir(parents=True, exist_ok=True)
    f = held / "test_bench_holdout.py"
    f.write_text(holdout)
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(f)],
        cwd=tree, capture_output=True, text=True,
        env={"PYTHONPATH": str(tree), "PATH": _ENV_PATH})
    f.unlink()
    shutil.rmtree(held, ignore_errors=True)
    return p.returncode, p.stdout


def _restore_reference(subject: Path, spec: dict,
                       mutate: bool = False) -> Path:
    """A copy of *subject* with the blanked modules restored to real source
    (optionally with the spec's mutation applied)."""
    lib_root = VENDOR / spec["lib"]
    dst = Path(tempfile.mkdtemp(prefix="c0-ref-"))
    shutil.copytree(subject, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                  ".pytest_cache"))
    for rel in spec["blank"]:
        real = (lib_root / rel).read_text(encoding="utf-8")
        if mutate:
            old, new = spec["mutation"]
            count = real.count(old)
            if count != 1:
                raise SystemExit(
                    f"{spec['id']}: mutation anchor {old!r} appears {count} "
                    f"times in {rel} (expected exactly 1) — probe is invalid")
            real = real.replace(old, new)
        (dst / rel).write_text(real, encoding="utf-8")
    return dst


def names_demanded(holdout: str) -> set[str]:
    """Public identifiers the holdout imports from the subject package."""
    demanded: set[str] = set()
    for node in ast.walk(ast.parse(holdout)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] \
                in {"wcwidth", "cachetools"}:
            demanded |= {a.name for a in node.names if a.name != "*"}
    return demanded


def unstated_names(spec: dict, holdout: str, base_source: str) -> list[str]:
    """Names the holdout imports that neither the ticket states nor the base
    (stubbed) tree already defines. For Commit0 the stubs keep every signature,
    so this should always be empty — the control confirms it."""
    stated = (spec["request"] + " " + " ".join(spec["acceptance_criteria"])).lower()
    out = []
    for name in sorted(names_demanded(holdout)):
        if len(name) <= 2 or name.lower() in stated or name in base_source:
            continue
        out.append(name)
    return out


def main() -> int:
    subjects_dir = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    subjects_dir.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for spec in SPECS:
        sid = spec["id"]
        lib_root = VENDOR / spec["lib"]
        holdout = bundle_tests(lib_root, spec["holdout_tests"],
                               spec["drop_tests"])

        subject = build_subject(spec, subjects_dir)
        # The subject's own (stubbed) source, so a public name the coder READS
        # rather than invents is not reported as one the ticket withheld.
        base_source = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((subject / spec["pkg"]).rglob("*.py")))

        full = holdout + pass_to_pass_block(subject)

        # base=FAIL — holdout must fail on the stubbed subject.
        neg_rc, neg_out = _run_pytest(subject, full)
        # ref=PASS — holdout must pass with the real modules restored.
        ref = _restore_reference(subject, spec)
        pos_rc, pos_out = _run_pytest(ref, full)
        # suite=green — the PASS_TO_PASS sibling tests, on the reference tree.
        if spec["pass_to_pass"]:
            p2p_rc, p2p_out = _run_pytest(
                ref, pass_to_pass_block(subject).replace(
                    "test_the_sibling_modules_still_pass_against_this_change",
                    "test_suite_green_probe"))
            suite_ok = p2p_rc == 0
        else:
            suite_ok, p2p_out = None, ""
        # mutation — a bug in the reference must turn the holdout red.
        mut = _restore_reference(subject, spec, mutate=True)
        mut_rc, mut_out = _run_pytest(mut, holdout)
        caught = mut_rc != 0
        # names — every demanded public name is in the ticket or the base tree.
        unstated = unstated_names(spec, holdout, base_source)

        shutil.rmtree(ref, ignore_errors=True)
        shutil.rmtree(mut, ignore_errors=True)

        ok = (neg_rc != 0 and pos_rc == 0 and caught and not unstated
              and (suite_ok is not False))
        suite_col = ("green" if suite_ok else "RED ✗") if suite_ok is not None \
            else "n/a"
        print(f"{sid:24} base={'FAIL ok' if neg_rc else 'PASS ✗'} "
              f"ref={'PASS ok' if pos_rc == 0 else 'FAIL ✗'} "
              f"suite={suite_col:7} "
              f"mutation={'CAUGHT ok' if caught else 'MISSED ✗'} "
              f"names={'stated' if not unstated else 'UNSTATED ✗'}")
        if not ok:
            failures.append(sid)
            if neg_rc == 0:
                print("    known-negative broken: holdout already passes on the "
                      "stubbed subject — it measures nothing")
            if pos_rc != 0:
                print("    known-positive broken:\n" + pos_out[-1800:])
            if suite_ok is False:
                print("    PASS_TO_PASS broken on the reference:\n" + p2p_out[-1200:])
            if not caught:
                print("    mutation NOT caught — the holdout has no teeth here:\n"
                      + mut_out[-800:])
            if unstated:
                print(f"    holdout demands unstated names: {', '.join(unstated)}")

        pin = subprocess.run(["git", "rev-parse", "HEAD"], cwd=subject,
                             capture_output=True, text=True, check=True).stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=subject, capture_output=True, text=True,
                                check=True).stdout.strip()
        doc = {
            "id": sid,
            "title": spec["title"],
            "request": spec["request"],
            "source": {"kind": "vendored", "session": spec["lib"],
                       "label": "commit0 verifiable corpus"},
            "repo": {"path": str(subject), "pin": pin, "branch": branch},
            "original": {},
            "acceptance_criteria": spec["acceptance_criteria"],
            "judge_rubric": [],
            "holdout": full,
            "subset": "commit0",
            "runnable": True,
            "skip_reason": "",
            "escalation_reason": "",
            "dirty_seed": {},
            "expect_escalation": False,
        }
        (out / f"{sid}.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))

    print(f"\n{len(SPECS)} specs written to {out}")
    print(f"subjects materialised under {subjects_dir}")
    if failures:
        print(f"CONTROLS FAILED for {len(failures)}: {', '.join(failures)}")
        return 1
    print("controls PASSED: every holdout fails on the stubbed subject, passes "
          "on the real source, catches its mutation, and keeps PASS_TO_PASS green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
