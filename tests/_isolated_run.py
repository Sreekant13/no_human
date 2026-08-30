"""Helper for the `..._passes_with_no_pytest_on_path` regression tests.

Deliberately not `test_*` — a plain module, imported with the relative-import
idiom these test files already use for shared fixtures
(`from .test_e2e_orchestrator import ...`).

Runs a single node id in a subprocess under a fresh HOME and a PATH stripped
of any directory that resolves a `pytest`/`pytest.exe`/`pytest.bat`
executable, proving the target test is self-contained rather than depending
on the ambient launcher's PATH (see `tests/conftest.py`'s `own_pytest_on_path`
docstring for the full root-cause writeup).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PYTEST_EXE_NAMES = {"pytest", "pytest.exe", "pytest.bat"}


def scrubbed_path() -> str:
    """The current PATH with every directory carrying a `pytest` executable
    removed — not the whole PATH replaced, so `git`/`/bin/sh` stay resolvable.
    """
    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            names = set(os.listdir(entry))
        except OSError:
            kept.append(entry)
            continue
        if names & _PYTEST_EXE_NAMES:
            continue
        kept.append(entry)
    result = os.pathsep.join(kept)
    assert shutil.which("git", path=result) is not None, (
        "scrubbing every PATH entry that carries a pytest executable also "
        "removed the only entry carrying git — on every known layout pytest "
        "lives in a venv bin that carries no git, so this should not happen: "
        f"PATH={result!r}"
    )
    return result


def run_node_id_isolated(
    node_id: str, tmp_home: Path, timeout: int = 600
) -> subprocess.CompletedProcess:
    """Run *node_id* alone, in a subprocess, under *tmp_home* as HOME, with
    no `pytest` resolvable on PATH.

    Inherits the rest of the parent environment (notably
    `no_human.updates.DISABLE_ENV_VAR`, which keeps the inner run off PyPI)
    except for `VIRTUAL_ENV` and every `PYTEST_*` variable, which are popped
    so the inner run's plugin/xdist state cannot leak in from the outer one.
    Deliberately does NOT set `NH_TEST_HOME` — the inner
    `no_human.testing.pytest_isolated_home` plugin allocates its own.
    """
    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    env["USERPROFILE"] = str(tmp_home)
    env["PATH"] = scrubbed_path()
    env.pop("VIRTUAL_ENV", None)
    for key in list(env):
        if key.startswith("PYTEST_"):
            env.pop(key, None)

    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", node_id, "-q",
             "-p", "no:randomly", "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"")
        stderr = (exc.stderr or b"")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise AssertionError(
            f"isolated run of {node_id!r} timed out after {timeout}s; "
            f"partial output:\n{(stdout + stderr)[-4000:]}"
        ) from exc
