"""The installed wheel must be able to serve the board.

`README` calls `nh start` the primary entrypoint and `nh start` serves the React
board. `web/dist` is a gitignored build artifact, so for as long as it was not
declared as package data, `pip install no-human` produced a CLI whose main
command came up with no UI: the API answered, `GET /` returned FastAPI's bare
`{"detail":"Not Found"}`, and nothing said why.

`tests/test_api.py::test_web_dist_path_points_at_repo_web_dir` cannot catch that
— it asserts a path CONSTANT, computed inside a source checkout, and the
constant was always right there. The defect only exists once the code has been
copied somewhere else. So the test below builds the real wheel, installs it into
a genuinely clean virtualenv, starts a real HTTP server out of that venv, and
reads the bytes off a socket.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fast tier — runs everywhere, including CI (which has no npm in the py job)    #
# --------------------------------------------------------------------------- #

def test_pyproject_force_includes_the_board_in_wheel_and_sdist():
    """The declaration that puts the board in the wheel.

    `force-include` specifically, not an `include`/`artifacts` glob: hatchling
    raises `FileNotFoundError: Forced include not found` when the source is
    absent, so a release built without `npm run build` fails loudly instead of
    silently shipping the broken product again. A glob would match nothing and
    build a quiet, boardless wheel — which is the bug this file exists for.
    """
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    targets = cfg["tool"]["hatch"]["build"]["targets"]

    # Asserted as an EXACT mapping, not per-entry. Relaxing it to per-entry
    # lookups (as this file briefly did when `migrations` was added) leaves the
    # wheel with no membership guard anywhere in the suite: a rogue entry such
    # as `"src/no_human/eval" = "no_human/leaked_eval"` becomes invisible, and
    # shipping the private eval vendor-terms supplement in every wheel is a leak
    # this repo has already had once. The sdist has a separate exact-membership
    # test (`test_sdist_members_are_exactly_the_allowlist`); the wheel has only
    # this. The cost is one line here per legitimate addition, which is the
    # guard working rather than the guard being in the way.
    assert targets["wheel"]["force-include"] == {
        "web/dist": "no_human/web_dist",
        "migrations": "no_human/migrations",
    }
    # The sdist keeps repo-relative paths, so a wheel built FROM the sdist still
    # finds them where the wheel target expects to read them. `uv build` does
    # exactly that, and `pip install` does it whenever it cannot use a prebuilt
    # wheel.
    assert targets["sdist"]["force-include"] == {
        "web/dist": "web/dist",
        "migrations": "migrations",
    }


def test_resolve_web_dist_prefers_checkout_then_package_data(tmp_path, monkeypatch):
    """Both layouts resolve, and neither can shadow the other.

    Driven against real directory trees rather than mocks: the function is
    `is_file()` on a path, so a mock would only assert that the code calls the
    mock. Each layout is built on disk and the resolver is asked to find it.

    The module is fetched with importlib, not named in a monkeypatch string:
    `no_human.api` re-exports the FastAPI instance as `app`, and that attribute
    shadows the submodule when the path is resolved attribute-by-attribute.
    """
    app_module = importlib.import_module("no_human.api.app")
    _resolve_web_dist = app_module._resolve_web_dist

    # Layout A: source checkout / frozen bundle — <root>/web/dist, reached from
    # <root>/src/no_human/api/app.py by parents[3].
    checkout = tmp_path / "checkout"
    api_dir = checkout / "src" / "no_human" / "api"
    api_dir.mkdir(parents=True)
    (checkout / "web" / "dist").mkdir(parents=True)
    (checkout / "web" / "dist" / "index.html").write_text("<html>checkout</html>")

    monkeypatch.setattr(app_module, "__file__", str(api_dir / "app.py"))
    assert _resolve_web_dist() == checkout / "web" / "dist"

    # Layout B: installed wheel — <site-packages>/no_human/web_dist. parents[3]
    # points at <venv>/lib/python3.X, outside the package, and holds nothing.
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    wheel_api = site / "no_human" / "api"
    wheel_api.mkdir(parents=True)
    (site / "no_human" / "web_dist").mkdir()
    (site / "no_human" / "web_dist" / "index.html").write_text("<html>wheel</html>")

    monkeypatch.setattr(app_module, "__file__", str(wheel_api / "app.py"))
    assert _resolve_web_dist() == site / "no_human" / "web_dist"

    # Neither present: fall back to the checkout path, so the "board was never
    # built" message names the directory a developer is expecting to see.
    bare = tmp_path / "bare"
    bare_api = bare / "src" / "no_human" / "api"
    bare_api.mkdir(parents=True)
    monkeypatch.setattr(app_module, "__file__", str(bare_api / "app.py"))
    assert _resolve_web_dist() == bare / "web" / "dist"


def test_resolve_web_dist_ignores_a_directory_with_no_index(tmp_path, monkeypatch):
    """An empty `web/dist` must not count as a board.

    `npm run build` writes into an existing directory, and an interrupted or
    cleaned build leaves the directory behind with no index.html. The old check
    was `_WEB_DIST.exists()`, which was true for that husk and mounted a board
    whose every request 404s at the filesystem layer.
    """
    app_module = importlib.import_module("no_human.api.app")
    _resolve_web_dist = app_module._resolve_web_dist

    root = tmp_path / "checkout"
    api_dir = root / "src" / "no_human" / "api"
    api_dir.mkdir(parents=True)
    (root / "web" / "dist").mkdir(parents=True)  # husk: exists, but empty

    site_pkg = root / "src" / "no_human" / "web_dist"
    site_pkg.mkdir(parents=True)
    (site_pkg / "index.html").write_text("<html>real</html>")

    monkeypatch.setattr(app_module, "__file__", str(api_dir / "app.py"))
    assert _resolve_web_dist() == site_pkg


# --------------------------------------------------------------------------- #
# Real-artifact tier — builds and installs the actual wheel                     #
# --------------------------------------------------------------------------- #

# Runs `uv build` + `uv venv` + `uv pip install` + boots a server: ~1 minute on
# a warm uv cache. Deselect with -m "not slow".
pytestmark_reason_uv = "uv is not on PATH — cannot build the wheel"
pytestmark_reason_board = (
    "web/dist is not built in this checkout, so there is no board to package. "
    "It is a gitignored build artifact; run `cd web && npm install && npm run "
    "build` first. (CI's python job has no npm, so this is expected there — "
    "the packaging claim is verified on the machine that cuts releases.)"
)

# The script that runs INSIDE the clean venv. It must not import anything from
# the repo — that is the whole point — so it is passed as source text and talks
# back over stdout as JSON.
_PROBE = r"""
import json, re, sys, threading, time, urllib.request, socket
import uvicorn
import no_human
from no_human.api.app import app, _WEB_DIST

