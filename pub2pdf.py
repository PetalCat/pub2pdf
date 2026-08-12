#!/usr/bin/env python3
"""Convert Microsoft Publisher (.pub) files to PDF.

Microsoft Publisher is REQUIRED. The tool drives Publisher over COM for a
pixel-identical PDF, and if Publisher is not installed it stops with a clear
message rather than silently producing a lower-fidelity file.

  publisher    Microsoft Publisher via COM automation (Windows + Office only).
               Pixel-identical to Publisher's own "Save as PDF". The default.
               Requires the pywin32 package.
  libreoffice  LibreOffice headless (soffice --convert-to pdf) using its
               libmspub import filter. APPROXIMATE layout — fonts may
               substitute and text may reflow. Reachable only by asking for it
               explicitly with --engine libreoffice; never used as a fallback.

Usage:
    py pub2pdf.py newsletter.pub
    py pub2pdf.py C:\\flyers --recurse --output-dir C:\\flyers\\pdf
    py pub2pdf.py C:\\flyers --engine libreoffice --force   # approximate, opt-in
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Publisher COM enum constants
PB_FIXED_FORMAT_TYPE_PDF = 2   # PbFixedFormatType.pbFixedFormatTypePDF
PB_INTENT_PRINTING = 2         # PbFixedFormatIntent.pbIntentPrinting (high quality)

SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in SOFFICE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def publisher_exe() -> str | None:
    """Path to Publisher's registered COM server if Publisher exists AND is
    installed, else None.

    Both halves are checked: the Publisher.Application ProgID must be registered,
    and the LocalServer32 binary it points at must actually be on disk. A stale
    ProgID left behind by an uninstall (registry present, MSPUB.EXE gone) reads
    as not-installed — the tool must not claim faithful conversion is available
    when it isn't.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    def _default(path: str) -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as key:
                return winreg.QueryValueEx(key, None)[0]
        except OSError:
            return None

    clsid = _default(r"Publisher.Application\CLSID")
    if not clsid:
        return None
    server = _default(rf"CLSID\{clsid}\LocalServer32")
    if not server:
        return None
    # LocalServer32 looks like:  C:\Program Files\...\MSPUB.EXE /automation
    # The path can contain spaces and the trailing switch is unquoted, so match
    # up to the .exe rather than splitting on the first space.
    m = re.match(r'\s*"?(.+?\.exe)"?', server, re.IGNORECASE)
    if not m:
        return None
    exe = m.group(1)
    return exe if os.path.isfile(exe) else None


class PublisherEngine:
    """Converts via Microsoft Publisher COM. One app instance per batch."""

    name = "publisher"

    def __enter__(self):
        import pythoncom
        import win32com.client
        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        self._app = win32com.client.Dispatch("Publisher.Application")
        return self

    def __exit__(self, *exc):
        self._app.Quit()
        self._pythoncom.CoUninitialize()

    def convert(self, pub_file: Path, pdf_path: Path) -> None:
        # Publisher rejects the documented ReadOnly/SaveChanges args for
        # .pub files (E_INVALIDARG) — open with the path only. The document
        # is never saved, so the source is not modified.
        doc = self._app.Open(str(pub_file.resolve()))
        try:
            doc.ExportAsFixedFormat(
                PB_FIXED_FORMAT_TYPE_PDF, str(pdf_path), PB_INTENT_PRINTING
            )
        finally:
            doc.Close()


