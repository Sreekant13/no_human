"""LeadAgent (compound task decomposition) was removed 2026-08-12 — see
OPERATOR DECISION 2026-08-12 (A1). The gate it required
(`decomposition.enabled`, default False) was reachable from nothing, so this
file's removal is a legitimate net test-count reduction. `no_human.core
.lead_agent` no longer exists; `decomposition.enabled=true` now raises a
startup `ConfigError` instead — see
tests/test_config.py::test_load_config_rejects_decomposition_enabled.
"""
