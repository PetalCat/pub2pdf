# pub2pdf

Converts Microsoft Publisher (`.pub`) files to PDF.

**Two deliverables in this repo:**

1. **`pub2pdf.py`** — script with two engines, auto-detected (see table below)
2. **standalone app** — fully self-contained, **no Office, no LibreOffice, no
   Python needed**: libmspub 0.1.4 + librevenge 0.0.5 (compiled with MinGW-w64),
   svg2pdf (Rust/resvg), and a PyInstaller orchestrator
   (`standalone/pub2pdf_app.py`). It is **not committed** — CI builds it on each
   `v*` tag and publishes the zip to
   [Releases](../../releases). To build it yourself, see [BUILD.md](BUILD.md).
   **Double-click `pub2pdf.exe` to open the GUI** (queue files/folders, pick
   where PDFs go, watch a progress bar) — or pass a path on the command line for
   the same batch conversion headless. Ideal for migrating a whole folder of
   `.pub` files at once with no install.

| Engine | Needs | Fidelity |
|---|---|---|
| `publisher` | MS Publisher + `pywin32` (Windows) | Pixel-identical — drives Publisher's own "Save as PDF" via COM |
| `libreoffice` | LibreOffice (free, any OS) | Approximate — LibreOffice re-renders the layout with its libmspub filter |
| standalone app | nothing | Approximate — same libmspub parser as LibreOffice, rendered via resvg |

Auto-detection prefers Publisher when installed, otherwise falls back to
LibreOffice (`soffice` on PATH or in the standard install directory).
So the same script works on machines **without any Microsoft Office install** —
just install LibreOffice there.

## Usage

```
pub2pdf <path> [options]

  path                a .pub file or a directory containing .pub files
  -o, --output-dir D  write PDFs into D (default: next to each source file)
  -r, --recurse       when path is a directory, also search subdirectories
  -f, --force         overwrite existing PDFs instead of skipping
  -e, --engine E      auto (default) | publisher | libreoffice
```

Examples:

```
pub2pdf newsletter.pub
pub2pdf C:\flyers --recurse --output-dir C:\flyers\pdf
pub2pdf C:\flyers --engine libreoffice --force
```

Exit code is 1 if any file failed to convert, 0 otherwise.

> Note: this is a Python tool on purpose — `powershell.exe` is blocked by
> group policy on this machine, so a `.ps1` converter would not run.
> Run it via `py` or the `pub2pdf.cmd` wrapper.

## Gotchas

- Publisher's COM `Open` rejects the documented `ReadOnly`/`SaveChanges`
  arguments for `.pub` files with `E_INVALIDARG`; the script opens with the
  path only and never saves, so source files are not modified.
- With the `publisher` engine, Publisher briefly starts in the background
  (one instance reused across the whole batch). Close any interactive
  Publisher session with unsaved work before running large batches.
- The `libreoffice` engine starts one `soffice` process per file; large
  batches are slower but require no Office license.
- LibreOffice fidelity caveats: complex Publisher layouts (linked text
  boxes, WordArt, master pages) may shift or simplify. Spot-check output
  when the source design matters.
