"""The UI-evidence runner: a coder-authored browser walk, harness-executed.

TRUST BOUNDARY. The coder writes ``.no_human/ui_evidence.json`` — a small,
bounded manifest choosing which selectors to click and which moments to
screenshot. This module is what the HARNESS runs, not the coder: it drives
a real (or injected fake, in tests) Playwright page through those steps and
captures whatever the page actually showed — screenshots, a video, console
errors — into ``out_dir``. A screenshot proves what the page rendered at the
instant that step ran, and nothing more: not that the feature works, not
that anything else on the page is correct, not that the steps chosen were
the right ones. The coder chose the walk; the harness took the pictures.

Mirrors the shape of :mod:`no_human.testing.repro_gate`: a small manifest
under ``.no_human/**``, never committed (``.no_human/`` is excluded from
every commit — see ``MANIFEST``), read by a pure function that degrades
loudly (``not_run`` with a named reason) rather than raising or silently
skipping.

Playwright is an OPTIONAL dependency (the ``e2e`` extras group,
``pyproject.toml``) — it is imported lazily, only inside :func:`run`, via
the :func:`_import_playwright` seam, so this module and its test suite work
with no browser installed. :func:`run` never raises; every failure mode is a
``UiEvidenceResult`` with a documented reason.

Conventions: step indices are 0-based everywhere (manifest order); artefact
paths recorded in the result are bare filenames relative to ``out_dir``
(shots and the video are always co-located there); writing artefacts is an
idempotent overwrite — a re-run replaces same-named files but does not
delete stale files left by a prior run with different shot names, so a
renderer should trust ``result.json``, not a directory listing. This module
never touches ``~/.no_human`` — ``out_dir`` is entirely caller-supplied.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import struct
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

MANIFEST = ".no_human/ui_evidence.json"
SCHEMA_HINT = (
    '{"base_url": "http://127.0.0.1:5173", "steps": [{"goto": "/"}, ...]}'
)

_ACTIONS = ("goto", "wait_for", "click", "fill", "shot", "assert_text", "press")
_MAX_STEPS = 40
_MAX_SHOTS = 12
_DEFAULT_TIMEOUT_MS = 10_000
_MAX_TIMEOUT_MS = 60_000
_SHOT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_PROBE_TIMEOUT_S = 5.0
_MAX_CONSOLE = 50
_CONSOLE_CHARS = 300
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_FINAL_SHOT_TIMEOUT_S = 10.0


def default_out_dir(task_id: str) -> Path:
    """A fresh scratch directory for one `run()` call's raw artifacts
    (shots, video, `manifest.json`/`console.json`/`result.json`) — never
    inside the target repo (this module's docstring: "never touches
    ``~/.no_human``"; a caller does not need it to, either — it is a plain
    OS temp directory, cleaned up by the caller once it has read back
    whatever it wants to keep, e.g. `core/orchestrator.py`'s attempt-time
    delivery step, which copies out only the specific shot/video files it
    is about to commit elsewhere and never trusts this directory to
    persist).

    Centralized here (D1.2) rather than left to each caller's own
    `tempfile.mkdtemp` call so there is exactly one place that decides the
    naming convention — a caller can still pass any `out_dir` it likes
    directly to :func:`run`; this is a convenience, not a requirement.
    """
    return Path(tempfile.mkdtemp(prefix=f"nh-ui-evidence-{task_id[:8]}-"))


@dataclass(frozen=True)
class Step:
    action: str
    value: str
    text: str | None
    timeout_ms: int
    index: int


@dataclass(frozen=True)
class Manifest:
    base_url: str
    viewport: dict
    steps: tuple[Step, ...]
    raw: dict


@dataclass
class UiEvidenceResult:
    verdict: str  # "ran" | "not_run" | "failed"
    reason: str = ""
    shots: list = field(default_factory=list)
    video: str | None = None
    console_errors: list = field(default_factory=list)
    steps_run: int = 0
    steps_total: int = 0
    duration_s: float = 0.0
    manifest: dict = field(default_factory=dict)
    failed_step: int | None = None
    failed_action: str | None = None

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "shots": list(self.shots),
            "video": self.video,
            "console_errors": list(self.console_errors),
            "steps_run": self.steps_run,
            "steps_total": self.steps_total,
            "duration_s": round(self.duration_s, 3),
            "manifest": self.manifest,
            "failed_step": self.failed_step,
            "failed_action": self.failed_action,
        }


# --------------------------------------------------------------------------
# Validation — `_load` (file → JSON) + `_parse` (JSON → Manifest) are the
# ONE shared path for both `manifest_problem` and `read_manifest`, so the
# two cannot drift into disagreeing about what is valid.
# --------------------------------------------------------------------------


def _load(repo_path: Path) -> tuple[object | None, str | None]:
    """Read + JSON-decode the manifest file. Returns (data, problem).

    Both None means the file is absent. `problem` set means data is None —
    the file exists but could not be read or parsed.
    """
    p = repo_path / MANIFEST
    try:
        text = p.read_text(errors="replace")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{MANIFEST} is present but unreadable ({exc.__class__.__name__})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{MANIFEST} is not valid JSON ({exc.msg} at line {exc.lineno})"
    return data, None


def _hostname_problem(value: str, parsed) -> str | None:
    hostname = parsed.hostname or ""
    if hostname not in _LOCAL_HOSTS:
        return (
            f"must be http(s) on 127.0.0.1 or localhost — the runner drives "
            f"the attempt's own dev server, never a remote host; got `{value}`"
        )
    return None


def _base_url_problem(value) -> str | None:
    if not isinstance(value, str) or not value:
        return f"base_url is missing or not a non-empty string — expected {SCHEMA_HINT}"
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        return f"base_url `{value}` is not a parseable URL ({exc})"
    if parsed.scheme not in ("http", "https"):
        return (
            f"base_url must be http(s) on 127.0.0.1 or localhost — the "
            f"runner drives the attempt's own dev server, never a remote "
            f"host; got `{value}`"
        )
    return _hostname_problem(value, parsed)


def _goto_local_problem(value: str) -> str | None:
    """None for a value that stays on the attempt's own host once resolved.

    A bare relative path (``/foo``, ``foo/bar``) always resolves under the
    manifest's own ``base_url`` host via ``urljoin`` and is safe. A value
    carrying a netloc — whether a full absolute URL (``http://evil.com/``)
    OR a protocol-relative one (``//evil.com/x``, empty scheme, non-empty
    netloc — ``urljoin`` still replaces the authority component with it) —
    must be checked against the same local-host rule as ``base_url``.
    """
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        return f"is not a parseable URL ({exc})"
    if not parsed.scheme and not parsed.netloc:
        return None
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return f"scheme must be http(s), got `{parsed.scheme}`"
    return _hostname_problem(value, parsed)


def _viewport_problem(viewport) -> str | None:
    if not isinstance(viewport, dict):
        return f"viewport must be an object — expected {SCHEMA_HINT}"
    for name in ("width", "height"):
        v = viewport.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > 10000:
            return f"viewport.{name} must be a positive integer <= 10000"
    return None


def _step_problem(raw_step, i: int, shot_names: set) -> tuple[Step | None, str | None]:
    if not isinstance(raw_step, dict):
        return None, f"step {i} is {type(raw_step).__name__}, not an object"
    extra = set(raw_step) - set(_ACTIONS) - {"text", "timeout_ms"}
    if extra:
        return None, f"step {i} has unknown key(s): {', '.join(sorted(extra))}"
    action_keys = [k for k in _ACTIONS if k in raw_step]
    if not action_keys:
        return None, f"step {i} has no action key — expected one of {_ACTIONS}"
    if len(action_keys) > 1:
        return None, f"step {i} has multiple action keys: {', '.join(sorted(action_keys))}"
    action = action_keys[0]
    value = raw_step[action]
    if not isinstance(value, str) or not value:
        return None, f"step {i} ({action}) value must be a non-empty string"

    text = raw_step.get("text")
    if action == "fill":
        if not isinstance(text, str) or not text:
            return None, f"step {i} (fill) requires a non-empty \"text\""
    elif "text" in raw_step:
        return None, f"step {i} ({action}) — \"text\" is only valid on a \"fill\" step"

    if action == "goto":
        problem = _goto_local_problem(value)
        if problem:
            return None, f"step {i} (goto) {problem}"

    if action == "shot":
        if not _SHOT_RE.match(value):
            return None, (
                f"step {i} (shot) name `{value}` does not match "
                f"{_SHOT_RE.pattern}"
            )
        if value == "final":
            return None, f"step {i} (shot) uses the reserved name `final`"
        if value in shot_names:
            return None, f"step {i} (shot) duplicates shot name `{value}`"

    timeout_ms = raw_step.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or timeout_ms <= 0
        or timeout_ms > _MAX_TIMEOUT_MS
    ):
        return None, (
            f"step {i} ({action}) timeout_ms must be an integer in "
            f"(0, {_MAX_TIMEOUT_MS}]"
        )

    step = Step(
        action=action,
        value=value,
        text=text if action == "fill" else None,
        timeout_ms=timeout_ms,
        index=i,
    )
    return step, None


def _parse(data) -> tuple[Manifest | None, str | None]:
    """JSON document -> (Manifest, None) or (None, problem sentence)."""
    if not isinstance(data, dict):
        return None, (
            f"{MANIFEST} top level is {type(data).__name__}, not an object "
            f"— expected {SCHEMA_HINT}"
        )

    base_url = data.get("base_url")
    problem = _base_url_problem(base_url)
    if problem:
        return None, f"{MANIFEST}: {problem}"

    viewport = data.get("viewport", _DEFAULT_VIEWPORT)
    if viewport is not _DEFAULT_VIEWPORT:
        problem = _viewport_problem(viewport)
        if problem:
            return None, f"{MANIFEST}: {problem}"

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None, (
            f'{MANIFEST}: "steps" is missing or not a list — expected {SCHEMA_HINT}'
        )
    if not steps_raw:
        return None, f'{MANIFEST}: "steps" is empty'
    if len(steps_raw) > _MAX_STEPS:
        return None, (
            f'{MANIFEST}: "steps" has {len(steps_raw)} entries, more than '
            f"the max {_MAX_STEPS}"
        )

    steps: list[Step] = []
    shot_names: set = set()
    shot_count = 0
    for i, raw_step in enumerate(steps_raw):
        step, problem = _step_problem(raw_step, i, shot_names)
        if problem:
            return None, f"{MANIFEST}: {problem}"
        steps.append(step)
        if step.action == "shot":
            shot_names.add(step.value)
            shot_count += 1

    if shot_count > _MAX_SHOTS:
        return None, (
            f"{MANIFEST}: {shot_count} shot steps, more than the max {_MAX_SHOTS}"
        )

    manifest = Manifest(
        base_url=base_url,
        viewport=dict(viewport) if isinstance(viewport, dict) else dict(_DEFAULT_VIEWPORT),
        steps=tuple(steps),
        raw=data,
    )
    return manifest, None


def manifest_problem(repo_path: Path) -> str | None:
    """One sentence naming the first defect, or None if absent/usable."""
    p = repo_path / MANIFEST
    if not p.is_file():
        return None
    data, problem = _load(repo_path)
    if problem:
        return problem
    _, problem = _parse(data)
    return problem


def read_manifest(repo_path: Path) -> Manifest:
    """The parsed manifest. Raises ValueError with the problem sentence."""
    p = repo_path / MANIFEST
    if not p.is_file():
        raise ValueError(f"no {MANIFEST} manifest")
    data, problem = _load(repo_path)
    if problem:
        raise ValueError(problem)
    manifest, problem = _parse(data)
    if problem:
        raise ValueError(problem)
    return manifest


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


def _import_playwright():
    """The `async_playwright` factory, or None if playwright is not installed.

    The ONLY reference to `playwright` anywhere in this module — never
    imported at module scope, so importing `ui_evidence` never requires it.
    Tests monkeypatch this symbol directly.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    return async_playwright


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx from the probed server is the answer
    ("it is up"), never an instruction to open a second connection. Without
    this the default handler would follow `Location:` to ANY host, which is
    the one way a loopback-validated base_url could make this process talk
    to the outside."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reachable(base_url: str) -> bool:
    """One local HTTP GET probe. Fails closed (False) on anything but 2xx/3xx/4xx/5xx.

    A `ProxyHandler({})` opener means a shell HTTP_PROXY cannot reroute a
    127.0.0.1 probe to somewhere else, and `_NoRedirect` means a `Location:`
    header cannot either — the probe opens exactly one connection, to
    base_url, and follows nothing.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        opener.open(base_url, timeout=_PROBE_TIMEOUT_S)
        return True
    except urllib.error.HTTPError:
        # A dev server 404/500 on `/` is still a running server.
        return True
    except Exception:
        return False


def _png_size(data: bytes, fallback: dict) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", data[16:24])
        except struct.error:
            pass
    return fallback["width"], fallback["height"]


async def _maybe_await(value):
    """Awaits `value` if it is awaitable — tolerates a sync fake in tests."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _open(out_dir: Path, viewport: dict, launch):
    """Returns (page, closer). `closer` is an async no-arg callable.

    Closing the CONTEXT (not just the page) is what flushes the `.webm`
    recording to disk, so the closer always prefers `page.context.close()`.
    """
    if launch is not None:
        page = await launch(out_dir, viewport)

        async def _injected_closer() -> None:
            context = getattr(page, "context", None)
            if context is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(context.close())
                    return
            with contextlib.suppress(Exception):
                await _maybe_await(page.close())

        return page, _injected_closer

    async_playwright = _import_playwright()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport=viewport,
        record_video_dir=str(out_dir),
        record_video_size=viewport,
        ignore_https_errors=True,
    )
    page = await context.new_page()

    async def _real_closer() -> None:
        with contextlib.suppress(Exception):
            await context.close()
        with contextlib.suppress(Exception):
            await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()

    return page, _real_closer


