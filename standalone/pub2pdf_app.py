#!/usr/bin/env python3
"""Standalone Microsoft Publisher (.pub) to PDF converter.

No Microsoft Office or LibreOffice required. Pipeline:

    .pub --[pub2xhtml (libmspub)]--> XHTML with one SVG per page
         --[split + unit fixes]----> per-page SVG files
         --[svg2pdf (resvg)]-------> per-page vector PDFs
         --[pypdf]-----------------> merged output PDF

The native tools (pub2xhtml.exe, svg2pdf.exe and their DLLs) are expected in
a `bin` directory next to this executable/script.

Run with a path argument for the command line; run with NO arguments (e.g.
double-click the .exe) to open the GUI.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter


# ── core: native-tool pipeline (pure logic is unit-tested in tests/) ──────────

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
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
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


def gather_from_inputs(inputs: list, recurse: bool) -> list:
    """Expand a mix of file and directory paths into a de-duplicated, ordered
    list of .pub files. Used by the GUI, whose queue holds both kinds."""
    seen, out = set(), []
    for raw in inputs:
        p = Path(raw)
        items = []
        if p.is_dir():
            items = sorted(q for q in p.glob("**/*.pub" if recurse else "*.pub") if q.is_file())
        elif p.is_file() and p.suffix.lower() == ".pub":
            items = [p]
        for q in items:
            key = q.resolve()
            if key not in seen:
                seen.add(key)
                out.append(q)
    return out


def require_tools() -> Path:
    """Return the bin/ dir, raising FileNotFoundError if a native tool is missing."""
    tools = bin_dir()
    missing = [exe for exe in ("pub2xhtml.exe", "svg2pdf.exe") if not (tools / exe).exists()]
    if missing:
        raise FileNotFoundError(
            f"{', '.join(missing)} missing from {tools} — broken installation"
        )
    return tools


def run_batch(files: list, output_dir, force: bool, tools: Path, progress=None) -> dict:
    """Convert every file. `progress(i, total, name, status, detail)` is called
    once per file with status in {OK, SKIP, FAIL} (detail = pages or error).
    Returns {converted, skipped, failed, failures:[(name, error)]}."""
    converted = skipped = failed = 0
    failures = []
    total = len(files)
    for i, pub_file in enumerate(files, 1):
        target_dir = Path(output_dir) if output_dir else pub_file.parent
        pdf_path = target_dir / (pub_file.stem + ".pdf")
        if pdf_path.exists() and not force:
            skipped += 1
            if progress:
                progress(i, total, pub_file.name, "SKIP", "PDF exists")
            continue
        try:
            n = convert_one(pub_file, pdf_path, tools)
            converted += 1
            if progress:
                progress(i, total, pub_file.name, "OK", f"{n} pages -> {pdf_path}")
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the batch
            failed += 1
            failures.append((pub_file.name, str(e)))
            if progress:
                progress(i, total, pub_file.name, "FAIL", str(e))
    return {"converted": converted, "skipped": skipped, "failed": failed, "failures": failures}


# ── command-line mode ─────────────────────────────────────────────────────────

def cli_main(args) -> int:
    try:
        tools = require_tools()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    files = collect_pub_files(Path(args.path), args.recurse)
    if not files:
        print(f"no .pub files found under {args.path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    def show(i, total, name, status, detail):
        stream = sys.stderr if status == "FAIL" else sys.stdout
        print(f"[{i}/{total}] {status:<4} {name}" + (f": {detail}" if detail else ""), file=stream)

    r = run_batch(files, output_dir, args.force, tools, progress=show)
    print(f"\nDone: {r['converted']} converted, {r['skipped']} skipped, {r['failed']} failed.")
    return 1 if r["failed"] else 0


# ── GUI mode (tkinter — Python stdlib, no extra dependency) ───────────────────

def launch_gui() -> int:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("pub2pdf — Publisher to PDF")
    root.minsize(620, 470)

    inputs = []          # queued file/folder paths (strings)
    events = queue.Queue()
    state = {"running": False}

    pad = {"padx": 10, "pady": 6}
    root.columnconfigure(0, weight=1)
    root.rowconfigure(3, weight=1)

    ttk.Label(root, text="Convert Microsoft Publisher (.pub) files to PDF",
              font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", **pad)

    # ── queue list + add/remove buttons ──
    qframe = ttk.LabelFrame(root, text="Files to convert")
    qframe.grid(row=1, column=0, sticky="nsew", **pad)
    qframe.columnconfigure(0, weight=1)
    qframe.rowconfigure(3, weight=1)
    listbox = tk.Listbox(qframe, height=7, activestyle="none", selectmode="extended")
    listbox.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=(8, 4), pady=8)
    sb = ttk.Scrollbar(qframe, orient="vertical", command=listbox.yview)
    sb.grid(row=0, column=1, rowspan=4, sticky="ns", pady=8)
    listbox.config(yscrollcommand=sb.set)

    opt_recurse = tk.BooleanVar(value=True)

    def refresh_list():
        listbox.delete(0, tk.END)
        for p in inputs:
            tag = "[folder]  " if Path(p).is_dir() else "          "
            listbox.insert(tk.END, tag + p)
        n = len(gather_from_inputs(inputs, opt_recurse.get()))
        count_lbl.config(text=f"{n} .pub file(s) queued")

    def add_files():
        for p in filedialog.askopenfilenames(
                title="Choose .pub files",
                filetypes=[("Publisher files", "*.pub"), ("All files", "*.*")]):
            if p not in inputs:
                inputs.append(p)
        refresh_list()

    def add_folder():
        d = filedialog.askdirectory(title="Choose a folder of .pub files")
        if d and d not in inputs:
            inputs.append(d)
        refresh_list()

    def remove_selected():
        for idx in reversed(listbox.curselection()):
            del inputs[idx]
        refresh_list()

    def clear_all():
        inputs.clear()
        refresh_list()

    ttk.Button(qframe, text="Add files…", command=add_files).grid(row=0, column=2, sticky="ew", padx=8, pady=(8, 2))
    ttk.Button(qframe, text="Add folder…", command=add_folder).grid(row=1, column=2, sticky="ew", padx=8, pady=2)
    ttk.Button(qframe, text="Remove", command=remove_selected).grid(row=2, column=2, sticky="ew", padx=8, pady=2)
    ttk.Button(qframe, text="Clear", command=clear_all).grid(row=3, column=2, sticky="ew", padx=8, pady=(2, 8))

    # Optional native drag-and-drop if tkinterdnd2 is present (graceful no-op otherwise).
    try:
        from tkinterdnd2 import DND_FILES  # noqa: F401
        def _drop(event):
            for p in root.tk.splitlist(event.data):
                if p not in inputs:
                    inputs.append(p)
            refresh_list()
        listbox.drop_target_register(DND_FILES)
        listbox.dnd_bind("<<Drop>>", _drop)
    except Exception:
        pass

    # ── options ──
    oframe = ttk.LabelFrame(root, text="Options")
    oframe.grid(row=2, column=0, sticky="ew", **pad)
    oframe.columnconfigure(1, weight=1)

    opt_outmode = tk.StringVar(value="beside")
    opt_outdir = tk.StringVar(value="")
    opt_force = tk.BooleanVar(value=False)

    ttk.Radiobutton(oframe, text="Save each PDF next to its .pub", variable=opt_outmode,
                    value="beside").grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
    ttk.Radiobutton(oframe, text="Save all PDFs to this folder:", variable=opt_outmode,
                    value="dir").grid(row=1, column=0, sticky="w", padx=8)
    ttk.Entry(oframe, textvariable=opt_outdir).grid(row=1, column=1, sticky="ew", padx=4)

    def pick_outdir():
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            opt_outdir.set(d)
            opt_outmode.set("dir")
    ttk.Button(oframe, text="Browse…", command=pick_outdir).grid(row=1, column=2, padx=8)
    ttk.Checkbutton(oframe, text="Include subfolders", variable=opt_recurse,
                    command=refresh_list).grid(row=2, column=0, sticky="w", padx=8)
    ttk.Checkbutton(oframe, text="Overwrite existing PDFs", variable=opt_force).grid(
        row=2, column=1, sticky="w", padx=8, pady=(0, 8))

    # ── progress + log ──
    lframe = ttk.LabelFrame(root, text="Progress")
    lframe.grid(row=3, column=0, sticky="nsew", **pad)
    lframe.columnconfigure(0, weight=1)
    lframe.rowconfigure(1, weight=1)

    bar = ttk.Progressbar(lframe, mode="determinate")
    bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
    log = tk.Text(lframe, height=8, state="disabled", wrap="none", font=("Consolas", 9))
    log.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
    log.tag_config("OK", foreground="#1a7f37")
    log.tag_config("SKIP", foreground="#9a6700")
    log.tag_config("FAIL", foreground="#cf222e")

    def logline(text, tag=None):
        log.config(state="normal")
        log.insert(tk.END, text + "\n", (tag,) if tag else ())
        log.see(tk.END)
        log.config(state="disabled")

    # ── footer: count + convert ──
    fframe = ttk.Frame(root)
    fframe.grid(row=4, column=0, sticky="ew", **pad)
    fframe.columnconfigure(0, weight=1)
    count_lbl = ttk.Label(fframe, text="0 .pub file(s) queued")
    count_lbl.grid(row=0, column=0, sticky="w")
    convert_btn = ttk.Button(fframe, text="Convert")
    convert_btn.grid(row=0, column=1, sticky="e")

    def set_running(running):
        state["running"] = running
        convert_btn.config(text="Converting…" if running else "Convert",
                           state="disabled" if running else "normal")

    def worker(files, output_dir, force, tools):
        def progress(i, total, name, status, detail):
            events.put(("file", i, total, name, status, detail))
        r = run_batch(files, output_dir, force, tools, progress=progress)
        events.put(("done", r))

    def start():
        if state["running"]:
            return
        try:
            tools = require_tools()
        except FileNotFoundError as e:
            messagebox.showerror("pub2pdf", str(e))
            return
        files = gather_from_inputs(inputs, opt_recurse.get())
        if not files:
            messagebox.showinfo("pub2pdf", "Add some .pub files or a folder first.")
            return
        output_dir = opt_outdir.get().strip() if opt_outmode.get() == "dir" else None
        if opt_outmode.get() == "dir" and not output_dir:
            messagebox.showinfo("pub2pdf", "Choose an output folder, or switch to 'next to its .pub'.")
            return
        log.config(state="normal"); log.delete("1.0", tk.END); log.config(state="disabled")
        bar.config(maximum=len(files), value=0)
        logline(f"Converting {len(files)} file(s)…")
        set_running(True)
        threading.Thread(target=worker, args=(files, output_dir, opt_force.get(), tools),
                         daemon=True).start()

    convert_btn.config(command=start)

    def drain():
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "file":
                    _, i, total, name, status, detail = ev
                    bar.config(value=i)
                    logline(f"[{i}/{total}] {status:<4} {name}" + (f"  {detail}" if detail else ""), status)
                elif ev[0] == "done":
                    r = ev[1]
                    logline(f"\nDone: {r['converted']} converted, {r['skipped']} skipped, "
                            f"{r['failed']} failed.", "FAIL" if r["failed"] else "OK")
                    set_running(False)
                    if r["failed"]:
                        messagebox.showwarning("pub2pdf", f"{r['failed']} file(s) failed — see the log.")
                    else:
                        messagebox.showinfo("pub2pdf", f"All done — {r['converted']} converted.")
        except queue.Empty:
            pass
        root.after(80, drain)

    refresh_list()
    root.after(80, drain)
    root.mainloop()
    return 0


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pub2pdf",
        description="Convert Microsoft Publisher (.pub) files to PDF — "
                    "standalone, no Office or LibreOffice required. "
                    "Run with no arguments to open the GUI.",
    )
    parser.add_argument("path", nargs="?",
                        help="a .pub file or a directory of .pub files (omit to open the GUI)")
    parser.add_argument("-o", "--output-dir",
                        help="directory to write PDFs into (default: next to source)")
    parser.add_argument("-r", "--recurse", action="store_true",
                        help="also search subdirectories")
    parser.add_argument("-f", "--force", action="store_true",
                        help="overwrite existing PDFs")
    parser.add_argument("--gui", action="store_true", help="force the GUI even with a path given")
    args = parser.parse_args()

    if args.gui or args.path is None:
        sys.exit(launch_gui())
    sys.exit(cli_main(args))


if __name__ == "__main__":
    main()
