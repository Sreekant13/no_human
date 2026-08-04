# The friend-shareable installer

A no-source macOS build: the nh server is frozen to bytecode with PyInstaller and
shipped inside the Electron app, so someone can run no_human without a Python
toolchain and without reading the source.

## Build

Requires a populated `.venv` at the repo root (`uv sync`), since the freeze runs
`.venv/bin/pyinstaller`, plus `npm install` in `desktop/` and `web/`.

```bash
uv sync
cd desktop && npm run dist:bundled
```

That runs three steps:

1. `packaging/build-installer.sh` — builds `web/dist`, freezes the server via
   `packaging/nh-server.spec`, places runtime data at the bundle root, and
   **fails the build** if the frozen tree contains any `.py` file or is missing
   the board or the migrations.
2. `electron-builder --config electron-builder.config.cjs --mac` — wraps it as
   `no_human.app`, with the frozen server copied in as `extraResources`, and
   also emits the `.zip` + `latest-mac.yml` the auto-updater consumes.
3. `packaging/make-dmg.sh` — produces
   `packaging/dist/no_human-<version>[-UNSIGNED|-UNNOTARIZED].dmg`.

The filename carries the signing verdict: only a signed **and** notarized build
gets the plain `no_human-<version>.dmg` name, so an unshippable artifact cannot
be uploaded by mistake. See `docs/DISTRIBUTION.md` for the signing runbook.

Step 2 has no build-time assertion of its own: electron-builder's `files` is a
literal allowlist, and a file omitted there is simply absent from `app.asar`
with no error — which once shipped an app that could not launch at all.
`desktop/packagedFiles.test.mjs` guards that in the test suite instead, so run
`npm test` in `desktop/` before building.

## What a friend does

The build is **unsigned**, so Gatekeeper refuses a double-click. Right-click the
app and choose **Open**, then confirm once. After that it launches normally.

**Claude Code must be installed on that Mac.** no_human runs coding tasks by
launching the `claude` CLI through the Agent SDK; the CLI is *not* bundled (it is
244 MB, and a friend needs it anyway for the `claude setup-token` step below).
Without it the board loads and looks perfectly healthy while every task fails
with `CLINotFoundError`. The app widens the server's `PATH` to cover the usual
install locations — including `/opt/homebrew/bin`, which is in neither launchd's
`PATH` nor the SDK's own fallback list — but the CLI has to exist somewhere.

On first launch the app asks to connect Claude and offers two credential
types. The default is a **subscription token** (personal or enterprise):
create one with `claude setup-token` and paste it in. The alternative is an
**Anthropic API key**, which bills the operator's own Anthropic account —
select "Anthropic API key" on that screen and paste an `sk-ant-api…` key.
Either credential is written to `~/.no_human/.env` (mode 600) on that machine
only, and a run bills exactly the one configured path: a key pasted into the
subscription field is rejected, and stray billing variables are scrubbed at
startup.

## Verify your install is real

**Decision (F6):** the DMG reader's install-health check is `nh doctor` — path
A (a real command reachable from the installed build), not path B (new prose
instructions in place of a command). It needed **no new code**: `doctor` is
already a `@cli.command` in `src/no_human/cli/commands.py`, and
`packaging/nh_entry.py` statically imports `no_human.cli.commands` as a
PyInstaller analysis anchor, so every subcommand — `doctor` included — is
already in the frozen `nh` binary the app ships. The gap `docs/quickstart.md`
had was pointing at it only through `nh init` in section 3, which it tells
`.dmg` readers to skip; the fix is routing them to the command directly
instead of building a second one.

Run it from a Terminal, using the binary the app already bundles — no source
checkout, no `uv run`:

```bash
/Applications/no_human.app/Contents/Resources/nh-server/nh doctor
```

What you should see: a `coding backend — claude CLI: <path>` line, then a
mechanism-liveness table, ending either `no contradictions, no evidence gaps`
(exit 0) or a red `contradictions` block naming what's wrong (exit 1).

**Real output**, captured by building the frozen bundle in this environment
(`bash packaging/build-installer.sh`, then running
`packaging/dist/nh-server/nh doctor` directly — the same binary that lands at
`no_human.app/Contents/Resources/nh-server/nh`) and running it both with and
without the `claude` CLI on `PATH`:

Healthy (exit 0):

```
coding backend — claude CLI: ~/.local/bin/claude
mechanism liveness (lifetime firings)
  planning                0  last: never  zero = planning disabled or no task got past intake
  ...
no contradictions, no evidence gaps
```

Unhealthy (exit 1) — `claude` CLI not on `PATH`, the single most common friend
failure (see "What a friend does" above):

```
coding backend — claude CLI: not found
mechanism liveness (lifetime firings)
  ...
contradictions — evidence of activity without evidence of the mechanism:
  ✗ CODING BACKEND UNUSABLE: the `claude` CLI is not installed or not on PATH —
the Claude Agent SDK shells out to it for every task, so every task would fail
at launch. Install it with: npm install -g @anthropic-ai/claude-code
```

On a brand-new install every mechanism reads `0  last: never` — expected,
nothing has run yet. Re-run `nh doctor` after your first task to see counters
move, and whenever something behaves oddly.