async def _write_shot(page, out_dir: Path, name: str, step_index: int, shots: list) -> None:
    png = await _maybe_await(page.screenshot())
    (out_dir / f"{name}.png").write_bytes(png)
    width, height = _png_size(png, _DEFAULT_VIEWPORT)
    shots.append({
        "name": name,
        "path": f"{name}.png",
        "step_index": step_index,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(png).hexdigest(),
    })


async def _dispatch(page, step: Step, base_url: str, out_dir: Path, shots: list) -> None:
    action, value = step.action, step.value
    if action == "goto":
        await _maybe_await(page.goto(urljoin(base_url, value)))
    elif action == "wait_for":
        await _maybe_await(page.wait_for_selector(value))
    elif action == "click":
        await _maybe_await(page.click(value))
    elif action == "fill":
        await _maybe_await(page.fill(value, step.text))
    elif action == "press":
        # Global key press — Playwright's `Page.press(selector, key)` takes a
        # selector as its FIRST positional argument, so calling it with only
        # the key name would target a nonexistent selector. `Keyboard.press`
        # is the actual global key-press primitive and takes just the key.
        await _maybe_await(page.keyboard.press(value))
    elif action == "assert_text":
        locator = page.locator(f"text={value}")
        count = await _maybe_await(locator.count())
        if not count:
            raise RuntimeError(f"text not found: {value}")
    elif action == "shot":
        await _write_shot(page, out_dir, value, step.index, shots)
    else:  # pragma: no cover - validation forbids this
        raise RuntimeError(f"unknown action: {action}")


