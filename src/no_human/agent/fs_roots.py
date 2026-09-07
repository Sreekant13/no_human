"""What counts as a whole filesystem root, on either platform.

Split out of `guard.py` rather than added to it: that file is frozen at its
current length by `tests/test_structural_budget.py` (`FROZEN_FILE_LINES`), a
budget that only ratchets down, and it sits exactly at the line. So this is a
module of its own, and `guard.py` gains no lines: its existing
`from . import venv_install_guard` grew a second name in place, and the two
call sites were edited in place too.

The gap this closes (issue #106): `_operand_is_blocked_scan` keyed off a
leading `/`, which no Windows absolute path has, so `grep -r secret C:/` was
allowed while the identical `grep -r secret /` was denied. The rationale in
`_REPO_SCOPE_REASON` is about cost and scope, not about POSIX, and reading a
whole volume costs the same on either platform.

Pure string comparison, no filesystem access, matching the rest of the guard.
"""

from __future__ import annotations

import re

#: A bare Windows drive root, in either separator form, with an optional
#: trailing glob: `C:`, `C:\`, `C:/`, `c:\*`, `C:/*`.
#:
#: The bare `C:` form is not padding. `guard.py` tokenises with POSIX `shlex`,
#: which treats `\` as an escape, so a `C:\` operand arrives here already
#: reduced to `C:` (issue #105). Matching it here means this works whether or
#: not that separate defect is fixed.
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]*\*?$")

#: A UNC root -- `\\server\share` or `//server/share` -- and nothing below it.
#: `\\server\share\proj` is a path INSIDE a share, which is the ordinary case
#: and must stay allowed, exactly as `/Users/dev/repo` does on POSIX.
_UNC_ROOT_RE = re.compile(r"^[\\/]{2}[^\\/]+[\\/]+[^\\/]+[\\/]*\*?$")


def is_windows_filesystem_root(operand: str) -> bool:
    """True when *operand* names a whole Windows volume or network share.

    Deliberately roots only. `C:\\Users` is the Windows analogue of `/Users` in
    `guard._SYSTEM_ROOTS`, but a Windows system-root list is a wider change
    than issue #106 asked for: it would need the same cwd-before-roots ordering
    care that comment in `_operand_is_blocked_scan` explains, and getting that
    wrong denies every ordinary repo-scoped scan on the platform.
    """
    return bool(_DRIVE_ROOT_RE.match(operand) or _UNC_ROOT_RE.match(operand))
