#!/usr/bin/env python3
"""Standalone Microsoft Publisher (.pub) to PDF converter.

No Microsoft Office or LibreOffice required. Pipeline:

    .pub --[pub2xhtml (libmspub)]--> XHTML with one SVG per page
         --[split + unit fixes]----> per-page SVG files
         --[svg2pdf (resvg)]-------> per-page vector PDFs
         --[pypdf]-----------------> merged output PDF

The native tools (pub2xhtml.exe, svg2pdf.exe and their DLLs) are expected in
a `bin` directory next to this executable/script.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def bin_dir() -> Path:
    # PyInstaller onefile: sys.executable is the app; bin/ sits next to it.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "bin"
    return Path(__file__).parent / "bin"


SVG_PAGE_RE = re.compile(r"<svg:svg\b.*?</svg:svg>", re.DOTALL)
FONT_SIZE_RE = re.compile(r'font-size="([0-9.]+)"')


def fix_font_sizes(svg: str) -> str:
    # libmspub emits font-size in inches while all coordinates are in
    # points; convert so text is not rendered 72x too small.
    return FONT_SIZE_RE.sub(
        lambda m: f'font-size="{float(m.group(1)) * 72:.2f}"', svg
    )


def extract_pages(xhtml_text: str) -> list[str]:
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'
    return [header + fix_font_sizes(m.group(0)) for m in SVG_PAGE_RE.finditer(xhtml_text)]


def run_tool(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kw
    )


def convert_one(pub_file: Path, pdf_path: Path, tools: Path) -> int:
    """Convert a single .pub file. Returns the page count."""
    with tempfile.TemporaryDirectory(prefix="pub2pdf_") as tmp:
        tmp = Path(tmp)
        xhtml = tmp / "doc.xhtml"

        result = run_tool([str(tools / "pub2xhtml.exe"), str(pub_file), str(xhtml)])
        if result.returncode != 0 or not xhtml.exists():
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"pub2xhtml failed (exit {result.returncode}): {detail}")

        pages = extract_pages(xhtml.read_text(encoding="utf-8"))
        if not pages:
            raise RuntimeError("no pages found (unsupported or corrupt .pub?)")

        page_pdfs = []
        for i, svg in enumerate(pages, 1):
            svg_file = tmp / f"page{i}.svg"
            page_pdf = tmp / f"page{i}.pdf"
            svg_file.write_text(svg, encoding="utf-8")
            result = run_tool(
                # --dpi 96: pub2xhtml sizes pages in inches (CSS 96 px/in);
                # this maps them to the correct PDF point dimensions.
                [str(tools / "svg2pdf.exe"), "--dpi", "96",
                 str(svg_file), str(page_pdf)]
            )
            if result.returncode != 0 or not page_pdf.exists():
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"svg2pdf failed on page {i}: {detail}")
            page_pdfs.append(page_pdf)

        writer = PdfWriter()
        for page_pdf in page_pdfs:
            for page in PdfReader(str(page_pdf)).pages:
                writer.add_page(page)
        with open(pdf_path, "wb") as fh:
            writer.write(fh)
        return len(page_pdfs)


def collect_pub_files(path: Path, recurse: bool) -> list:
    if path.is_file():
        if path.suffix.lower() != ".pub":
            sys.exit(f"error: not a .pub file: {path}")
        return [path]
    if path.is_dir():
        pattern = "**/*.pub" if recurse else "*.pub"
        return sorted(p for p in path.glob(pattern) if p.is_file())
    sys.exit(f"error: path not found: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pub2pdf",
        description="Convert Microsoft Publisher (.pub) files to PDF — "
                    "standalone, no Office or LibreOffice required.",
    )
    parser.add_argument("path", help="a .pub file or a directory containing .pub files")
    parser.add_argument("-o", "--output-dir",
                        help="directory to write PDFs into (default: next to source)")
    parser.add_argument("-r", "--recurse", action="store_true",
                        help="also search subdirectories")
    parser.add_argument("-f", "--force", action="store_true",
                        help="overwrite existing PDFs")
    args = parser.parse_args()

    tools = bin_dir()
    for exe in ("pub2xhtml.exe", "svg2pdf.exe"):
        if not (tools / exe).exists():
            sys.exit(f"error: {exe} missing from {tools} — broken installation")

    files = collect_pub_files(Path(args.path), args.recurse)
    if not files:
        sys.exit(f"no .pub files found under {args.path}")

    output_dir = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    converted = skipped = failed = 0
    for pub_file in files:
        target_dir = output_dir if output_dir else pub_file.parent
        pdf_path = target_dir / (pub_file.stem + ".pdf")
        if pdf_path.exists() and not args.force:
            print(f"SKIP  {pub_file.name} (PDF exists, use --force to overwrite)")
            skipped += 1
            continue
        try:
            n = convert_one(pub_file, pdf_path, tools)
            print(f"OK    {pub_file.name} -> {pdf_path} ({n} pages)")
            converted += 1
        except Exception as e:
            print(f"FAIL  {pub_file.name}: {e}", file=sys.stderr)
            failed += 1

    print(f"\nDone: {converted} converted, {skipped} skipped, {failed} failed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
