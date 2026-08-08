# Build the no-source nh server bundle on WINDOWS: React board -> PyInstaller
# onedir freeze. The Windows counterpart of build-installer.sh.
#
# Output: packaging\dist\nh-server\{nh.exe,_internal\,web\dist\,migrations\}
# The board MUST sit at the bundle root (not under _internal): the server
# resolves it with Path(__file__).resolve().parents[3]/"web"/"dist", and under a
# frozen onedir build __file__ is <bundle>\_internal\no_human\api\app.py. That
# equivalence holds identically on both platforms — verified on Windows, see
# docs/WINDOWS.md.
#
# WHY THIS IS A SEPARATE SCRIPT rather than build-installer.sh run under Git
# Bash. One gate decides it: the build-path leak check. Under Git Bash $HOME is
# `/c/Users/<user>`, a form that appears NOWHERE in a Windows-built binary, so
# `grep -e "$HOME/"` would report clean while a real `C:\Users\<user>` path sat
# in the bundle. A gate that cannot fire is exactly the defect build-installer.sh
# was written to prevent ("it was dark exactly when it mattered"). Every gate
# below is therefore re-expressed in Windows-native terms, not shimmed.
#
# Requires on PATH: npm (Node >=22.12, per desktop/package.json `engines`) and a
# synced .venv (uv sync) containing pyinstaller.

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $PSScriptRoot
$Out    = Join-Path $Root 'packaging\dist'
$Bundle = Join-Path $Out 'nh-server'
$Work   = Join-Path $Out '.work'

function Fail($msg) { Write-Host "FAIL: $msg" -ForegroundColor Red; exit 1 }

# --- byte-level search, used by the leak gate ---------------------------- #
# Latin-1 maps bytes 1:1 to chars, so an ASCII/UTF-8 path survives the round
# trip intact; UTF-16LE is checked separately because PE resources and some
# compiled-in strings are stored that way and would evade a byte-oriented scan.
# NOTE: [System.Text.Encoding]::Latin1 does NOT exist in Windows PowerShell 5.1
# (it is .NET Core only) — GetEncoding(28591) is the portable spelling. Using
# the missing member yields $null and every ReadAllText then throws, which is
# how this gate silently scanned nothing in its first draft.
$Latin1  = [System.Text.Encoding]::GetEncoding(28591)
$Utf16le = [System.Text.Encoding]::Unicode

function Test-FileContainsAny {
  param([string]$Path, [string[]]$Needles)
  $bytes = [System.IO.File]::ReadAllBytes($Path)
  $asAscii = $Latin1.GetString($bytes)
  $asWide  = $Utf16le.GetString($bytes)
  foreach ($n in $Needles) {
    if ($asAscii.Contains($n) -or $asWide.Contains($n)) { return $true }
  }
  return $false
}

function Get-TreeSizeMb($path) {
  $sum = (Get-ChildItem -Path $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
  return [math]::Round($sum / 1MB, 1)
}

# ------------------------------------------------------------------------ #

# The board is always rebuilt below, so it matches ROOT's SOURCE — but nothing
# checked that ROOT's source was current. On 2026-08-05 a signed, notarized DMG
# was found to contain none of the UI shipped that week: it had been built from
# a checkout sitting 44 commits behind main on an old branch. Every step
# "succeeded"; the artefact was simply built from the wrong commit, and the only
# way to discover it was to mount the DMG and grep the bundle.
#
# This cannot be a soft warning. A release build is exactly the moment nobody
# re-reads the log, and the failure is invisible in the output — it looks like a
# clean build, because it IS a clean build of the wrong tree. Mirrors the same
# guard in build-installer.sh; the two must refuse identically.
# Under $ErrorActionPreference='Stop', PowerShell 5.1 turns any stderr output of
# a redirected native command into a TERMINATING NativeCommandError — an offline
# `git fetch` would kill the build here rather than being tolerated the way the
# .sh's `|| true` tolerates it. The guard must never fail a build for a reason
# other than staleness, so it runs under 'Continue' and is restored after.
$eapWas = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& git -C $Root rev-parse --git-dir *> $null
if ($LASTEXITCODE -eq 0) {
  & git -C $Root fetch -q origin main 2>$null
  $ref = & git -C $Root rev-parse --verify -q origin/main 2>$null
  if (-not $ref) { $ref = & git -C $Root rev-parse --verify -q main 2>$null }
  if ($ref) {
    $behind = & git -C $Root rev-list --count "HEAD..$ref" 2>$null
    if ([int]$behind -gt 0) {
      $head = (& git -C $Root rev-parse --abbrev-ref HEAD) + ' ' + (& git -C $Root rev-parse --short HEAD)
      # ASCII only in STRINGS here: PS 5.1 reads a BOM-less .ps1 as the ANSI
      # codepage, and a misdecoded em-dash inside a quoted string breaks the
      # PARSER (comments merely render wrong). Learned from a real parse failure.
      Write-Host "FAIL: $Root is $behind commit(s) behind origin/main - this build would" -ForegroundColor Red
      Write-Host '      ship stale code. Update the checkout, or set NH_ALLOW_STALE_BUILD=1' -ForegroundColor Red
      Write-Host '      to build a deliberate point-in-time artefact.' -ForegroundColor Red
      Write-Host "      HEAD: $head" -ForegroundColor Red
      if ($env:NH_ALLOW_STALE_BUILD -ne '1') { exit 1 }
      Write-Host '      (NH_ALLOW_STALE_BUILD=1 - proceeding anyway)'
    }
  }
}
$ErrorActionPreference = $eapWas

Write-Host '==> building the board (web/dist is gitignored, so always rebuild)'
Push-Location (Join-Path $Root 'web')
try {
  & npm run build
  if ($LASTEXITCODE -ne 0) { Fail "the board build failed (npm run build exited $LASTEXITCODE)" }
} finally { Pop-Location }

Write-Host '==> freezing the server'
$PyInstaller = Join-Path $Root '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path $PyInstaller)) {
  Fail 'no pyinstaller at .venv\Scripts\pyinstaller.exe - run "uv sync" first'
}
Push-Location $Root
try {
  & $PyInstaller --noconfirm --distpath $Out --workpath $Work (Join-Path $Root 'packaging\nh-server.spec')
  if ($LASTEXITCODE -ne 0) { Fail "PyInstaller exited $LASTEXITCODE" }
} finally { Pop-Location }

