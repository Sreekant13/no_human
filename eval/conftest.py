"""Keep pytest out of eval/ — it holds fixtures, not tests.

`reviewer_recall/cases/<id>/base/` holds materialised snapshots of product
source, including files named `tests/test_*.py`. `pyproject.toml` sets
`testpaths = ["tests"]`, so a bare `pytest` never sees them — but `pytest .`
or `pytest eval` would collect them, and several share a basename (three cases
carry a `tests/test_runner.py`), which pytest reports as an import-file
mismatch. They are fixtures, not tests: never collect them.

This file lives at `eval/` rather than inside the corpus so that its
`__pycache__` never lands in `cases/`, where `load_cases` iterates.
"""

collect_ignore_glob = ["*"]
