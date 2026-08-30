import subprocess, sys
from pathlib import Path
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_context_files.py"

def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

def test_clean_index_passes(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = tmp_path / "MEMORY.md"; idx.write_text("# t\n- [A](a.md) — one\n")
    r = run("--memory", str(idx)); assert r.returncode == 0 and "VERDICT=OK" in r.stdout

def test_duplicate_link_target_fails(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = tmp_path / "MEMORY.md"; idx.write_text("- [A](a.md) — one\n- [B](a.md) — two\n")
    r = run("--memory", str(idx)); assert r.returncode == 1 and "duplicate target a.md" in r.stdout

def test_dangling_link_fails(tmp_path):
    idx = tmp_path / "MEMORY.md"; idx.write_text("- [A](missing.md) — one\n")
    r = run("--memory", str(idx)); assert r.returncode == 1 and "missing target missing.md" in r.stdout

def test_long_line_fails(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = tmp_path / "MEMORY.md"; idx.write_text("- [A](a.md) — " + "y" * 200 + "\n")
    r = run("--memory", str(idx)); assert r.returncode == 1 and "line 1 is 214 chars" in r.stdout

def test_byte_cap_fails(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = tmp_path / "MEMORY.md"; idx.write_text("".join(f"- [A{i}](a{i}.md) — z\n" for i in range(150)))
    for i in range(150): (tmp_path / f"a{i}.md").write_text("x")
    r = run("--memory", str(idx), "--max-bytes", "1000"); assert r.returncode == 1 and "bytes" in r.stdout

def test_claude_md_line_cap(tmp_path):
    c = tmp_path / "CLAUDE.md"; c.write_text("\n" * 201)
    r = run("--claude", str(c)); assert r.returncode == 1 and "201 lines" in r.stdout

def test_absent_claude_is_skipped_not_crash(tmp_path):
    # public export ships the CI step but no instruction file — must pass, not FileNotFoundError-crash
    r = run("--claude", str(tmp_path / "CLAUDE.md")); assert r.returncode == 0 and "absent" in r.stdout
