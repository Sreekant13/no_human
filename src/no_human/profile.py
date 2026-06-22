"""ProjectProfile — per-repo, human-confirmed build/test/CI recipe.

The mechanism that keeps no_human general: how to install / unit-test / lint a
repo, which CI to trigger and how to read it, and which steps are human-gated —
all *config derived from the repo's own declarations and proven by running*,
never hardcoded per repo (no ``if repo == "metrics-core"`` anywhere).

The YAML at ``<repo>/.no_human/project.yml`` is the human-confirmable source of
truth; ``Store`` mirrors it (with the confirmation flag) for the daemon. A
profile is only trusted once ``confirmed`` is true — `nh onboard` proposes it,
a human confirms via the same gate as `nh learnings`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROFILE_RELPATH = Path(".no_human") / "project.yml"


@dataclass
class ProjectProfile:
    repo_path: str
    ecosystem: str = ""                 # e.g. "python-pytest", "node", "maven"
    install_cmd: str = ""               # e.g. "uv sync"
    test_cmd: str = ""                  # unit tests, e.g. "uv run pytest -q"
    lint_cmd: str = ""                  # e.g. "uv run ruff check"
    ci: dict[str, Any] = field(default_factory=dict)        # mirrors config "ci" block
    human_gated_steps: list[str] = field(default_factory=list)
    # Provenance: which repo declarations each command was derived from, and
    # whether `nh onboard` proved it by running it. Trust requires proof.
    derived_from: list[str] = field(default_factory=list)
    proven: dict[str, bool] = field(default_factory=dict)   # cmd-key -> ran-clean
    confirmed: bool = False
    notes: str = ""

    # --- serialization ---------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "ecosystem": self.ecosystem,
            "install_cmd": self.install_cmd,
            "test_cmd": self.test_cmd,
            "lint_cmd": self.lint_cmd,
            "ci": self.ci,
            "human_gated_steps": self.human_gated_steps,
            "derived_from": self.derived_from,
            "proven": self.proven,
            "confirmed": self.confirmed,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectProfile":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # --- YAML in the repo ------------------------------------------------- #

    def yaml_path(self) -> Path:
        return Path(self.repo_path).expanduser() / PROFILE_RELPATH

    def save(self) -> Path:
        """Write the profile to ``<repo>/.no_human/project.yml``."""
        path = self.yaml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # repo_path is implied by location; keep it out of the on-disk file.
        body = {k: v for k, v in self.to_dict().items() if k != "repo_path"}
        path.write_text(yaml.safe_dump(body, sort_keys=False))
        return path

    @classmethod
    def load(cls, repo_path: str | Path) -> "ProjectProfile | None":
        path = Path(repo_path).expanduser() / PROFILE_RELPATH
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text()) or {}
        data["repo_path"] = str(Path(repo_path).expanduser())
        return cls.from_dict(data)

    # --- readiness -------------------------------------------------------- #

    @property
    def is_usable(self) -> bool:
        """A profile may drive a task only if a human confirmed it and its test
        command was proven to run."""
        return bool(self.confirmed and self.test_cmd and self.proven.get("test_cmd"))
