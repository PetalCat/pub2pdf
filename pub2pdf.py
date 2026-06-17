#!/usr/bin/env python3
"""Convert Microsoft Publisher (.pub) files to PDF.

Two conversion engines, auto-detected in this order:

  publisher    Microsoft Publisher via COM automation (Windows + Office only).
               Pixel-identical to Publisher's own "Save as PDF".
               Requires the pywin32 package.
  libreoffice  LibreOffice headless (soffice --convert-to pdf) using its
               libmspub import filter. Works without any Microsoft Office
               install, but layout fidelity is approximate.

Usage:
    py pub2pdf.py newsletter.pub
    py pub2pdf.py C:\\flyers --recurse --output-dir C:\\flyers\\pdf
    py pub2pdf.py C:\\flyers --engine libreoffice --force
"""

import argparse
import os
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


def publisher_com_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Publisher.Application"):
            return True
    except OSError:
        return False


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
    if choice in ("auto", "publisher") and publisher_com_available():
        try:
            import win32com.client  # noqa: F401
            return PublisherEngine()
        except ImportError:
            if choice == "publisher":
                sys.exit("error: pywin32 not installed (py -m pip install pywin32)")
    elif choice == "publisher":
        sys.exit("error: Microsoft Publisher is not installed on this machine")

    soffice = find_soffice()
    if soffice:
        return LibreOfficeEngine(soffice)
    if choice == "libreoffice":
        sys.exit("error: LibreOffice not found (install it or add soffice to PATH)")
    sys.exit(
        "error: no conversion engine available.\n"
        "Install Microsoft Publisher (plus 'py -m pip install pywin32') "
        "or LibreOffice (https://www.libreoffice.org/)."
    )


def collect_pub_files(path: Path, recurse: bool) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() != ".pub":
            sys.exit(f"error: not a .pub file: {path}")
        return [path]
    if path.is_dir():
        pattern = "**/*.pub" if recurse else "*.pub"
        return sorted(p for p in path.glob(pattern) if p.is_file())
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
        help="conversion engine (default: auto — Publisher if installed, "
             "else LibreOffice)",
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
