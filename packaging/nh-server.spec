# PyInstaller spec — onedir bytecode freeze of the nh server.
#
# Goal: a friend can run no_human without a Python toolchain and without
# reading the source. Two properties are load-bearing:
#
#   1. NO .py FILES IN THE BUNDLE. `collect_all()` copies a package's source
#      tree in as *data* — that shipped 88 readable .py files (48 websockets,
#      40 uvicorn) in the first spike. `collect_submodules()` bundles the same
#      modules as bytecode inside the PYZ instead, which is what we want.
#   2. web/dist LANDS AT <bundle>/web/dist. The server resolves the board with
#      `Path(__file__).resolve().parents[3] / "web" / "dist"`. Measured under a
#      frozen onedir build, `__file__` is <bundle>/_internal/no_human/api/app.py,
#      so parents[3] is the bundle root — the datas target below matches that
#      WITHOUT any change to the server source.

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Packages that resolve implementations by string at runtime, so static
# analysis alone under-collects them.
_DYNAMIC = ("uvicorn", "websockets", "anthropic", "claude_agent_sdk",
            "textual", "rich", "aiosqlite")

hiddenimports = []
for _pkg in _DYNAMIC:
    hiddenimports += collect_submodules(_pkg)

# Non-source assets only (textual ships .tcss stylesheets; without them the CLI
# TUI renders unstyled). Any .py caught here would defeat property 1, so it is
# filtered out explicitly rather than trusted.
#
# web/dist is deliberately NOT listed here. PyInstaller 6 puts every datas entry
# under the _internal contents dir, and rejects a `..` DEST_DIR outright
# ("DEST_DIR must not point outside of application's top-level directory"), so
# datas cannot reach <bundle>/web/dist — which is where the server looks.
# packaging/build-installer.sh copies the board there after COLLECT instead.
datas = []
for _pkg in ("textual", "rich"):
    datas += [(src, dst) for src, dst in collect_data_files(_pkg)
              if not src.endswith(".py")]

a = Analysis(
    [os.path.join(SPECPATH, "nh_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trimming the test/build toolchain a friend never runs, plus one product
    # package that must not leave this repo: `no_human.ci_gate` is the
    # glab/kubectl/metrics-core post-PR gate, written for one employer's pipeline
    # (operator decision D1, 2026-07-30). It stays in the source tree for
    # internal use and is excluded here instead. blockers/wake.py imports it
    # lazily inside a try/except that logs and returns None, and nothing else
    # imports the package, so the frozen server runs with the rung disabled.
    # Excluding the package drops its submodules with it (PyInstaller resolves
    # `no_human.ci_gate.gate` through the excluded parent).
    excludes=["pytest", "pytest_asyncio", "pytest_xdist", "playwright",
              "tkinter", "PyInstaller",
              "no_human.ci_gate",
              # The employer half of the publish guard's term inventory — its
              # docstring forbids publication in any form, and vendor_terms.py
              # ships an empty-list fallback for exactly this absence. Frozen
              # into every DMG until 2026-07-31; build-installer.sh now fails
              # the build if either module reappears in the PYZ table.
              "no_human.eval._vendor_terms_private"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nh",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="nh-server",
)