**Scope of this evidence:** the two transcripts above are real, run against
the actual frozen PyInstaller bundle (`packaging/nh-server.spec` /
`packaging/build-installer.sh`), not inferred from source. What was **not**
run in this environment: `electron-builder --mac` and `make-dmg.sh` — no
`electron-builder` was installed here, and installing new packages is out of
scope for a docs-only change. So the exact path
`no_human.app/Contents/Resources/nh-server/nh` is confirmed by
`electron-builder.config.cjs`'s `extraResources` mapping and by "Verifying a
build" below (which does mount and run a built DMG this way), but was not
re-mounted from a freshly-built signed DMG for this change.

**Gatekeeper on the nested binary, not just the app.** If you extract or
`open` the `nh` binary directly instead of running it from a Terminal path,
Gatekeeper can refuse *it* independently of the app's own right-click-Open
step: a quarantined copy gives `spctl -a -t exec` a `rejected` verdict and
`open` fails with error `-128`. Invoking the identical binary as a shell
command (`.../nh doctor`, as documented above) is unaffected by that check —
verified on a deliberately quarantined copy of the frozen binary built here,
which still ran and exited 0 as a shell command. Always run it as a path in
Terminal; don't double-click or `open` the nested binary.

## Why the packaging looks the way it does

Three constraints drove the design, each found by measuring a real build:

- **`collect_submodules`, not `collect_all`.** `collect_all()` copies a package's
  source tree in as *data* — that put 88 readable `.py` files (48 `websockets`,
  40 `uvicorn`) in the bundle. `collect_submodules()` bundles the same modules as
  bytecode inside the PYZ.
- **`web/dist` and `migrations/` are placed at the bundle root by a script, not
  by PyInstaller `datas`.** The server resolves both with
  `Path(__file__).resolve().parents[3]`, which under a frozen onedir build is the
  bundle root. PyInstaller 6 puts every `datas` entry under `_internal` and
  rejects a `..` destination outright, so `datas` cannot reach the right place.
  Without `migrations/` the server aborts on a fresh install with
  `sqlite3.OperationalError: no such table: tasks`.
- **The DMG is built with `hdiutil`, not electron-builder's dmg target.** That
  target drives Finder over AppleScript and then dies in `hdiutil detach`.
  `hdiutil create -srcfolder` on a directory containing an `.app` fails with
  `Resource busy`. What works: create a read-write image, copy into it,
  force-detach, convert. The force is required because an endpoint-security
  data-protection agent holds a volume containing an app bundle.

## Known limits

- **The packaged build cannot run the repro gate — and fails it *confidently*.**
  `src/no_human/testing/repro_gate.py` and `src/no_human/eval/replay.py` invoke
  `[sys.executable, "-m", "pytest", …]`; in a frozen bundle `sys.executable` is
  the `nh` binary, not a Python interpreter. The subprocess therefore *launches*
  (so the gate's `OSError` handler never fires), the CLI rejects `-m` and exits
  2, and `ran = returncode != 5` makes that look like a genuine failing test
  rather than a broken environment. Every bugfix task in a frozen build gets a
  false "still fails" verdict. The bundle serves the board and the API; running a
  target repo's test suite needs a real Python install. (The fix belongs in
  `repro_gate.py`, which is outside this installer's scope.)
- **`nh test` is a developer command** and is not part of this build — it shells
  out to `scripts/run_tests.sh`, which the bundle does not ship.
- **Built for the host architecture.** No `mac.target.arch` is set, so the build
  matches whatever machine produced it, and it is not a universal binary — an
  arm64 build will not run on an Intel Mac.
- **A wrong-but-well-formed token is only caught at boot.** The token is checked
  for shape, not validity. If the server then fails to start, use **File →
  Re-enter Claude Token…** (or the link on the error screen) to replace it.
- **Unsigned and un-notarised.** Hence the right-click-Open step above.
- **The server is spawned in its own process group** (`detached`), so stopping it
  takes its workers down too. The trade-off is that a *crash* of the desktop app
  (as opposed to a clean quit) leaves `nh` running, and Ctrl-C in a terminal that
  launched the app no longer reaches it — quit from the app, or stop `nh` itself.

## Verifying a build

The build script's checks run against the frozen tree. To check the shipped DMG
itself, mount it and drive it by hand. Note the deliberate non-default port: the
default is 8420, and a dev server already bound there would answer the `curl`
and make the check pass without proving anything.

```bash
hdiutil attach packaging/dist/no_human-0.1.0-UNSIGNED.dmg -nobrowse -readonly
find /Volumes/no_human -name '*.py'            # must print nothing

H=$(mktemp -d); mkdir -p "$H/.no_human"
printf 'server:\n  port: 18994\n' > "$H/.no_human/config.yaml"
printf 'CLAUDE_CODE_OAUTH_TOKEN=dummy\n' > "$H/.no_human/.env"
env -i HOME="$H" PATH=/usr/bin:/bin \
  /Volumes/no_human/no_human.app/Contents/Resources/nh-server/nh start --no-open &
# The frozen server takes a few seconds to bind; poll rather than curling once.
for i in $(seq 1 40); do sleep 1; \
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18994/api/queue/health); \
  [ "$code" = "200" ] && break; done; echo "$code"
hdiutil detach /Volumes/no_human -force
```

The server refuses to boot without a token, hence the dummy one above; the app's
first-run screen supplies a real one for an actual user. That checks the frozen
server — it does not launch the Electron app, which is what `npm test` in
`desktop/` covers.
