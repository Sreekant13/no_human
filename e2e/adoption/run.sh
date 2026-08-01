#!/usr/bin/env bash
# The daily adoption test. One command, no arguments required.
#
#   e2e/adoption/run.sh                  # smoke: docs + install + adapters, no spend
#   e2e/adoption/run.sh --mode full      # + real task execution (costs real money)
#
# Cron:
#   0 6 * * *  cd /path/to/no_human && e2e/adoption/run.sh >> /var/log/nh-adoption.log 2>&1
#
# Exit code is the signal: 0 = every blocking assertion held, 1 = at least one
# regressed. The friction log lands in e2e/adoption/out/.
#
# This script deliberately does NOT source your shell profile, does not read
# ~/.no_human, and does not accept a credential on the command line. Full mode
# takes one from NH_ADOPTION_OAUTH_TOKEN only, exported deliberately for the
# invocation.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# Run the harness itself on a plain interpreter with no dependency on the
# product's venv: the harness has to work BEFORE the product is installable,
# which is precisely the condition it exists to detect.
python3 "$here/adoption_run.py" --out "$here/out" "$@"
