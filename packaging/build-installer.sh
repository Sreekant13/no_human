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

# no_human.ci_gate is the glab/kubectl/metrics-core post-PR gate written for one
# employer's pipeline and must not ship (operator decision D1, 2026-07-30).
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
# Same check for the private half of the term inventory: hex-encoded employer
# terms plus their replacement mapping. Never distributable in any artifact.
private_terms="$(grep -o 'no_human\.eval\._vendor_terms_private' "${pyz_toc}" | sort -u || true)"
if [ -n "${private_terms}" ]; then
  echo "FAIL: no_human.eval._vendor_terms_private is frozen into the bundle (it must never ship)" >&2
  exit 1
fi

echo "OK: ${BUNDLE} ($(du -sh "${BUNDLE}" | cut -f1)), 0 .py files, no ci_gate"