class LibreOfficeEngine:
    """Converts via headless LibreOffice (libmspub import filter)."""

    name = "libreoffice"

    def __init__(self, soffice: str):
        self._soffice = soffice

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def convert(self, pub_file: Path, pdf_path: Path) -> None:
        # soffice names the output <stem>.pdf inside --outdir; that matches
        # pdf_path because the caller derives it from the same stem.
        result = subprocess.run(
            [
                self._soffice, "--headless", "--norestore",
                "--convert-to", "pdf", "--outdir", str(pdf_path.parent),
                str(pub_file.resolve()),
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or not pdf_path.exists():
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"soffice failed (exit {result.returncode}): {detail}")


def pick_engine(choice: str):
    # Publisher is REQUIRED, not merely preferred: the tool must never silently
    # hand back an approximate PDF. 'auto' and 'publisher' both demand the
    # faithful COM path; the approximate LibreOffice engine is reachable only by
    # asking for it explicitly with --engine libreoffice.
    if choice in ("auto", "publisher"):
        if not publisher_exe():
            sys.exit(
                "error: Microsoft Publisher is required but is not installed on "
                "this machine.\n"
                "Ask IT to install Microsoft Publisher (it ships with Office "
                "Professional / Microsoft 365 Apps for enterprise).\n"
                "If you knowingly accept lower-fidelity output you can force the "
                "approximate engine with:  --engine libreoffice"
            )
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            sys.exit(
                "error: Microsoft Publisher is installed but the pywin32 package "
                "is missing, so it can't be driven.\n"
                "  py -m pip install pywin32"
            )
        return PublisherEngine()

    # choice == "libreoffice": explicit, deliberate opt-in to approximate output.
    soffice = find_soffice()
    if not soffice:
        sys.exit("error: LibreOffice not found (install it or add soffice to PATH)")
    print(
        "WARNING: --engine libreoffice produces APPROXIMATE layout via LibreOffice's "
        "libmspub filter, not Publisher. Fonts may substitute and text boxes may "
        "reflow. Use Publisher for a faithful PDF.",
        file=sys.stderr,
    )
    return LibreOfficeEngine(soffice)


# .pub only: Parker confirmed no .pubx files exist, so no speculative support for
# a format nobody has. If one ever turns up, inspect a real sample and decide then.
PUB_EXTS = (".pub",)
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_publisher_file(path: Path) -> bool:
    """True if the file is an OLE2 compound document, which is what Microsoft
    Publisher writes (header D0 CF 11 E0 A1 B1 1A E1, plus Quill/Escher streams).

    The .pub extension is badly overloaded — the flood on any drive scan is SSH
    public keys (ssh-rsa / ssh-ed25519 …, plain text), which would otherwise show
    up as documents and fail conversion one confusing row at a time. Eight bytes,
    no parsing, no Publisher round-trip. Anything text- or zip-based is not an
    OLE2 doc and is correctly excluded.
    """
    try:
        with open(path, "rb") as f:
            return f.read(8) == OLE2_MAGIC
    except OSError:
        return False


def collect_pub_files(path: Path, recurse: bool) -> list[Path]:
    if path.is_file():
        # An explicitly named file is the user's choice — don't second-guess it.
        if path.suffix.lower() not in PUB_EXTS:
            sys.exit(f"error: not a Publisher file: {path}")
        return [path]
    if path.is_dir():
        pats = [f"**/*{e}" for e in PUB_EXTS] if recurse else [f"*{e}" for e in PUB_EXTS]
        cands = {p for pat in pats for p in path.glob(pat) if p.is_file()}
        # Bulk discovery: keep only real Publisher files, drop the .pub impostors.
        return sorted(p for p in cands if is_publisher_file(p))
    sys.exit(f"error: path not found: {path}")


def convert_all(engine, files: list[Path], output_dir: Path | None, force: bool) -> int:
    converted = skipped = failed = 0

    with engine:
        for pub_file in files:
            target_dir = output_dir if output_dir else pub_file.parent
            pdf_path = target_dir / (pub_file.stem + ".pdf")

            if pdf_path.exists() and not force:
                print(f"SKIP  {pub_file.name} (PDF exists, use --force to overwrite)")
                skipped += 1
                continue

            try:
                engine.convert(pub_file, pdf_path)
                print(f"OK    {pub_file.name} -> {pdf_path}")
                converted += 1
            except Exception as e:
                print(f"FAIL  {pub_file.name}: {e}", file=sys.stderr)
                failed += 1

    print(f"\nDone: {converted} converted, {skipped} skipped, {failed} failed "
          f"(engine: {engine.name}).")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Microsoft Publisher (.pub) files to PDF."
    )
    parser.add_argument("path", help="a .pub file or a directory containing .pub files")
    parser.add_argument(
        "-o", "--output-dir",
        help="directory to write PDFs into (default: next to each source file)",
    )
    parser.add_argument(
        "-r", "--recurse", action="store_true",
        help="when path is a directory, also search subdirectories",
    )
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="overwrite existing PDFs instead of skipping",
    )
    parser.add_argument(
        "-e", "--engine", choices=["auto", "publisher", "libreoffice"],
        default="auto",
        help="conversion engine (default: auto — require Microsoft Publisher and "
             "convert faithfully over COM; 'libreoffice' forces the approximate "
             "engine, never used as a fallback)",
    )
    args = parser.parse_args()

    files = collect_pub_files(Path(args.path), args.recurse)
    if not files:
        sys.exit(f"no .pub files found under {args.path}")

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    engine = pick_engine(args.engine)
    sys.exit(convert_all(engine, files, output_dir, args.force))


if __name__ == "__main__":
    main()
