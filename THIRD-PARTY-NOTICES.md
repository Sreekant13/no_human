# Third-party notices

no_human
Copyright (c) 2025-2026 eyalgolan

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

Do not distribute a build until those notices are included. See
[`packaging/build-installer.sh`](packaging/build-installer.sh).

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