# Runtime data the server locates via Path(__file__).resolve().parents[3], which
# under a frozen onedir build is the bundle root. Both are load-bearing:
# migrations\*.sql builds the schema on first run (without it the server aborts
# with "no such table: tasks"), and web\dist is the board.
#
# The destinations are REMOVED first rather than copied over: Copy-Item merges
# into an existing directory, so a rebuild after a file was renamed upstream
# would leave the stale copy behind in the shipped bundle.
Write-Host '==> placing runtime data at the bundle root'
foreach ($d in @((Join-Path $Bundle 'web'), (Join-Path $Bundle 'migrations'))) {
  if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}
New-Item -ItemType Directory -Path (Join-Path $Bundle 'web') -Force | Out-Null
Copy-Item -Recurse (Join-Path $Root 'web\dist')   (Join-Path $Bundle 'web\dist')
Copy-Item -Recurse (Join-Path $Root 'migrations') (Join-Path $Bundle 'migrations')

# Strip build-machine provenance. For an EDITABLE install pip records the source
# checkout's ABSOLUTE PATH in direct_url.json — on this platform, verbatim:
#     {"url":"file:///C:/Users/<user>/<checkout>","dir_info":{"editable":true}}
# ONLY direct_url.json is removed, NOT the .dist-info directory: a frozen app
# DOES resolve distributions, and deleting the directory kills `nh --version` in
# importlib.metadata's from_name. Same reasoning as the macOS script; the defect
# reproduces identically on Windows and is confirmed by the gate below.
Write-Host '==> stripping build-machine provenance'
Get-ChildItem -Path (Join-Path $Bundle '_internal') -Recurse -Filter 'direct_url.json' -ErrorAction SilentlyContinue |
  Where-Object { $_.Directory.Name -like '*.dist-info' } |
  ForEach-Object { Write-Host "    removed $($_.FullName)"; Remove-Item -Force $_.FullName }

Write-Host '==> verifying'

# 1. No source ships.
$pyFiles = @(Get-ChildItem -Path $Bundle -Recurse -Filter '*.py' -ErrorAction SilentlyContinue)
if ($pyFiles.Count -ne 0) {
  Write-Host "FAIL: $($pyFiles.Count) .py files in the bundle (it must ship no source)" -ForegroundColor Red
  $pyFiles | ForEach-Object { Write-Host "    $($_.FullName)" }
  exit 1
}

# 2. The board landed where the server looks for it.
if (-not (Test-Path (Join-Path $Bundle 'web\dist\index.html'))) {
  Fail "board missing at $Bundle\web\dist\index.html"
}

# 3. The schema ships.
$sql = @(Get-ChildItem -Path (Join-Path $Bundle 'migrations') -Filter '*.sql' -ErrorAction SilentlyContinue)
if ($sql.Count -lt 1) { Fail "no migrations in $Bundle\migrations" }

# 4/5. Modules that must never ship. Searching the bundle for the NAME cannot do
# this job: pure modules live in the zlib-compressed PYZ, so the string is absent
# from the bytes while the module is inside. PyInstaller's own module table is
# the readable inventory.
$toc = Get-ChildItem -Path (Join-Path $Work 'nh-server') -Filter 'PYZ-*.toc' -ErrorAction SilentlyContinue |
       Select-Object -First 1
