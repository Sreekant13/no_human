"""SCRUM-24: pure-function tests for the intake surface classifier."""

from no_human.intake.surface_advisory import classify_surfaces, surface_advisory


def test_single_surface_no_advisory():
    files = ["src/a.py", "src/b.py"]
    assert classify_surfaces(files) == {"backend"}
    assert surface_advisory(files) is None


def test_backend_plus_frontend():
    files = ["src/a.py", "web/b.js"]
    surfaces = classify_surfaces(files)
    assert surfaces == {"backend", "frontend"}
    advisory = surface_advisory(files)
    assert advisory is not None
    assert "backend" in advisory
    assert "frontend" in advisory


def test_desktop_surface():
    files = ["desktop/main.ts", "src/x.py"]
    assert classify_surfaces(files) == {"backend", "desktop"}
    assert surface_advisory(files) is not None


def test_docs_surface_dir_and_md():
    assert classify_surfaces(["docs/guide.md"]) == {"docs"}
    files = ["src/a.py", "README.md"]
    assert classify_surfaces(files) == {"backend", "docs"}
    assert surface_advisory(files) is not None


def test_tests_only_excluded():
    files = [
        "src/a.py",
        "tests/test_a.py",
        "src/a_test.py",
        "web/__tests__/x.js",
        "conftest.py",
    ]
    assert classify_surfaces(files) == {"backend"}
    assert surface_advisory(files) is None


def test_markdown_item_with_description():
    files = ["src/foo.py — add validation", "web/bar.js | tweak"]
    assert classify_surfaces(files) == {"backend", "frontend"}


def test_unknown_paths_contribute_nothing():
    files = ["Makefile", "pyproject.toml"]
    assert classify_surfaces(files) == set()
    assert surface_advisory(files) is None