def _finalize_video(out_dir: Path) -> str | None:
    target = out_dir / "walk.webm"
    if target.exists():
        return "walk.webm"
    webms = sorted(out_dir.glob("*.webm"))
    if not webms:
        return None
    with contextlib.suppress(OSError):
        os.replace(webms[0], target)
    return "walk.webm" if target.exists() else None


def _write_artifacts(out_dir: Path, result: UiEvidenceResult) -> None:
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "manifest.json").write_text(json.dumps(result.manifest, indent=2))
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "console.json").write_text(
            json.dumps({"errors": list(result.console_errors)}, indent=2)
        )
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "result.json").write_text(json.dumps(result.as_dict(), indent=2))


async def _run_body(
    repo_path: Path, out_dir: Path, deadline_s: float, launch, start: float,
    result: UiEvidenceResult,
) -> None:
    p = repo_path / MANIFEST
    if not p.is_file():
        result.reason = "no manifest"
        return

    data, problem = _load(repo_path)
    manifest = None
    if problem is None:
        manifest, problem = _parse(data)
    if problem:
        result.reason = problem
        return

    result.manifest = manifest.raw
    result.steps_total = len(manifest.steps)

    if not _reachable(manifest.base_url):
        result.reason = f"app not reachable at {manifest.base_url}"
        return

    if launch is None and _import_playwright() is None:
        result.reason = "playwright not installed (uv sync --group e2e)"
        return

    try:
        page, closer = await _open(out_dir, manifest.viewport, launch)
    except Exception as exc:
        msg = str(exc).replace("\n", " ")[:300]
        result.reason = f"browser launch failed: {msg}"
        return

    console_errors: list[str] = []

    def _on_console(msg) -> None:
        with contextlib.suppress(Exception):
            if len(console_errors) < _MAX_CONSOLE and getattr(msg, "type", "error") == "error":
                console_errors.append(str(getattr(msg, "text", msg))[:_CONSOLE_CHARS])

    def _on_pageerror(exc) -> None:
        with contextlib.suppress(Exception):
            if len(console_errors) < _MAX_CONSOLE:
                console_errors.append(str(exc)[:_CONSOLE_CHARS])

    with contextlib.suppress(Exception):
        page.on("console", _on_console)
    with contextlib.suppress(Exception):
        page.on("pageerror", _on_pageerror)

    shots: list[dict] = []
    steps_run = 0
    verdict, reason = "ran", ""
    failed_step: int | None = None
    failed_action: str | None = None

    for i, step in enumerate(manifest.steps):
        remaining_s = deadline_s - (time.monotonic() - start)
        if remaining_s <= 0:
            verdict = "failed"
            reason = f"step {i} ({step.action}): deadline of {deadline_s:g}s exceeded"
            failed_step, failed_action = i, step.action
            break
        timeout_s = min(step.timeout_ms / 1000, remaining_s)
        deadline_capped = timeout_s < (step.timeout_ms / 1000) - 1e-9
        try:
            await asyncio.wait_for(
                _dispatch(page, step, manifest.base_url, out_dir, shots),
                timeout=timeout_s,
            )
        except TimeoutError:
            verdict = "failed"
            if deadline_capped:
                reason = f"step {i} ({step.action}): deadline of {deadline_s:g}s exceeded"
            else:
                reason = f"step {i} ({step.action}): timed out after {step.timeout_ms}ms"
            failed_step, failed_action = i, step.action
            break
        except Exception as exc:
            verdict = "failed"
            reason = f"step {i} ({step.action}): {exc}"
            failed_step, failed_action = i, step.action
            break
        steps_run += 1

    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            _write_shot(page, out_dir, "final", steps_run, shots),
            timeout=_FINAL_SHOT_TIMEOUT_S,
        )

    with contextlib.suppress(Exception):
        await closer()

    result.verdict = verdict
    result.reason = reason
    result.shots = shots
    result.video = _finalize_video(out_dir)
    result.console_errors = console_errors
    result.steps_run = steps_run
    result.failed_step = failed_step
    result.failed_action = failed_action


