# Third-Party Notices

## Pre-assembly notice

This committed document records the fixed, reviewable notices known before the
architecture-specific macOS resources are assembled. It is intentionally not a
release dependency inventory. Release CI must assemble the PyInstaller and
PostgreSQL trees and then run `packaging/macos/generate-notices.sh`; the generated
file inventories every packaged resource and the locked Rust, Python, and npm
production dependencies. `packaging/macos/generate-notices.sh --check` rejects a
stale generated file.

## Application notice

Poly is the top-level application. This document records third-party material
used by or distributed with it; it does not grant a license to Poly itself.

## Reproducible inventory inputs

- Rust/Tauri: `src-tauri/Cargo.lock`, inventoried by `cargo-about` or
  `cargo-license`.
- Python: `uv.lock`, inventoried from the frozen environment by `pip-licenses`,
  including license text when provided by installed distributions.
- Web frontend: production entries in `frontend/package-lock.json`.
- Embedded runtime: the assembled CPython license and PyInstaller bootloader
  license.
- Database: the pinned PostgreSQL 16.15 source `COPYRIGHT` file.
- Native libraries: every additional packaged Mach-O must have an explicit
  matching license file; generation fails when evidence is absent.

## Tauri, WebKit, and system libraries

Tauri and its redistributed Rust dependencies are included in the generated
Rust section. The application uses the WebKit framework supplied by macOS;
WebKit and Apple system libraries are dynamically used system components and
are not copied into the application bundle.

## PostgreSQL License

PostgreSQL Database Management System
(formerly known as Postgres, then as Postgres95)

Portions Copyright © 1996-2025, The PostgreSQL Global Development Group

Portions Copyright © 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement is
hereby granted, provided that the above copyright notice and this paragraph and
the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR DIRECT,
INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING LOST PROFITS,
ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION, EVEN IF THE
UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES, INCLUDING,
BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE. THE SOFTWARE PROVIDED HEREUNDER IS ON AN "AS IS" BASIS, AND
THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO PROVIDE MAINTENANCE, SUPPORT,
UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
