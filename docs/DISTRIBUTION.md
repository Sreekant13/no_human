# Distribution — where the artifacts live, and how they update

Two artifacts, two homes:

| Artifact | What it is | Where it goes | Why |
|---|---|---|---|
| `no_human-<version>.dmg` (~145 MB) | the macOS desktop app, with the frozen `nh` server inside it | **GitHub Releases** | free, unmetered bandwidth, `electron-updater` speaks it natively |
| `no_human-<version>-py3-none-any.whl` / `.tar.gz` (~1 MB) | the `nh` command line | **PyPI** (`pip install no-human`) | free, the only place `pip` looks |

**Stale as of 2026-08-22:** versions 0.1.0 through 0.1.4 ARE published, to PyPI
(`no-human`) and to GitHub Releases, by `.github/workflows/publish-pypi.yml`
(Trusted Publishing). Read the rest of this file as a record of the decision,
not as current state. The original text follows.

Nothing here has been published. Every build script passes `--publish never`,
and `desktop/packagedFiles.test.mjs` fails the suite if one stops doing so.
The exact publish commands are in [§5](#5-the-commands-that-actually-publish).

---

## 1. Why GitHub Releases

The decision is **not** about cost — at the volumes this product will see, all
three candidates are free or nearly so. It is about operational surface.

Cost of serving a 145 MB artifact (0.1356 GiB), per month:

| Downloads/month | GitHub Releases | Cloudflare R2 | S3 + CloudFront | S3 direct |
|---|---|---|---|---|
| 100 | $0 | $0 | $0 | $0 |
| 1,000 | $0 | $0 | $0 | $3.20 |
| 10,000 | $0 | $0 | ~$28.34 | $113.04 |
| 100,000 | $0 | $0 | ~$1,049 | ~$1,211 |

Arithmetic for the two that are not zero:
- **S3 + CloudFront @ 10,000**: 1,356.04 GiB egress − 1,024 GiB always-free =
  332.04 GiB × $0.085 = $28.22, plus ~5 GB S3 storage × $0.023 = $0.12.
  Requests are inside the 10M/month free allowance.
- **S3 direct @ 10,000**: 1,256.04 GB × $0.09 = $113.04 (only 100 GB is free,
  and that 100 GB is shared across all AWS services).
- **R2** charges **$0 egress by policy**. Even outside every free tier the bill
  would be 5 GB × $0.015 + 10k GETs × $0.36/M = **under $0.08/month**.

GitHub Releases wins on everything that is not cost: no account to provision,
no DNS, no bucket policy, no credentials in CI beyond the `GITHUB_TOKEN` that
already exists, and `electron-updater` has a first-class GitHub provider.
Release assets are capped at **2 GiB** each (we are at 0.145 GiB) and GitHub
states there is *"no limit on the total size of a release, nor bandwidth
usage."* The only backstop is the Acceptable Use Policy's discretionary
"excessive bandwidth" clause, which is not a real constraint below six figures
of downloads.

### What we rejected, and the condition that would change it

- **Cloudflare R2** — genuinely free at any volume we will reach, and the site
  is already on Cloudflare. Rejected only because it adds a bucket, a custom
  domain, and a credential for zero benefit today. **Revisit if downloads pass
  ~100k/month**, where GitHub's AUP clause starts to matter and R2 stays $0.
  Migration is a config change, not a rearchitecture: `electron-updater`
  supports a `generic` provider (a plain static host) and a native `r2`
  provider. The client gains no dependency either way.
- **S3 + CloudFront** — the most expensive option and the most moving parts.
  Rejected. ⚠️ One number here is unverified: AWS now leads with flat-rate
  CloudFront plans (Free = 100 GB, Pro = $15/mo for 50 TB) and no fetchable AWS
  page states whether choosing a plan *replaces* the always-free 1 TB tier. If
  it does, the free tier is 10× smaller than assumed above. Verify before ever
  choosing AWS on cost grounds.
- The branch `installer/freeze-s3` is **not** about Amazon S3 — "S3" there is
  the internal program lane (installer/frontend/a11y). It is already merged and
  superseded by `main`; there is nothing to revive.

### The blocker: the repo is private

GitHub Releases on a **private** repo require an authenticated token to
download an asset. There is no unauthenticated public URL. `electron-updater`
supports private repos only by shipping a `GH_TOKEN` **to every end user**,
which its own documentation calls "not intended and not suitable for all
users". That is not a distribution channel.

**So public downloads are gated on the repo going public** — a separate,
in-flight workstream.

**Interim answer, if a working download is needed before go-public:** upload
the DMG to a **Cloudflare R2 bucket with a public custom domain** and point
`electron-updater` at it with `provider: "generic"`. It is ~$0/month, requires
no repo visibility change, and is a two-line change in
`desktop/electron-builder.config.cjs`. Do not use the `r2.dev` URL — Cloudflare
rate-limits it and documents it as development-only. The alternative interim —
emailing a DMG — is fine for a handful of friends and needs no infrastructure
at all, but gives up the update feed.

### PyPI

Free, no bandwidth terms, and the name `no-human` is **available** (verified:
`no-human`, `no_human` and `nohuman` all return HTTP 404 on PyPI). Limits are
**100 MiB per file** and **10 GiB per project**; the wheel is ~1 MB, so neither
binds. Note the DMG **cannot** ship via PyPI — it exceeds the per-file cap, and
it does not belong in a wheel regardless.

⚠️ **The wheel does not contain the web board.** `[tool.hatch.build.targets.wheel]`
packages `src/no_human` only, and `web/dist` is gitignored and built separately.
`pip install no-human` therefore gives a working CLI whose `nh start` has no UI
to serve. That is a real gap, out of scope for this change, and it must be
resolved before the CLI is published — either by adding `web/dist` to the wheel
via a build hook, or by documenting the CLI as headless.

---

## 2. How updating works

Two independent halves, because they have different constraints.

### The CLI half — works today, needs no signing

`src/no_human/updates.py`. On every `nh <subcommand>` invocation:

1. Read `~/.no_human/cache/update-check.json`.
2. If a newer version is cached, print one line **after** the command's own
   output — on **stderr only**, and only when stdout is an interactive TTY.
   It is suppressed entirely for machine output (`--json` / `--json-out`
   commands mark themselves) and whenever stdout is piped, so scripts and
   parsers never see it (it corrupted `--json` output and failed eight tests
   the day 0.1.3 reached PyPI). `nh --version` is a click eager option and
   exits first, so the fastest path is untouched.
3. If the cache is older than 24 h, start a **daemon thread** to refresh it and
   return immediately. The notice always comes from the *previous* run's cache.

It never blocks (the network call is on a thread nobody joins), never fails a
command (every path is wrapped and degrades to silence), and never nags (one
line, at most one network call per day).

Turn it off with either:

```yaml
# ~/.no_human/config.yaml
updates:
  enabled: false
```

```sh
export NH_NO_UPDATE_CHECK=1     # also the right switch for CI
```

Honest limitation: a command that exits faster than an HTTPS round trip
(`nh --version`) dies before the daemon thread finishes, so that invocation's
cache write is lost and the next one retries. That is the deliberate trade for
never adding latency.

### The desktop half — built, and blocked on signing

`desktop/updater.mjs` drives `electron-updater` with **`autoDownload: false`**
and **`autoInstallOnAppQuit: false`**. Both default to the wrong value:
`autoDownload` would move 145 MB unasked, and `autoInstallOnAppQuit` would
install on quit the very update the user deferred.

The flow:

1. On launch (after the window exists, never awaited) the shell checks — at
   most once a day.
2. If a newer version exists, the board's **Settings → Updates** panel shows it
   with **Download now** and **Later**. Nothing is downloaded until the click.
3. **"Later" persists**, keyed on the *version*, in
   `<userData>/update-state.json`. That version never prompts again. A *newer*
   version still gets through, and **File → Check for Updates…** always answers
   regardless of any deferral — otherwise "Later" would mean "never".
4. Once downloaded, the panel offers **Restart and install**.

**macOS requires the app to be signed for any of step 4 to work.** Electron's
own docs: *"Your application must be signed for automatic updates on macOS.
This is a requirement of Squirrel.Mac."* Unsigned, `SQRLUpdater` throws
`Could not get code signature for running application`, which surfaces late and
opaquely.

So the unsigned build **fails loudly and early instead**: `signing.cjs` records
at build time that this artifact cannot auto-update, the verdict is stamped
into the packaged app (`extraMetadata.nhCanAutoUpdate`), and the updater still
*reports* that a new version exists while refusing the install with a sentence
that names the cause and points at the manual download. It never silently does
nothing.

Also load-bearing: `mac.target` **must include `zip`**. Squirrel.Mac updates
from a ZIP, and electron-builder only emits `latest-mac.yml` — the file
`electron-updater` fetches — when a zip target is present. With `["dmg"]` alone
the updater fails at runtime with `ERR_UPDATER_ZIP_FILE_NOT_FOUND`. The DMG is
what a human downloads; the ZIP is what the updater consumes. Both ship in the
same release. `desktop/packagedFiles.test.mjs` pins this.

---

## 3. Code signing — the runbook

Assume you have never done this. Do these in order.

### 3.1 Enrol

1. Go to <https://developer.apple.com/programs/enroll/>.
2. Enrol as an **individual** (a company enrolment needs a D-U-N-S number and
   is not required here).
3. Cost: **99 USD per year**, renewed annually.
4. You must be the **Account Holder** of the membership to create a Developer
   ID certificate. Enrolling as an individual makes you that automatically.

> ⚠️ Verify before relying on it: Apple lists "Notarization & Developer ID for
> Mac apps" as a $99-tier benefit, but no Apple page explicitly confirms that an
> *individual* (non-organisation) membership can issue Developer ID
> certificates. If this matters, ask Apple Developer Support before paying.
> The $299 Enterprise Program is **not** what you want — it is internal-
> distribution only and never grants Developer ID.

### 3.2 Create the right certificate

Create a **“Developer ID Application”** certificate.

**This is the one that matters.** The alternatives are wrong in ways that cost
a full cycle to discover:

| Certificate | Use | For us |
|---|---|---|
| **Developer ID Application** | apps distributed **outside** the Mac App Store | ✅ **this one** |
| Developer ID Installer | signing `.pkg` installers | ❌ we ship a DMG |
| Mac App Store / Apple Distribution | submitting to the Mac App Store | ❌ wrong channel entirely |
| Apple Development | local debugging | ❌ cannot be distributed |

Steps: Xcode → Settings → Accounts → your Apple ID → **Manage Certificates** →
**+** → **Developer ID Application**. (Or the web flow at
<https://developer.apple.com/account/resources/certificates>, which requires
generating a CSR with Keychain Access first.)

### 3.3 Export it for CI

In **Keychain Access**, find `Developer ID Application: <YOUR NAME> (<TEAM ID>)`,
right-click → **Export** → `.p12` format → set a strong password.

For a local build the keychain is enough. For CI, base64 it:

```sh
base64 -i DeveloperID.p12 | pbcopy     # paste into the CI secret
```

### 3.4 Create an App Store Connect API key for notarization

Notarization needs separate credentials. Apple's `altool` was **decommissioned
on 2023-11-01**; only `notarytool` works. Apple and electron-builder both
recommend the **API key** method over an Apple ID password.

<https://appstoreconnect.apple.com/access/integrations/api> → **+** → give it
the **Developer** role → download the `.p8` file (**you can only download it
once**). Note the **Key ID** and **Issuer ID** shown on that page.

### 3.5 Set the environment variables

`desktop/signing.cjs` accepts any **one** of Apple's three credential sets. Use
the first.

**Identity (always required):**

| Variable | Value |
|---|---|
| `CSC_LINK` | path to (or base64 of) the `.p12` |
| `CSC_KEY_PASSWORD` | the `.p12` export password |

**Notarization — pick one set:**

| Set | Variables |
|---|---|
| **1 (recommended)** | `APPLE_API_KEY` (path to the `.p8`), `APPLE_API_KEY_ID`, `APPLE_API_ISSUER` |
| 2 | `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` |
| 3 | `APPLE_KEYCHAIN`, `APPLE_KEYCHAIN_PROFILE` |

A **partially** filled set counts as no credentials at all — the build will
sign but not notarize, and will name the artifact `-UNNOTARIZED` rather than
pretend. Empty-string values (how CI exports unset secrets) also count as
absent.

Your **Team ID** and **Apple ID** are not known until you enrol, so nothing in
this repo hardcodes them. Do not add placeholder values — supply them only as
environment variables.

As GitHub Actions secrets, the names are identical:
`CSC_LINK`, `CSC_KEY_PASSWORD`, `APPLE_API_KEY`, `APPLE_API_KEY_ID`,
`APPLE_API_ISSUER`.

### 3.6 Build

```sh
cd desktop && npm run dist:bundled
```

This one command freezes the server, packages the app, signs it, builds the
DMG, notarizes it, staples it, and verifies it.

To make an unsigned build a hard error (use this in a release pipeline):

```sh
NH_REQUIRE_SIGNED=1 npm run dist:bundled
```

### 3.7 Verify — expected output

Run all three. Any deviation means it is **not** shippable.

```sh
codesign -dv --verbose=2 packaging/dist/no_human-0.1.0.dmg
```
Expect `Authority=Developer ID Application: <NAME> (<TEAMID>)`,
`Authority=Developer ID Certification Authority`, `Authority=Apple Root CA`,
and a `Timestamp=` line.
*Today this prints `code object is not signed at all`.*

```sh
spctl -a -t open --context context:primary-signature -v packaging/dist/no_human-0.1.0.dmg
```
Expect `accepted` and `source=Notarized Developer ID`.
*Today this prints `rejected`.*
`accepted` with `source=Developer ID` (no "Notarized") means signing worked but
notarization did not — the artifact will still be refused on a machine that has
never seen it.

```sh
xcrun stapler validate packaging/dist/no_human-0.1.0.dmg
```
Expect `The validate action worked!`.
An unstapled DMG can still fail on a machine with no network even when
notarization succeeded, so this is not optional.

Final check — the filename itself. A shippable build is
`no_human-<version>.dmg`. If it says `-UNSIGNED` or `-UNNOTARIZED`, the build
told you what is missing.

---

## 4. Current state

- The DMG builds and launches, and the bundled server starts from inside the
  `.app`.
- It is **unsigned**: `codesign -dv` → `code object is not signed at all`;
  `spctl` → `rejected`. Expected until §3 is done.
- Desktop auto-update is fully implemented and inert until a signature exists.
- The CLI update check works now.
- No application icon is set — electron-builder logs *"default Electron icon is
  used"*. Cosmetic, but a shipped app should not wear the Electron logo.

## 4a. Gotcha: a user-level `~/.npmrc` can rewrite the lockfile

A `registry=` line in a user-level `~/.npmrc` silently redirects every
`npm install`, and npm rewrites the `"resolved"` URLs in `package-lock.json`
to whatever registry served the request. The repo pins the public registry
with per-directory `.npmrc` files (repo root, `web/`, `desktop/`) because npm
reads project config from the current directory only — it does not walk up.

After any `npm install`, verify the lockfile still resolves only to the
public registry:

```sh
grep '"resolved"' desktop/package-lock.json | grep -vc 'registry\.npmjs\.org'   # must be 0
```

If a foreign host appears, search-and-replace that host's URL prefix back to
`https://registry.npmjs.org/`. The integrity hashes are unaffected when the
foreign registry is a pass-through proxy, so only the URL differs.

## 5. The commands that actually publish

Not run. Listed so publishing is one reviewed command, and so nobody has to
reconstruct it under pressure.

**CLI to PyPI** (irreversible — a version number can never be reused):

```sh
uv build
uv publish            # or: python -m twine upload dist/*
```

**Desktop to GitHub Releases** — requires the repo to be public to be useful:

```sh
gh release create v0.1.0 \
  packaging/dist/no_human-0.1.0.dmg \
  desktop/dist/no_human-0.1.0-arm64-mac.zip \
  desktop/dist/latest-mac.yml \
  --title "no_human 0.1.0" --notes-file CHANGELOG.md
```

All three files are required: the DMG is what a human downloads, and the ZIP +
`latest-mac.yml` are what `electron-updater` reads. A release missing the ZIP
produces an updater that fails for every user.

> ⚠️ `electron-builder`'s GitHub provider defaults `releaseType` to **draft**,
> and a draft release is invisible to the updater — it looks exactly like "no
> updates available". Publish the release, don't leave it drafted.