async def run(
    repo_path: Path, out_dir: Path, *, deadline_s: float = 120.0, launch=None,
) -> UiEvidenceResult:
    """Execute the coder-authored manifest, capturing evidence into `out_dir`.

    `launch` injects a page-producing async callable, `(out_dir, viewport)
    -> page`, in place of a real headless-chromium launch — the module's
    test seam. Never raises; every failure path returns a `UiEvidenceResult`
    with `verdict` in {"ran", "not_run", "failed"}.
    """
    start = time.monotonic()
    result = UiEvidenceResult(verdict="not_run")
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.reason = exc.__class__.__name__
        result.duration_s = time.monotonic() - start
        return result

    try:
        await _run_body(repo_path, out_dir, deadline_s, launch, start, result)
    except Exception as exc:
        result.verdict = "not_run"
        result.reason = exc.__class__.__name__

    result.duration_s = time.monotonic() - start
    with contextlib.suppress(Exception):
        _write_artifacts(out_dir, result)
    return result


def summary_line(result: UiEvidenceResult) -> str:
    if result.verdict == "not_run":
        return f"NOT RUN — {result.reason}"

    n_shots = len(result.shots)
    shot_word = "screenshot" if n_shots == 1 else "screenshots"

    if result.verdict == "failed":
        return f"failed at step {result.failed_step} ({result.failed_action}) — {n_shots} {shot_word}"

    video_part = "video" if result.video else "no video"
    n_err = len(result.console_errors)
    err_word = "console error" if n_err == 1 else "console errors"
    return f"ran — {n_shots} {shot_word}, {video_part}, {n_err} {err_word}"
