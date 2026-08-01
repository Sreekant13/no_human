"""The three team-brain config keys, and nothing else.

(Named ``settings.py`` rather than the obvious ``config.py``: this repo's
documentation cites source as ``config.py:NNN`` and resolves the citation by
BASENAME, so a second ``config.py`` under ``src/no_human/`` makes every existing
citation ambiguous and fails ``tests/test_readme_claims.py``. Found by that test,
not by inspection.)

Deliberately three, and deliberately dumb. ``team_brain`` in ``config.yaml`` is
allowed exactly:

    team_brain:
      enabled: false            # nothing happens at all
      control_plane_url: ""     # ONE url; no region, account, table or ARN
      max_stale_days: 14        # stop injecting rules older than this

The hosted tier is built out of DynamoDB, Lambda, KMS, S3, Cognito, IAM session
tags and Terraform. None of those nouns appear here or anywhere else under
``src/no_human/`` — the client knows one URL, and the service's shape is
deliberately unlearnable from it. ``tests/test_brain_invariants.py`` greps for
them and fails the build if one appears.

No credential lives in config, ever. ``config.py::_reject_api_key_in_config``
makes that a hard rule for the Anthropic key; the brain credential obeys the
same principle by construction, because the only reader of a brain credential is
``brain/credentials.py`` and the only place it looks is
``~/.no_human/brain/credentials.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Wire schema the client understands. The control plane publishes
#: ``schema_version`` on every sync response and refuses to serve a store newer
#: than itself; the client mirrors that rule outward. A newer server means STOP
#: SYNCING and say so — never guess at fields this build does not know, and
#: never store them.
MAX_BRAIN_SCHEMA = 1


@dataclass(frozen=True)
class BrainConfig:
    enabled: bool
    control_plane_url: str
    max_stale_days: int

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.control_plane_url)


def read(config: Any) -> BrainConfig:
    """Read the three keys off a ``Config`` (or a plain dict).

    ``(config.get("team_brain") or {})`` rather than ``.get("team_brain", {})``:
    a hand-edited ``config.yaml`` with a bare ``team_brain:`` and its body
    commented out deep-merges to None, not to a dict — the same trap
    ``cli/commands.py::_bootstrap`` documents for ``llm:``.
    """
    raw = (config.get("team_brain") or {}) if config is not None else {}
    url = str(raw.get("control_plane_url") or "").strip().rstrip("/")
    try:
        stale = int(raw.get("max_stale_days", 14))
    except (TypeError, ValueError):
        stale = 14
    return BrainConfig(
        enabled=bool(raw.get("enabled", False)),
        control_plane_url=url,
        max_stale_days=max(0, stale),
    )
