"""Windows console suppression for nh's own subprocess spawns.

`nh` and the CLIs it launches (`git`, `codex`) are console-subsystem binaries.
When the desktop app starts `nh` without a console of its own (see
`desktop/server.mjs`), every such child is otherwise allocated a fresh VISIBLE
console — the "multiple empty terminals" real users reported on Windows.
`CREATE_NO_WINDOW` suppresses that console; `CREATE_NEW_PROCESS_GROUP` (only
where a group is wanted) detaches the child from our console so a Ctrl-C to nh
does not also hit it. Both flags are Windows-only; POSIX uses
`start_new_session` for the group and needs nothing to hide a console.
"""

import sys

# subprocess creationflags (Windows). Ints so this module imports on any OS.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def hidden_console_kwargs(
    *, new_group: bool = False, platform: str | None = None
) -> dict[str, object]:
    """Popen/subprocess.run kwargs to suppress a Windows console for a child.

    Windows: ``creationflags`` with ``CREATE_NO_WINDOW`` (plus
    ``CREATE_NEW_PROCESS_GROUP`` when ``new_group``). POSIX:
    ``{"start_new_session": True}`` when ``new_group``, else ``{}`` — no console
    to hide there. ``platform`` defaults to :data:`sys.platform`.
    """
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        flags = CREATE_NO_WINDOW
        if new_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    if new_group:
        return {"start_new_session": True}
    return {}
