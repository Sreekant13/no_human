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
# The force is required because an endpoint-security agent can hold a volume
# containing an app bundle — a plain `detach` returns
# "couldn't unmount diskN - Operation not permitted".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${ROOT}/desktop/dist/mac-arm64/no_human.app"
OUT="${ROOT}/packaging/dist"
RW="${OUT}/.no_human-rw.dmg"
VOL="no_human"

# ONE source of truth for "is this build shippable" — desktop/signing.cjs, the
# same module electron-builder.config.cjs uses to decide whether to sign. A
# second copy of the rule here is how you get a signed .app inside a DMG named
# as if it were unsigned, or worse, the reverse.
read_plan() { (cd "${ROOT}/desktop" && node -e '
  const { signingPlan } = require("./signing.cjs");
  const v = signingPlan(process.env)[process.argv[1]];
  process.stdout.write(v === undefined || v === null ? "" : String(v));
' "$1"); }

SIGN_MODE="$(read_plan mode)"
ARTIFACT_TAG="$(read_plan artifactTag)"
VERSION="$(node -p "require('${ROOT}/desktop/package.json').version")"

# The filename carries the verdict. An operator cannot upload
# "no_human-0.1.0-UNSIGNED.dmg" to a release page believing it is shippable.
DMG="${OUT}/no_human-${VERSION}${ARTIFACT_TAG}.dmg"

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

# ---------------------------- sign / notarize ---------------------------- #
# electron-builder signs the .app; the DMG it is wrapped in is built here, so
# the container has to be signed, notarized and stapled here too. Stapling is
# what lets the DMG validate OFFLINE — without it, a user on a bad network sees
# Gatekeeper fail even though notarization succeeded.
if [ "${SIGN_MODE}" = "signed" ]; then
  echo "==> signing the DMG"
  # CSC_NAME is the identity when set; otherwise let codesign resolve the sole
  # Developer ID Application identity in the keychain.
  ident="${CSC_NAME:-Developer ID Application}"
  codesign --force --sign "${ident}" --timestamp "${DMG}"

  echo "==> notarizing (this waits on Apple and can take several minutes)"
  if [ -n "${APPLE_API_KEY:-}" ]; then
    xcrun notarytool submit "${DMG}" --wait \
      --key "${APPLE_API_KEY}" --key-id "${APPLE_API_KEY_ID}" \
      --issuer "${APPLE_API_ISSUER}"
  elif [ -n "${APPLE_KEYCHAIN_PROFILE:-}" ]; then
    xcrun notarytool submit "${DMG}" --wait \
      --keychain-profile "${APPLE_KEYCHAIN_PROFILE}"
  else
    xcrun notarytool submit "${DMG}" --wait \
      --apple-id "${APPLE_ID}" --password "${APPLE_APP_SPECIFIC_PASSWORD}" \
      --team-id "${APPLE_TEAM_ID}"
  fi

  echo "==> stapling"
  xcrun stapler staple "${DMG}"

  echo "==> verifying (all three must pass, or this is not shippable)"
  codesign -dv --verbose=2 "${DMG}" 2>&1 | sed 's/^/    /'
  spctl -a -t open --context context:primary-signature -v "${DMG}" 2>&1 | sed 's/^/    /'
  xcrun stapler validate "${DMG}" 2>&1 | sed 's/^/    /'
else
  # Loud, and impossible to mistake for a release. This is the expected state
  # until the Apple Developer membership is active.
  # `tr` rather than bash 4's ${x^^}: macOS ships bash 3.2 and this script
  # runs under /usr/bin/env bash, which is usually that one.
  mode_upper="$(printf '%s' "${SIGN_MODE}" | tr '[:lower:]' '[:upper:]')"
  cat >&2 <<EOF

  ────────────────────────────────────────────────────────────────────────
  ${DMG##*/} is ${mode_upper}.
  macOS Gatekeeper will REJECT it on any machine that downloads it, and the
  app cannot auto-update (Squirrel.Mac requires a signature).
  This is the expected state until a Developer ID certificate exists —
  see docs/DISTRIBUTION.md. Do not publish this artifact.
  ────────────────────────────────────────────────────────────────────────

EOF
fi

echo "OK: ${DMG} ($(du -sh "${DMG}" | cut -f1)) [${SIGN_MODE}]"
