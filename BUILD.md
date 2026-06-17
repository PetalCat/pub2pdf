# Building the standalone app

The standalone converter (`dist/pub2pdf-standalone/`) is **not** committed —
it is produced by compiling the native `.pub` parser and packaging it. CI does
this on every `v*` tag and attaches the zip to a GitHub Release
(see [`.github/workflows/release.yml`](.github/workflows/release.yml)).

## Components

| Component | Source | Role |
|---|---|---|
| **libmspub** 0.1.4 + **librevenge** 0.0.5 | LibreOffice src mirror | `.pub` → SVG (`pub2xhtml.exe`) |
| **svg2pdf** (resvg) | `cargo install svg2pdf-cli` | SVG → vector PDF |
| **orchestrator** | [`standalone/pub2pdf_app.py`](standalone/pub2pdf_app.py) | drives the pipeline, frozen with PyInstaller |

## Build locally (Windows)

Prerequisites:

- [MSYS2](https://www.msys2.org/) at `C:\msys64`
- A Rust toolchain (`rustup`) and Python 3 with `pip`

```bash
# 1. MSYS2 toolchain + native deps
/c/msys64/usr/bin/bash -lc 'pacman -S --noconfirm --needed \
  make m4 mingw-w64-x86_64-gcc mingw-w64-x86_64-make \
  mingw-w64-x86_64-pkgconf mingw-w64-x86_64-boost \
  mingw-w64-x86_64-icu mingw-w64-x86_64-zlib'

# 2. SVG renderer
cargo install svg2pdf-cli --locked

# 3. Compile libmspub/librevenge and assemble standalone/bin/
MSYSTEM=MINGW64 /c/msys64/usr/bin/bash -lc \
  'SVG2PDF_EXE="$USERPROFILE/.cargo/bin/svg2pdf.exe" bash scripts/build-native.sh'

# 4. Freeze the orchestrator and package
py -m pip install pyinstaller pypdf
py -m PyInstaller --onefile --console --name pub2pdf \
  --distpath dist/pub2pdf-standalone \
  --workpath build/pyinstaller --specpath build/pyinstaller \
  standalone/pub2pdf_app.py
cp -r standalone/bin dist/pub2pdf-standalone/bin
cp standalone/README.txt dist/pub2pdf-standalone/
```

The result in `dist/pub2pdf-standalone/` runs on any Windows machine with no
Office, LibreOffice, or Python installed.

## Known build gotchas

- **`m4: command not found`** while building librevenge — install `m4`
  (the Windows resource compiler step needs it).
- **`'uint32_t' has not been declared`** in libmspub — GCC 13+ dropped the
  transitive `<cstdint>` include the 2018-era headers relied on.
  `scripts/build-native.sh` forces it with `CXXFLAGS="-include cstdint"`.
- **Page size / tiny text** — `pub2xhtml` emits page dimensions in inches and
  `font-size` in inches while coordinates are in points. The orchestrator
  rescales font sizes (×72) and renders with `svg2pdf --dpi 96`. Don't "fix"
  these independently.
