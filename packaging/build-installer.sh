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

echo "OK: ${BUNDLE} ($(du -sh "${BUNDLE}" | cut -f1)), 0 .py files"
