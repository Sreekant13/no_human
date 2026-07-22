#!/usr/bin/env bash
# Package the built .app into a shareable DMG.
#
# Two things here are deliberate, both forced by what actually happens on a
# managed Mac:
#
#  * NOT electron-builder's dmg target. It drives Finder over AppleScript to lay
#    out the window; Finder keeps the volume busy and the build dies in
#    `hdiutil detach`.
#  * NOT `hdiutil create -srcfolder <dir-containing-.app>` either: that fails
#    with "Resource busy" on an app bundle (an empty image and a plain folder
#    both succeed, so it is the bundle specifically).
#
# So: create a read-write image, copy into it, FORCE-detach, convert compressed.
# The force is required because CrowdStrike's data-protection agent holds a
# volume containing an app bundle — a plain `detach` returns
# "couldn't unmount diskN - Operation not permitted".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${ROOT}/desktop/dist/mac-arm64/no_human.app"
OUT="${ROOT}/packaging/dist"
RW="${OUT}/.no_human-rw.dmg"
DMG="${OUT}/no_human.dmg"
VOL="no_human"

test -d "${APP}" || { echo "FAIL: no .app at ${APP} — build it first" >&2; exit 1; }
mkdir -p "${OUT}"
rm -f "${RW}" "${DMG}"

# Size the image from the app plus headroom, so this keeps working as it grows.
size_mb=$(( $(du -sm "${APP}" | cut -f1) + 120 ))
hdiutil create -size "${size_mb}m" -fs HFS+ -volname "${VOL}" -ov "${RW}" >/dev/null

# Refuse to start if the name is taken: macOS would silently mount this image as
# "/Volumes/no_human 1" and the copy below would target the WRONG volume.
if [ -e "/Volumes/${VOL}" ]; then
  echo "FAIL: /Volumes/${VOL} is already mounted — detach it first" >&2
  exit 1
fi

# Take the mountpoint from the attach output (column 3) rather than assuming it.
attach_out="$(hdiutil attach "${RW}" -nobrowse -noverify)"
dev="$(printf '%s\n' "${attach_out}" | grep '/Volumes/' | head -1 | awk '{print $1}')"
mnt="$(printf '%s\n' "${attach_out}" | grep '/Volumes/' | head -1 | cut -f3-)"
cleanup() { hdiutil detach "${dev}" -force >/dev/null 2>&1 || true; }
trap cleanup EXIT
test -d "${mnt}" || { echo "FAIL: could not determine mountpoint" >&2; exit 1; }

cp -R "${APP}" "${mnt}/"
ln -s /Applications "${mnt}/Applications"   # drag-to-install affordance

hdiutil detach "${dev}" -force >/dev/null
trap - EXIT
hdiutil convert "${RW}" -format UDZO -o "${DMG}" >/dev/null
rm -f "${RW}"

echo "OK: ${DMG} ($(du -sh "${DMG}" | cut -f1))"
