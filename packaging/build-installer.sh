#!/usr/bin/env bash
# Build the no-source nh server bundle: React board -> PyInstaller onedir freeze.
#
# Output: packaging/dist/nh-server/{nh,_internal/,web/dist/}
# The board MUST sit at the bundle root (not under _internal): the server
# resolves it with Path(__file__).resolve().parents[3]/"web"/"dist", and under a
# frozen onedir build __file__ is <bundle>/_internal/no_human/api/app.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/packaging/dist"
BUNDLE="${OUT}/nh-server"

echo "==> building the board (web/dist is gitignored, so always rebuild)"
(cd "${ROOT}/web" && npm run build)

echo "==> freezing the server"
(cd "${ROOT}" && .venv/bin/pyinstaller --noconfirm \
  --distpath "${OUT}" --workpath "${OUT}/.work" \
  packaging/nh-server.spec)

# Runtime data the server locates via Path(__file__).resolve().parents[3], which
# under a frozen onedir build is the bundle root. Both are load-bearing:
# migrations/*.sql builds the schema on first run (without it the server aborts
# with "no such table: tasks"), and web/dist is the board.
echo "==> placing runtime data at the bundle root"
rm -rf "${BUNDLE}/web" "${BUNDLE}/migrations"
mkdir -p "${BUNDLE}/web"
cp -R "${ROOT}/web/dist" "${BUNDLE}/web/dist"
cp -R "${ROOT}/migrations" "${BUNDLE}/migrations"

echo "==> stripping build-machine provenance"
# PyInstaller copies the installed distribution's .dist-info verbatim, and for an
# EDITABLE install pip records the source checkout's ABSOLUTE PATH in
# direct_url.json:
#     {"url":"file:///Users/<user>/git/<employer>/.../no_human","dir_info":{"editable":true}}
# That shipped inside the .app in the Mac DMG — the maintainer's home directory
# and the employer-named parent directory, in an artifact handed to third
# parties. It is exactly what P1 exists to prevent, and no export rule could
# catch it: the DMG is BUILT, not exported, so EXPORT_CLASSIFICATION.txt never
# sees this file. Found by an independent reviewer, not by a test.
#
# ONLY direct_url.json is removed, NOT the .dist-info directory. Deleting the
# whole directory was tried first, on the assumption that a frozen app resolves
# no distributions -- it does: `nh --version` then dies in
# importlib/metadata/__init__.py:397 `from_name`. direct_url.json is the sole
# file carrying the path (verified by grep over the built bundle), and it is
# pip-install provenance that nothing reads at runtime.
find "${BUNDLE}/_internal" -path "*.dist-info/direct_url.json" -print -delete || true

echo "==> verifying"
py_count="$(find "${BUNDLE}" -name '*.py' | wc -l | tr -d ' ')"
if [ "${py_count}" != "0" ]; then
  echo "FAIL: ${py_count} .py files in the bundle (it must ship no source)" >&2
  find "${BUNDLE}" -name '*.py' >&2
  exit 1
fi
test -f "${BUNDLE}/web/dist/index.html" || {
  echo "FAIL: board missing at ${BUNDLE}/web/dist/index.html" >&2; exit 1; }
sql_count="$(find "${BUNDLE}/migrations" -name '*.sql' | wc -l | tr -d ' ')"
test "${sql_count}" -gt 0 || {
  echo "FAIL: no migrations in ${BUNDLE}/migrations" >&2; exit 1; }

# no_human.ci_gate is a post-PR gate wired to one specific CI estate. It is not
# general-purpose and must not ship.
# nh-server.spec excludes it; this is the check that the exclusion took, since
# a new import elsewhere pulls the package back into the freeze silently.
# Searching the bundle for the name cannot do this job: pure modules live in
# the zlib-compressed PYZ, so the string is absent from the bytes while the
# module is inside. PyInstaller's own module table is the readable inventory.
pyz_toc="$(find "${OUT}/.work/nh-server" -name 'PYZ-*.toc' | head -1)"
test -n "${pyz_toc}" || {
  echo "FAIL: no PYZ table under ${OUT}/.work/nh-server - cannot read what was frozen" >&2
  exit 1; }
gate_modules="$(grep -o 'no_human\.ci_gate[A-Za-z0-9_.]*' "${pyz_toc}" | sort -u || true)"
if [ -n "${gate_modules}" ]; then
  echo "FAIL: no_human.ci_gate is frozen into the bundle (D1: it must not ship)" >&2
  echo "${gate_modules}" >&2
  exit 1
fi
# Same check for the private half of the term inventory: hex-encoded
# terms plus their replacement mapping. Never distributable in any artifact.
private_terms="$(grep -o 'no_human\.eval\._vendor_terms_private' "${pyz_toc}" | sort -u || true)"
if [ -n "${private_terms}" ]; then
  echo "FAIL: no_human.eval._vendor_terms_private is frozen into the bundle (it must never ship)" >&2
  exit 1
fi

# No absolute build path may survive anywhere in the bundle. This is the check
# the DMG never had: the identity guard in the test suite SKIPS whenever
# desktop/dist is absent, which is every clean clone and every CI run, so it was
# dark exactly when it mattered. Checked here, where the artifact is made.
# -a, and the bare path as well as the file:// form. The first version of this
# gate used `grep -rl "file://${HOME}"` and a known-positive control exposed two
# holes in it: grep SKIPS BINARY FILES without -a, so a path baked into the
# frozen executable or a .so was invisible -- and PyInstaller is exactly the kind
# of tool that bakes build paths into binaries. The file:// prefix is also only
# how pip happens to write it; RECORD-style or compiled-in paths are bare.
# Verified with planted controls in both forms before being trusted.
leaked="$(grep -ral -e "file://${HOME}" -e "${HOME}/" "${BUNDLE}" 2>/dev/null || true)"
if [ -n "${leaked}" ]; then
  echo "FAIL: the bundle records this machine's build path (P1: no maintainer trace ships)" >&2
  echo "${leaked}" >&2
  exit 1
fi

echo "OK: ${BUNDLE} ($(du -sh "${BUNDLE}" | cut -f1)), 0 .py files, no ci_gate"
