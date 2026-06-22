"""`nh onboard`: derive a ProjectProfile from a repo's own declarations, then
PROVE each command by running it.

Two phases, deliberately split (design fork resolved 2026-06-22):

  - DERIVE candidate install/test/lint commands from what the repo itself
    declares — lockfiles, ``package.json`` scripts, Makefile targets, CI yaml —
    never a hardcoded ``if repo == "metrics-core"``. The deriver is pluggable:
    ``DeclarationDeriver`` (deterministic, the default) reads the declarations
    directly; ``AgentDeriver`` runs a bounded *read-only* recon session for
    repos whose declarations are nonstandard.

  - PROVE each candidate by **running it in a direct subprocess** and checking
    the exit status. Proving is never delegated to an agent: ``proven['test_cmd']``
    must mean *that exact command string* exited clean in that cwd, on the same
    execution path the orchestrator will later use (``testing.runner``). An agent
    in the prove loop could silently mutate the command (add a flag, install a
    dep, ``cd`` away) and record a proof for a command we never actually run —
    that is faking a step. Adaptiveness helps when *deriving* candidates (it
    cannot fake a signal there) and corrupts the signal when *proving*. If a
    command only passes after ad-hoc fixes beyond ``install_cmd``, the profile is
    genuinely not usable yet and stays unproven for a human to see.

The result is a proven-but-unconfirmed profile, proposed to a human. Confirming
it (``nh onboard <repo> --confirm``) reuses the ``nh learnings`` gate pattern:
nothing is trusted until a human confirms, and a profile drives a task only when
``confirmed AND proven['test_cmd']`` (``ProjectProfile.is_usable``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .profile import ProjectProfile
from .testing import runner

# Commands we prove, in the order we prove them. install runs first so the test
# command's dependencies exist; test is the trust anchor; lint is best-effort.
_KINDS = ("install", "test", "lint")


@dataclass
class CommandCandidate:
    kind: str            # "install" | "test" | "lint"
    command: str
    source: str          # which declaration it came from, e.g. "package.json:scripts.test"


@dataclass
class DerivedCommands:
    ecosystem: str = ""
    candidates: list[CommandCandidate] = field(default_factory=list)
    ci: dict[str, Any] = field(default_factory=dict)
    human_gated_steps: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # all declarations consulted

    def of_kind(self, kind: str) -> list[CommandCandidate]:
        return [c for c in self.candidates if c.kind == kind]


@dataclass
class ProveOutcome:
    kind: str
    command: str
    ok: bool
    exit_code: int
    output: str
    source: str

    @property
    def summary(self) -> str:
        verdict = "PROVEN" if self.ok else "FAILED"
        return f"[{verdict}] {self.kind}: {self.command}  (from {self.source}, exit {self.exit_code})"


@dataclass
class OnboardResult:
    profile: ProjectProfile
    proofs: list[ProveOutcome]


# --------------------------------------------------------------------------- #
# Derivers                                                                     #
# --------------------------------------------------------------------------- #


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _make_targets(makefile: Path) -> set[str]:
    targets: set[str] = set()
    for line in _read_text(makefile).splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", line)
        if m:
            targets.add(m.group(1))
    return targets


class DeclarationDeriver:
    """Derive candidate commands from a repo's own declared build/test config.

    Reads only shallow, well-known files — never a deep recursive walk on a large
    repo. Produces candidates in priority order (most-specific declaration
    first); the engine proves them in that order and keeps the first that runs
    clean.
    """

    def derive(self, repo_path: str | Path) -> DerivedCommands:
        repo = Path(repo_path).expanduser()
        d = DerivedCommands()

        self._detect_ci(repo, d)
        self._derive_node(repo, d)
        self._derive_python(repo, d)
        self._derive_maven(repo, d)
        self._derive_make(repo, d)
        return d

    # -- CI backend + human gates (declaration-driven) -- #

    def _detect_ci(self, repo: Path, d: DerivedCommands) -> None:
        if (repo / ".gitlab-ci.yml").exists():
            d.ci = {"backend": "gitlab"}
            d.sources.append(".gitlab-ci.yml")
        elif (repo / ".github" / "workflows").is_dir() and any(
            (repo / ".github" / "workflows").glob("*.y*ml")
        ):
            d.ci = {"backend": "github_actions"}
            d.sources.append(".github/workflows")
        if (repo / "Jenkinsfile").exists():
            d.human_gated_steps.append("build/CI gated on Jenkins (Jenkinsfile)")
            d.sources.append("Jenkinsfile")

    # -- Node -- #

    def _derive_node(self, repo: Path, d: DerivedCommands) -> None:
        pkg = repo / "package.json"
        if not pkg.exists():
            return
        try:
            data = json.loads(_read_text(pkg) or "{}")
        except json.JSONDecodeError:
            return
        scripts = data.get("scripts") or {}
        d.ecosystem = d.ecosystem or "node"
        d.sources.append("package.json")
        if (repo / "package-lock.json").exists():
            install = "npm ci"
        elif (repo / "yarn.lock").exists():
            install = "yarn install --frozen-lockfile"
        elif (repo / "pnpm-lock.yaml").exists():
            install = "pnpm install --frozen-lockfile"
        else:
            install = "npm install"
        d.candidates.append(CommandCandidate("install", install, "package.json"))
        if "test" in scripts:
            d.candidates.append(
                CommandCandidate("test", "npm test", "package.json:scripts.test")
            )
        if "lint" in scripts:
            d.candidates.append(
                CommandCandidate("lint", "npm run lint", "package.json:scripts.lint")
            )

    # -- Python (pytest) -- #

    def _derive_python(self, repo: Path, d: DerivedCommands) -> None:
        if not runner._looks_like_pytest(repo):
            return
        d.ecosystem = d.ecosystem or "python-pytest"
        pyproject = _read_text(repo / "pyproject.toml")
        if (repo / "uv.lock").exists():
            d.candidates.append(CommandCandidate("install", "uv sync", "uv.lock"))
            test_cmd, run_prefix = "uv run pytest -q", "uv run "
            d.sources.append("uv.lock")
        elif (repo / "poetry.lock").exists():
            d.candidates.append(CommandCandidate("install", "poetry install", "poetry.lock"))
            test_cmd, run_prefix = "poetry run pytest -q", "poetry run "
            d.sources.append("poetry.lock")
        else:
            reqs = sorted(repo.glob("requirements*.txt"))
            if reqs:
                d.candidates.append(CommandCandidate(
                    "install", f"pip install -r {reqs[0].name}", reqs[0].name))
            test_cmd, run_prefix = "pytest -q", ""
            d.sources.append("pyproject.toml" if (repo / "pyproject.toml").exists() else "pytest")
        d.candidates.append(CommandCandidate("test", test_cmd, "python/pytest"))
        if "ruff" in pyproject:
            d.candidates.append(CommandCandidate("lint", f"{run_prefix}ruff check .", "pyproject.toml:ruff"))

    # -- Maven -- #

    def _derive_maven(self, repo: Path, d: DerivedCommands) -> None:
        if not (repo / "pom.xml").exists():
            return
        d.ecosystem = d.ecosystem or "maven"
        d.sources.append("pom.xml")
        d.candidates.append(CommandCandidate("install", "mvn -q -DskipTests install", "pom.xml"))
        d.candidates.append(CommandCandidate("test", "mvn -q test", "pom.xml"))

    # -- Makefile (fallback for kinds nothing else declared) -- #

    def _derive_make(self, repo: Path, d: DerivedCommands) -> None:
        makefile = next((repo / n for n in ("Makefile", "makefile") if (repo / n).exists()), None)
        if makefile is None:
            return
        targets = _make_targets(makefile)
        added = False
        for kind in _KINDS:
            if kind in targets and not d.of_kind(kind):
                d.candidates.append(CommandCandidate(kind, f"make {kind}", f"Makefile:{kind}"))
                added = True
        if added:
            d.ecosystem = d.ecosystem or "make"
            d.sources.append("Makefile")


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class AgentDeriver:
    """Derive candidates via a bounded, read-only Agent SDK recon session — the
    path for repos whose declarations are nonstandard (the deterministic
    ``DeclarationDeriver`` covers the common ecosystems).

    The session may only *read* (the backend runs ``readonly=True``, so its
    PreToolUse guard blocks writes); it proposes candidates as a fenced JSON
    block which we parse. It never proves — proving stays a subprocess in the
    engine, so an agent cannot fake the proof signal.
    """

    PROMPT = (
        "You are onboarding a repository at {repo}. Do NOT modify anything — only "
        "read files (grep/glob/read).\n\n"
        "Inspect the repo's OWN declarations (lockfiles, package manifests, "
        "Makefile, CI yaml, README) and report the exact commands a developer "
        "runs to install dependencies, run the unit tests, and lint. Prefer "
        "commands the repo actually declares over generic guesses.\n\n"
        "Respond with ONLY a fenced ```json block of the form:\n"
        '{{"ecosystem": "...", "ci": {{"backend": "gitlab|github_actions|jenkins|"}}, '
        '"human_gated_steps": ["..."], '
        '"candidates": [{{"kind": "install|test|lint", "command": "...", "source": "<declaration>"}}]}}'
    )

    def __init__(self, backend: Any, *, max_turns: int = 12):
        self.backend = backend
        self.max_turns = max_turns

    async def derive(self, repo_path: str | Path) -> DerivedCommands:
        repo = Path(repo_path).expanduser()
        result = await self.backend.run(
            self.PROMPT.format(repo=repo),
            cwd=repo,
            max_turns=self.max_turns,
            effort="low",
        )
        return self.parse(result.final_text or "")

    @staticmethod
    def parse(text: str) -> DerivedCommands:
        blocks = _JSON_BLOCK.findall(text)
        if not blocks:
            return DerivedCommands()
        try:
            data = json.loads(blocks[-1])
        except json.JSONDecodeError:
            return DerivedCommands()
        cands = [
            CommandCandidate(c.get("kind", ""), c.get("command", ""), c.get("source", "agent"))
            for c in (data.get("candidates") or [])
            if c.get("kind") in _KINDS and c.get("command")
        ]
        return DerivedCommands(
            ecosystem=data.get("ecosystem", ""),
            candidates=cands,
            ci=data.get("ci") or {},
            human_gated_steps=list(data.get("human_gated_steps") or []),
            sources=["agent-recon"],
        )


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #


class OnboardEngine:
    """Derive candidates, prove each by running it, build a proposed profile."""

    def __init__(self, deriver: Any | None = None, *, prove_timeout: int = 600):
        self.deriver = deriver or DeclarationDeriver()
        self.prove_timeout = prove_timeout

    async def onboard(self, repo_path: str | Path) -> OnboardResult:
        repo = Path(repo_path).expanduser().resolve()
        derived = self.deriver.derive(repo)
        if inspect.isawaitable(derived):
            derived = await derived

        proofs: list[ProveOutcome] = []
        chosen: dict[str, CommandCandidate] = {}
        proven: dict[str, bool] = {}

        # Prove in install → test → lint order; the first candidate of each kind
        # that runs clean wins. install runs before test so deps are present.
        for kind in _KINDS:
            for cand in derived.of_kind(kind):
                outcome = await self._prove(repo, cand)
                proofs.append(outcome)
                if outcome.ok:
                    chosen[kind] = cand
                    proven[f"{kind}_cmd"] = True
                    break

        profile = ProjectProfile(
            repo_path=str(repo),
            ecosystem=derived.ecosystem,
            install_cmd=chosen["install"].command if "install" in chosen else "",
            test_cmd=chosen["test"].command if "test" in chosen else "",
            lint_cmd=chosen["lint"].command if "lint" in chosen else "",
            ci=derived.ci,
            human_gated_steps=derived.human_gated_steps,
            derived_from=sorted(set(derived.sources)),
            proven=proven,
            confirmed=False,
            notes=self._notes(derived, proofs),
        )
        return OnboardResult(profile=profile, proofs=proofs)

    async def _prove(self, repo: Path, cand: CommandCandidate) -> ProveOutcome:
        if cand.kind == "test":
            # Reuse the orchestrator's own test path so we prove what it will run.
            res = await asyncio.to_thread(
                runner.run_tests, repo, cand.command, timeout=self.prove_timeout
            )
            ok = res.ran and res.ok
            return ProveOutcome(
                cand.kind, cand.command, ok, 0 if ok else 1, res.output, cand.source
            )
        ok, code, out = await asyncio.to_thread(
            runner.run_command, repo, cand.command, timeout=self.prove_timeout
        )
        return ProveOutcome(cand.kind, cand.command, ok, code, out, cand.source)

    @staticmethod
    def _notes(derived: DerivedCommands, proofs: list[ProveOutcome]) -> str:
        proven = [p for p in proofs if p.ok]
        unproven = [p for p in proofs if not p.ok]
        lines = [f"derived by nh onboard (ecosystem: {derived.ecosystem or 'unknown'})"]
        if unproven:
            lines.append(
                "unproven candidates: "
                + "; ".join(f"{p.kind}={p.command!r} (exit {p.exit_code})" for p in unproven)
            )
        return " | ".join(lines)