if (-not $toc) { Fail "no PYZ table under $Work\nh-server - cannot read what was frozen" }

$gate = @(Select-String -Path $toc.FullName -Pattern 'no_human\.ci_gate[A-Za-z0-9_.]*' -AllMatches)
if ($gate.Count -gt 0) {
  Write-Host 'FAIL: no_human.ci_gate is frozen into the bundle (D1: it must not ship)' -ForegroundColor Red
  $gate | ForEach-Object { Write-Host "    $($_.Line.Trim())" }
  exit 1
}
$priv = @(Select-String -Path $toc.FullName -Pattern 'no_human\.eval\._vendor_terms_private' -AllMatches)
if ($priv.Count -gt 0) {
  Fail 'no_human.eval._vendor_terms_private is frozen into the bundle (it must never ship)'
}

# 6. No absolute build path may survive anywhere in the bundle.
#
# Windows-native forms, and BOTH slash spellings: pip writes the file:// URL with
# forward slashes (`file:///C:/Users/...`) while a compiled-in or RECORD-style
# path uses backslashes. Checking one form only would have passed this build —
# the known-positive control for this gate is direct_url.json, which carries the
# forward-slash form and is removed above.
#
# The repo root is checked IN ADDITION to the user profile (the macOS script
# checks only $HOME). A checkout outside the user profile - C:\dev\no_human, a
# build agent's workspace - leaks a path that a $HOME-only check cannot see.
#
# TWO needle sets, scanned over DIFFERENT file sets:
#
#  * The HOME (USERPROFILE) needle is NOT matched against third-party vendored
#    compiled extensions. Prebuilt Rust/C wheels (pydantic_core, rpds,
#    watchfiles, ...) bake their OWN build machine's cargo path into the .pyd
#    for panic backtraces - on a GitHub-hosted Windows runner that is
#    `C:\Users\runneradmin\.cargo\...`, and $env:USERPROFILE on THIS runner is
#    also `C:\Users\runneradmin`, so the needle collides and reports a leak that
#    is not one (Windows CI red 2026-08-08). We compile none of these; our own
#    Python is pure and lives in the PYZ, so any compiled extension under
#    _internal that is NOT under our own package (_internal\no_human\) is
#    third-party by construction. DISCOVERED from the tree, never enumerated.
#
#  * The $Root (current build path) needle STILL scans EVERYTHING, including
#    nh.exe and the PYZ: PyInstaller can bake the build cwd into the frozen
#    launcher, and that is a real leak that must stay armed.
#
# Both real HOME-leak vectors stay covered: direct_url.json is TEXT in a
# .dist-info (not a compiled extension), and a maintainer path baked into our
# own frozen launcher/PYZ is not a third-party compiled extension either.
$homeNeedles = @()
if ($env:USERPROFILE) {
  $homeNeedles += $env:USERPROFILE
  $homeNeedles += $env:USERPROFILE.Replace('\', '/')
}
$homeNeedles = $homeNeedles | Sort-Object -Unique
$rootNeedles = @()
if ($Root) {
  $rootNeedles += $Root
  $rootNeedles += $Root.Replace('\', '/')
}
$rootNeedles = $rootNeedles | Sort-Object -Unique

$thirdPartyExt = @('.pyd', '.so', '.dll', '.dylib')
# Trailing separator so a sibling like `_internal\no_human_helper\` is NOT
# treated as ours — exact parity with the `.sh` glob `_internal/no_human/`*.
$ourPkg = (Join-Path $Bundle '_internal\no_human') + [IO.Path]::DirectorySeparatorChar

$leaked = New-Object System.Collections.ArrayList
foreach ($f in (Get-ChildItem -Path $Bundle -Recurse -File)) {
  # $Root needle: every file, no exception.
  if (($rootNeedles.Count -gt 0) -and (Test-FileContainsAny -Path $f.FullName -Needles $rootNeedles)) {
    [void]$leaked.Add($f.FullName); continue
  }
  # HOME needle: skip third-party vendored compiled extensions.
  $isThirdPartyBinary = ($thirdPartyExt -contains $f.Extension.ToLower()) -and
                        (-not $f.FullName.StartsWith($ourPkg, [System.StringComparison]::OrdinalIgnoreCase))
  if ((-not $isThirdPartyBinary) -and ($homeNeedles.Count -gt 0) -and
      (Test-FileContainsAny -Path $f.FullName -Needles $homeNeedles)) {
    [void]$leaked.Add($f.FullName)
  }
}
if ($leaked.Count -gt 0) {
  Write-Host "FAIL: the bundle records this machine's build path (P1: no maintainer trace ships)" -ForegroundColor Red
  $leaked | ForEach-Object { Write-Host "    $_" }
  exit 1
}

Write-Host "OK: $Bundle ($(Get-TreeSizeMb $Bundle) MB), 0 .py files, no ci_gate, no build-path leak" -ForegroundColor Green
