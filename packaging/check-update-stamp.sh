#!/usr/bin/env bash
# packaging/check-update-stamp.sh
#
# WHY: the 0.1.8 DMG was signed, notarized (externally, after the fact) and
# stapled — codesign/spctl/stapler all passed — yet app.asar carried
# nhSigning="signed-not-notarized" / nhCanAutoUpdate=false baked in at build
# time, so every installer of that release has in-app updates permanently
# disabled and only gets the Open-downloads browser fallback (desktop/main.mjs
# reads exactly those two keys at runtime). The signature answers WHO BUILT
# IT; this answers WHAT THE APP WILL BELIEVE ABOUT ITSELF AT RUNTIME — a
# question codesign/spctl/stapler never ask.
#
# Sourceable (defines nh_check_update_stamp, for in-process testing) AND
# runnable directly (`bash check-update-stamp.sh <app_dir>`), which is how
# make-dmg.sh's acceptance section calls it — as its own subprocess, so a
# `set -euo pipefail` caller is unaffected by anything in here.
#
# Exit codes: 0 pass (verdict PASS, or NH_ALLOW_UNNOTARIZED=1 override, with a
#             loud warning); 1 bad stamp; 2 the app bundle or its app.asar is
#             missing/unreadable — never mistaken for a pass.
nh_check_update_stamp() {
  local app_dir="$1"
  local asar="${app_dir}/Contents/Resources/app.asar"

  if [ ! -d "${app_dir}" ]; then
    echo "FAIL: no app bundle at ${app_dir} — cannot check its update stamp" >&2
    return 2
  fi
  if [ ! -f "${asar}" ] || [ ! -r "${asar}" ]; then
    echo "FAIL: no readable app.asar at ${asar} — cannot check its update stamp" >&2
    return 2
  fi

  # app.asar is a binary container; the package.json it embeds is stored as
  # raw JSON bytes inside it. -a forces grep to scan it as text despite the
  # binary framing around those bytes, -o prints only the matched span, -E is
  # POSIX extended regex, LC_ALL=C keeps the byte-scan locale-independent.
  # Sorted+uniqued: a nested node_modules package.json or a second copy of the
  # stamp must not silently pick one — see the ambiguity check below. No
  # asar/npx/python dependency is introduced.
  local signing_hits update_hits signing_n update_n
  signing_hits="$(LC_ALL=C grep -a -o -E '"nhSigning"[[:space:]]*:[[:space:]]*"[a-z-]+"' "${asar}" 2>/dev/null | sort -u)"
  update_hits="$(LC_ALL=C grep -a -o -E '"nhCanAutoUpdate"[[:space:]]*:[[:space:]]*(true|false)' "${asar}" 2>/dev/null | sort -u)"

  signing_n=0
  [ -n "${signing_hits}" ] && signing_n="$(printf '%s\n' "${signing_hits}" | wc -l | tr -d ' ')"
  update_n=0
  [ -n "${update_hits}" ] && update_n="$(printf '%s\n' "${update_hits}" | wc -l | tr -d ' ')"

  local observed="nhSigning=${signing_hits:-<not found>} nhCanAutoUpdate=${update_hits:-<not found>}"
  local reason="" ok=0

  # Zero matches is a FAIL, not a pass: an absent key is exactly how
  # desktop/main.mjs's `pkg.nhCanAutoUpdate === true` reads as "no updates".
  if [ "${signing_n}" -eq 0 ] || [ "${update_n}" -eq 0 ]; then
    reason="the stamp key was not found in app.asar (nhSigning matches=${signing_n}, nhCanAutoUpdate matches=${update_n})"
  elif [ "${signing_n}" -gt 1 ] || [ "${update_n}" -gt 1 ]; then
    # More than one distinct value is also a FAIL: a second copy of the stamp
    # would make the verdict unreadable, so this refuses rather than picks one.
    reason="app.asar carries more than one distinct value for the stamp — ambiguous, refusing to guess"
  else
    local signing_pass=0 update_pass=0
    case "$(printf '%s' "${signing_hits}" | tr -d '[:space:]')" in
      '"nhSigning":"signed"') signing_pass=1 ;;
    esac
    case "$(printf '%s' "${update_hits}" | tr -d '[:space:]')" in
      '"nhCanAutoUpdate":true') update_pass=1 ;;
    esac
    if [ "${signing_pass}" -eq 1 ] && [ "${update_pass}" -eq 1 ]; then
      ok=1
    else
      reason="${observed} — not signed/true"
    fi
  fi

  if [ "${ok}" -eq 1 ]; then
    return 0
  fi

  if [ "${NH_ALLOW_UNNOTARIZED:-}" = "1" ]; then
    cat >&2 <<EOF

  ────────────────────────────────────────────────────────────────────────
  NH_ALLOW_UNNOTARIZED=1: overriding the update-stamp check for ${app_dir}.
  Observed: ${observed}
  (${reason})
  This artefact MUST NOT SHIP — do not upload it to a release page. This
  override exists only for deliberate unsigned/dev builds.
  ────────────────────────────────────────────────────────────────────────

EOF
    return 0
  fi

  echo "" >&2
  echo "FAIL: ${app_dir} carries an update stamp that disables in-app updates." >&2
  echo "      Observed: ${observed}" >&2
  echo "      Reason: ${reason}" >&2
  echo "      Every installer of this release would get in-app updates" >&2
  echo "      permanently disabled and only the Open-downloads browser" >&2
  echo "      fallback (see desktop/main.mjs)." >&2
  echo "      This is what a build with no notarization credentials in its" >&2
  echo "      environment produces. Set one of: APPLE_API_KEY+APPLE_API_KEY_ID+APPLE_API_ISSUER" >&2
  echo "      | APPLE_KEYCHAIN_PROFILE | APPLE_ID+APPLE_APP_SPECIFIC_PASSWORD+APPLE_TEAM_ID," >&2
  echo "      alongside CSC_LINK/CSC_NAME, and rebuild." >&2
  echo "      To ship a deliberate unsigned/dev build anyway, set" >&2
  echo "      NH_ALLOW_UNNOTARIZED=1 (loudly not-shippable)." >&2
  return 1
}

# Runnable directly: `bash check-update-stamp.sh <app_dir>`. Guarded so that
# sourcing this file (to call nh_check_update_stamp in-process, e.g. from a
# test) does not also execute it.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  nh_check_update_stamp "$1"
  exit $?
fi
