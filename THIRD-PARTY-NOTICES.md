# Third-party notices

no_human
Copyright (c) 2026 Eyal Golan

**This file is not a licence.** no_human is released under the MIT Licence; the
terms are in [`LICENSE`](LICENSE) and nothing here changes them. What this file
records is attribution, and the third-party obligations that a *packaged binary*
of no_human has to discharge before it is handed to anyone. MIT has no notice
mechanism of its own, so they are written down here instead.

## Source tree

This repository's source tree contains no vendored third-party code. Runtime
dependencies are declared in [`pyproject.toml`](pyproject.toml),
[`web/package.json`](web/package.json) and
[`desktop/package.json`](desktop/package.json), and are fetched at install time
under their own licences. Cloning this repository therefore obliges you to
nothing beyond `LICENSE`.

## Binary distributions

A packaged desktop build is a different artifact, and it carries obligations
this file does not by itself discharge.

- It embeds **Electron** and **Chromium**. Their notices —
  `node_modules/electron/dist/LICENSE` and `LICENSES.chromium.html` — must ship
  inside the bundle, in `Contents/Resources`.
- It embeds a frozen Python server produced with **PyInstaller** (see below).

Do not distribute a build until those notices are included. They are shipped by
`extraResources` in
[`desktop/electron-builder.config.cjs`](desktop/electron-builder.config.cjs),
which places Electron's licence at `Contents/Resources/LICENSE.electron.txt` and
Chromium's at `Contents/Resources/LICENSES.chromium.html`.

This paragraph previously cited `packaging/build-installer.sh`. That script
freezes the Python server and does not build the `.app`; it contains no licence
handling at all, and the notices were in fact **absent** from the built bundle.
A legal obligation pointed at a mechanism that does not exist is worse than one
with no pointer, because it reads as already discharged.

**Why they were absent** — corrected 2026-08-01; the first explanation given
here was wrong. Nothing of Electron's was overwritten. Electron ships `LICENSE`
and `LICENSES.chromium.html` at the *top* of `node_modules/electron/dist/`,
alongside `Electron.app` — **not** inside `Contents/Resources`, which has never
contained a `LICENSE` at all (checked: `ls node_modules/electron/dist/` lists
`Electron.app`, `LICENSE`, `LICENSES.chromium.html`, `version`; the same `ls`
inside `Electron.app/Contents/Resources/` matches nothing for `licen`). So our
`{ from: "../LICENSE", to: "LICENSE" }` never collided with anything.

electron-builder *deletes* them instead, and on macOS skips the rename that
would have kept Electron's:

* `app-builder-lib/out/electron/electronMac.js:219-220` calls
  `unlinkIfExists(appOutDir/LICENSE)` and
  `unlinkIfExists(appOutDir/LICENSES.chromium.html)`. `appOutDir` is
  `dist/mac-arm64/`, one level *above* the `.app` — which is why a built
  `dist/mac-arm64/` contains `no_human.app` and nothing else.
* `app-builder-lib/out/electron/ElectronFramework.js:236-239` renames
  `LICENSE` to `LICENSE.electron.txt` **only when the platform is not macOS**
  (`isMac ? Promise.resolve() : rename(...)`). On macOS that preservation step
  never runs.

So the notices are not lost to a name collision that a rename could fix; they
are removed by the packager, and shipping them requires putting them back
explicitly. That is what the `extraResources` entries below do, at names chosen
so they cannot collide with our own `LICENSE`.

## PyInstaller bootloader

PyInstaller (GPL-2.0-or-later WITH Bootloader-exception) is used only as a build
tool. Its bootloader exception is what permits distributing the resulting
application under this repository's own terms:

> "In addition to the permissions in the GNU General Public License, the authors
> give you unlimited permission to link or embed compiled bootloader and related
> files into combinations with other programs, and to distribute those
> combinations without any restriction coming from the use of those files. (The
> General Public License restrictions do apply in other respects; for example,
> they cover modification of the files, and distribution when not linked into a
> combined executable.)"

The bootloader is used unmodified. If it is ever modified, this exception no
longer applies and the GPL terms govern that component.

## Third-party trademarks

Everything above is about *copyright* licences. This section is about *trademarks*,
which those licences do not touch: an MIT grant over a vendor's code says nothing
about their name or their logo, and a CC licence over a logo file does not license
the mark it depicts.

no_human integrates with products made by other companies, and names them so that
users know what it works with. That is nominative use. Two of these acknowledgements
are **required** by the owner rather than offered as a courtesy — GitLab's Trademark
Guidelines §1.2.2 and the Jenkins project's trademark policy each prescribe a
statement — and the rest are the forms the owners publish.

- Jira is a trademark of Atlassian Pty Ltd.
- GITLAB is a trademark of GitLab Inc. in the United States and other countries and
  regions.
- GitHub is a trademark of GitHub, Inc.
- Slack is a trademark and service mark of Slack Technologies, Inc., registered in
  the U.S. and in other countries.
- Jenkins® is a registered trademark of LF Charities Inc.
- CircleCI is a trademark or registered trademark of Circle Internet Services, Inc.
- Claude and Anthropic are trademarks of Anthropic PBC.

All other trademarks are the property of their respective owners.

**no_human is an independent product. It is not affiliated with, endorsed, sponsored
or approved by any of these companies, and nothing in this repository or in its
marketing should be read as saying otherwise.**

Two standing rules follow from this, and they are the reason this section exists
rather than being a formality:

1. **Do not ship a vendor's logo — official or redrawn — without checking that
   vendor's current guidelines.** Redrawing a mark is not the safe option; it is the
   worse one, because it adds a modification breach (and possibly a copyright one) to
   the trademark question. GitLab forbids third-party logo use outright; Microsoft and
   CircleCI require a written licence.
2. **Do not describe no_human as a partner, an official integration, or as certified
   by anyone.** It is none of those things.

The reasoning, the clauses these lines come from, and what still needs a lawyer are
in [`docs/INTEGRATIONS_LEGAL.md`](docs/INTEGRATIONS_LEGAL.md). The strings above were
transcribed from vendor pages read on 2026-08-01 and should be re-checked against the
live pages before a public release — a paraphrase in a trademark notice is worse than
no notice.
