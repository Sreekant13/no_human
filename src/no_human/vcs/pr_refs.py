"""Parse PR/MR references out of free text — full URLs OR human shorthand.

A code_review task is often described the way a human writes it in a ticket:
"code.example.com/dev/acme-test PR #7001" or
"gitlab.acme.net ci_gate/customer/metrics-core MR !7006" — not a full
``https://…/pull/7001`` URL, and usually more than one. The old single-URL
regex found zero of these and failed the task at the gate.

``parse_pr_refs`` recognises both forms and returns full, canonical URLs that
``pr_watcher.parse_pr_url`` can then parse — de-duplicated, order-preserving.
"""

from __future__ import annotations

import re

# Full URLs already in canonical form (GitHub/GHE …/pull/N, GitLab …/merge_requests/N).
_FULL = re.compile(r"https?://[^\s()<>]+?/(?:pull|merge_requests)/\d+")

# Human shorthand: "<host> <owner/group/.../repo> PR #<n>" or "… MR !<n>".
# host has at least one dot + a TLD; separator is a slash or whitespace; the repo
# path is the slug; then "PR #" (GitHub) or "MR !" (GitLab) and the number.
_SHORT = re.compile(
    r"(?P<host>[a-z0-9][a-z0-9.-]*\.[a-z]{2,})"   # host with a TLD
    r"[\s/]+"                                       # sep: slash or whitespace
    r"(?P<path>[a-z0-9][a-z0-9._/-]*?)"            # owner/group/…/repo slug
    r"\s+"
    r"(?:(?P<pr>PR)\s*#|(?P<mr>MR)\s*!)"          # PR #  or  MR !
    r"(?P<num>\d+)",
    re.IGNORECASE,
)


def parse_pr_refs(text: str) -> list[str]:
    """Return every PR/MR reference in *text* as a full canonical URL.

    Handles full URLs and shorthand like ``host/path PR #123`` / ``host path
    MR !45``. De-duplicated, in first-seen order. Empty list if none found.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            out.append(url)

    for m in _FULL.finditer(text):
        _add(m.group(0))
    for m in _SHORT.finditer(text):
        host = m.group("host")
        path = m.group("path").rstrip("/")
        num = m.group("num")
        if m.group("mr"):
            _add(f"https://{host}/{path}/-/merge_requests/{num}")
        else:
            _add(f"https://{host}/{path}/pull/{num}")
    return out
