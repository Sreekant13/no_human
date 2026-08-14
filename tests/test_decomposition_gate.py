"""The decomposition feature gate this file pinned was removed 2026-08-12 —
see OPERATOR DECISION 2026-08-12 (A1). `decomposition.enabled` was reachable
from nothing (default False, no enabler), so the DECOMPOSE_PLAN marker path
and the LeadAgent hand-off it fed no longer exist in
`no_human.core.orchestrator`; this file's removal is a legitimate net
test-count reduction. `decomposition.enabled=true` now raises a startup
`ConfigError` instead — see
tests/test_config.py::test_load_config_rejects_decomposition_enabled.
"""
