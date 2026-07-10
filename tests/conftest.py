"""Suite-wide fixtures (EH1): no test eats a real production backoff.

The fast suite claimed ~50s and took ~12 minutes; part of that was real
sleeps leaking out of retry paths (a 30s PR-open retry pause, 120s CI infra
backoffs) whenever a test tripped them. Production delays are class/module
constants precisely so this file can zero them for every test — a test that
WANTS to observe a delay can set it back explicitly.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_backoffs(monkeypatch):
    from no_human.core.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "PR_OPEN_RETRY_DELAY", 0)
    # CI infra backoffs (module constants, 120s each — CLAUDE.md's 2-minute
    # retry rule; the tests that exercise retries patch sleep themselves, but
    # one unpatched path used to cost 2 real minutes).
    import no_human.ci.gitlab as _gl
    import no_human.ci.jenkins as _jk
    monkeypatch.setattr(_gl, "_INFRA_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(_jk, "_INFRA_BACKOFF_SECONDS", 0)