out = {"pkg": no_human.__file__, "web_dist": str(_WEB_DIST)}

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()

cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()

deadline = time.time() + 30
while not server.started and time.time() < deadline:
    time.sleep(0.05)
out["started"] = server.started

def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

status, body = get("/")
out["root_status"] = status
out["root_body"] = body.decode("utf-8", "replace")[:4000]

m = re.search(r'/assets/(index-[\w-]+\.js)', out["root_body"])
out["asset_name"] = m.group(1) if m else None
if m:
    st, data = get("/assets/" + m.group(1))
    out["asset_status"], out["asset_bytes"] = st, len(data)

out["api_404_status"] = get("/api/definitely-not-a-route")[0]
server.should_exit = True
print("PROBE_JSON:" + json.dumps(out))
"""


def _run_probe(venv_python: Path) -> dict:
    # The whole point is a CLEAN import. The developer loop this repo documents
    # (`PYTHONPATH=<checkout>/src pytest …`) would leak the checkout into the
    # probe via inherited env and make it import the repo, not the wheel — the
    # exact failure mode this test exists to catch, reported backwards.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([str(venv_python), "-c", _PROBE],
                          capture_output=True, text=True, timeout=180, env=env,
                          cwd=tempfile.gettempdir())  # never run from the repo
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith("PROBE_JSON:")), None)
    assert line, (f"probe produced no result\n--- stdout ---\n{proc.stdout}\n"
                  f"--- stderr ---\n{proc.stderr}")
    return json.loads(line[len("PROBE_JSON:"):])


@pytest.mark.slow
def test_wheel_installed_in_a_clean_venv_serves_the_board(tmp_path):
    """Build the real wheel, install it clean, and read the board off a socket.

    This is the test that would have failed before the packaging fix: every
    in-repo test passed while `pip install no-human` shipped a UI-less CLI.
    """
    if shutil.which("uv") is None:
        pytest.skip(pytestmark_reason_uv)
    if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip(pytestmark_reason_board)

    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert build.returncode == 0, f"uv build failed:\n{build.stderr}"
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    venv = tmp_path / "venv"
    mk = subprocess.run(["uv", "venv", str(venv), "--python", "3.12"],
                        capture_output=True, text=True, timeout=300)
    assert mk.returncode == 0, f"uv venv failed:\n{mk.stderr}"
    venv_python = venv / ("Scripts" if os.name == "nt" else "bin") / "python"

    inst = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), str(wheels[0])],
        capture_output=True, text=True, timeout=900,
        # Do not let the repo's own venv leak in and make a boardless wheel pass.
        env={**os.environ, "VIRTUAL_ENV": str(venv)},
    )
    assert inst.returncode == 0, f"install failed:\n{inst.stderr}"

    result = _run_probe(venv_python)

    # Really the installed copy, not the repo.
    assert str(REPO_ROOT) not in result["pkg"], (
        f"probe imported the repo, not the wheel: {result['pkg']}")
    assert "site-packages" in result["pkg"], result["pkg"]
    assert result["started"], "server never came up"

    # The board itself.
    assert result["root_status"] == 200, (
        f"GET / returned {result['root_status']}, body: {result['root_body'][:400]}")
    assert 'id="root"' in result["root_body"], (
        "GET / did not return the board's app shell:\n" + result["root_body"][:400])

    # index.html is worthless without the bundle it references — that is a
    # separate directory in the package and was the likelier thing to be
    # dropped, so it is fetched rather than assumed.
    assert result["asset_name"], "index.html referenced no JS bundle"
    assert result["asset_status"] == 200, (
        f"/assets/{result['asset_name']} returned {result['asset_status']}")
    assert result["asset_bytes"] > 10_000, (
        f"JS bundle was {result['asset_bytes']} bytes — truncated?")

    # The SPA catch-all must still not swallow unknown API routes.
    assert result["api_404_status"] == 404, result["api_404_status"]

    _assert_missing_board_is_honest(venv_python, tmp_path)


def _assert_missing_board_is_honest(venv_python: Path, tmp_path: Path) -> None:
    """With the board removed, `GET /` must explain itself, not just 404.

    Folded into the slow test rather than given its own, because it needs the
    same expensive clean install and simply deletes the board from it.
    """
    site = next((venv_python.parent.parent / "lib").glob("python3.*/site-packages"))
    board = site / "no_human" / "web_dist"
    assert board.is_dir(), board
    shutil.rmtree(board)

    result = _run_probe(venv_python)

    assert result["root_status"] == 503, (
        "a missing board must not answer 200 or a bare 404; got "
        f"{result['root_status']}: {result['root_body'][:300]}")
    body = result["root_body"]
    # The two things a user needs: what is wrong, and that the CLI still works.
    assert "web board is not installed" in body, body[:300]
    assert "npm run build" in body, body[:300]
    assert "nh task" in body, body[:300]
    # And a real API 404 must stay a 404 rather than become the notice.
    assert result["api_404_status"] == 404, result["api_404_status"]
