#!/usr/bin/env python3
"""pub2pdf desktop app — drop Publisher files, get faithful PDFs.

A thin GUI shell over the faithful Publisher COM engine in pub2pdf.py. The bar
for "done" is: a City clerk who has never seen it drops a file or a folder on the
window and gets PDFs next to the originals, without reading anything.

Design (spec by Janet, build by Glenn):
  - Drop zone is the primary surface; the file list appears only after files land.
  - Per-file state you can read: Queued / Converting / Done / Failed(reason).
  - Failures survive the run — you see which files failed and can retry just those.
  - "Open folder" when it finishes; remembers the last output choice.
  - Publisher missing = a plain sentence on the startup screen, never a crash.

Faithful engine only. There is deliberately no approximate fallback here — that
is a conscious CLI opt-in (pub2pdf.py --engine libreoffice), not something a user
gets by accident.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import customtkinter as ctk
import tkinterdnd2

import pub2pdf

APP_NAME = "pub2pdf"
ACCENT = "#2f6df6"
COL = {
    "queued": ("#4a4a4a", "#e8e8e8"),      # bg, fg — brighter so it's readable
    "converting": ("#8a5a00", "#ffdf9e"),
    "done": ("#1f5f37", "#b8f0c9"),
    "failed": ("#6e1f1f", "#ffc2c2"),
}


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def reveal(path: Path) -> None:
    """Open a folder in Explorer (or select a file inside it)."""
    try:
        if path.is_file():
            subprocess.run(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # noqa: S606 (Windows-only, trusted path)
    except OSError:
        pass


class DnDTk(ctk.CTk, tkinterdnd2.TkinterDnD.DnDWrapper):
    """A customtkinter root that also speaks native drag-and-drop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = tkinterdnd2.TkinterDnD._require(self)


class FileRow(ctk.CTkFrame):
    """One row: filename + a status chip you can read at a glance."""

    def __init__(self, master, pub_file: Path):
        super().__init__(master, fg_color="transparent")
        self.pub_file = pub_file
        self.pdf_path: Path | None = None
        self.state = "queued"

        self.grid_columnconfigure(0, weight=1)
        self.name = ctk.CTkLabel(self, text=pub_file.name, anchor="w")
        self.name.grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=6)
        self.chip = ctk.CTkLabel(self, text="Queued", width=110, corner_radius=8,
                                 fg_color=COL["queued"][0], text_color=COL["queued"][1])
        self.chip.grid(row=0, column=1, padx=(0, 10), pady=6)

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        label = {"queued": "Queued", "converting": "Converting…",
                 "done": "Done", "failed": "Failed"}[state]
        bg, fg = COL[state]
        self.chip.configure(text=label, fg_color=bg, text_color=fg)
        # Let the chip carry the state; keep the filename neutral so we don't say
        # the same thing twice (the old green-on-green). Only a failure adds text
        # the user needs — the reason, inline — in a muted red.
        if state == "failed" and detail:
            self.name.configure(text=f"{self.pub_file.name}   —   {detail}",
                                text_color="#e79a9a")
        else:
            self.name.configure(text=self.pub_file.name, text_color="#dcdcdc")


class App(DnDTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(f"{APP_NAME} — Publisher to PDF")
        self.geometry("760x600")
        self.minsize(620, 480)
        self._center()

        self.settings = load_settings()
        self.output_dir: Path | None = (
            Path(self.settings["output_dir"]) if self.settings.get("output_dir") else None
        )
        self.rows: list[FileRow] = []
        self.events: queue.Queue = queue.Queue()
        self.running = False

        self.publisher = pub2pdf.publisher_exe()
        self._build()
        if not self.publisher:
            self._show_no_publisher()
        self._after_id = self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        # Cancel the pending poll so it can't fire on a destroyed window.
        try:
            self.after_cancel(self._after_id)
        except Exception:  # noqa: BLE001 — teardown, swallow Tcl noise
            pass
        self.destroy()

    # ---- layout ---------------------------------------------------------
    def _center(self) -> None:
        self.update_idletasks()
        w, h = 760, 600
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Publisher → PDF",
                     font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, sticky="w")
        self.subtitle = ctk.CTkLabel(
            header, text="Convert Publisher files to PDF. Your .pub files stay untouched.",
            text_color="#9aa0a6")
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        # Drop zone — the primary surface.
        self.drop = ctk.CTkFrame(self, fg_color="#242424", border_width=2,
                                 border_color="#3a3a3a", corner_radius=14)
        self.drop.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)
        self.drop.grid_columnconfigure(0, weight=1)
        self.drop.grid_rowconfigure(0, weight=1)
        self.drop.grid_rowconfigure(2, weight=1)
        self.drop_label = ctk.CTkLabel(
            self.drop, text="⤓  Drop Publisher files or a folder here",
            font=ctk.CTkFont(size=17))
        self.drop_label.grid(row=1, column=0)
        self.browse_btn = ctk.CTkButton(self.drop, text="Choose files…", width=140,
                                        fg_color=ACCENT, command=self._browse)
        self.browse_btn.grid(row=2, column=0, pady=(12, 0), sticky="n")

        # Scrollable file list (hidden until files land).
        self.list = ctk.CTkScrollableFrame(self, fg_color="#1c1c1c", corner_radius=14)
        self.list.grid_columnconfigure(0, weight=1)

        # Footer: output choice + summary + actions.
        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 16))
        self.footer.grid_columnconfigure(1, weight=1)

        self.out_btn = ctk.CTkButton(self.footer, text=self._out_label(), width=240,
                                     fg_color="#2a2a2a", border_width=1,
                                     border_color="#4a4a4a", hover_color="#333333",
                                     anchor="w", command=self._choose_output)
        self.out_btn.grid(row=0, column=0, sticky="w")
        self.summary = ctk.CTkLabel(self.footer, text="", text_color="#9aa0a6")
        self.summary.grid(row=1, column=0, columnspan=5, padx=2, pady=(8, 0), sticky="w")

        # Retry is the safest action on the screen — dress it as a secondary
        # action, never in the red it would share with the Failed chip.
        self.retry_btn = ctk.CTkButton(self.footer, text="Retry failed", width=120,
                                       fg_color="transparent", border_width=1,
                                       border_color=ACCENT, text_color="#cdd7ff",
                                       hover_color="#22304f", command=self._retry_failed)
        self.open_btn = ctk.CTkButton(self.footer, text="Open folder", width=120,
                                      fg_color="transparent", border_width=1,
                                      border_color="#3a3a3a", command=self._open_output)
        self.convert_btn = ctk.CTkButton(self.footer, text="Convert", width=140,
                                         fg_color=ACCENT, command=self._start)
        self.convert_btn.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self.convert_btn.configure(state="disabled")   # nothing to convert until files land

        # Register drop targets.
        for w in (self.drop, self.drop_label):
            w.drop_target_register(tkinterdnd2.DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)
            w.dnd_bind("<<DropEnter>>", lambda e: self._hl(True))
            w.dnd_bind("<<DropLeave>>", lambda e: self._hl(False))

    def _hl(self, on: bool) -> None:
        self.drop.configure(border_color=ACCENT if on else "#3a3a3a")

    def _out_label(self) -> str:
        where = self.output_dir.name if self.output_dir else "next to originals"
        return f"Save to: {where}   ▾"

    # ---- Publisher-missing startup screen -------------------------------
    def _show_no_publisher(self) -> None:
        self.drop_label.configure(
            text="Microsoft Publisher isn't installed on this PC.\n\n"
                 "Ask IT to install Microsoft Publisher, then reopen this app.",
            text_color="#ffc2c2")
        self.browse_btn.configure(state="disabled")
        self.convert_btn.configure(state="disabled")
        self.subtitle.configure(text="This tool converts using Publisher itself, "
                                     "so Publisher must be installed.")

    # ---- adding files ---------------------------------------------------
    def _on_drop(self, event) -> None:
        if not self.publisher or self.running:
            return
        paths = [Path(p) for p in self.splitlist(event.data)]
        self._hl(False)
        self._add(paths)

    def _browse(self) -> None:
        from tkinter import filedialog
        picked = filedialog.askopenfilenames(
            title="Choose Publisher files",
            filetypes=[("Publisher files", "*.pub"), ("All files", "*.*")])
        if picked:
            self._add([Path(p) for p in picked])

    def _add(self, paths: list[Path]) -> None:
        found: list[Path] = []
        for p in paths:
            if p.is_dir():
                found += pub2pdf.collect_pub_files(p, recurse=True)
            elif p.suffix.lower() == ".pub" and p.is_file():
                found.append(p)
        known = {r.pub_file.resolve() for r in self.rows}
        new = [p for p in found if p.resolve() not in known]
        if not new:
            return
        if not self.rows:
            self._reveal_list()
        for p in new:
            row = FileRow(self.list, p)
            row.grid(sticky="ew", padx=6, pady=2)
            self.rows.append(row)
        self._update_summary()

    def _reveal_list(self) -> None:
        # Swap the big empty drop zone for the compact list once files exist.
        self.drop.grid_remove()
        self.list.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)
        # Keep the list itself a drop target so more files can be added.
        self.list.drop_target_register(tkinterdnd2.DND_FILES)
        self.list.dnd_bind("<<Drop>>", self._on_drop)
        self.subtitle.configure(text="Drop more anytime. Convert writes PDFs and "
                                     "leaves your .pub files untouched.")

    # ---- output choice --------------------------------------------------
    def _choose_output(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Where should PDFs go?")
        self.output_dir = Path(d) if d else None
        self.settings["output_dir"] = str(self.output_dir) if self.output_dir else ""
        save_settings(self.settings)
        self.out_btn.configure(text=self._out_label())

    def _open_output(self) -> None:
        done = [r for r in self.rows if r.state == "done" and r.pdf_path]
        if done:
            reveal(done[-1].pdf_path)
        elif self.output_dir:
            reveal(self.output_dir)

    # ---- conversion (worker thread) -------------------------------------
    def _start(self) -> None:
        if self.running or not self.publisher:
            return
        todo = [r for r in self.rows if r.state in ("queued", "failed")]
        if not todo:
            return
        self._run(todo)

    def _retry_failed(self) -> None:
        self._run([r for r in self.rows if r.state == "failed"])

    def _run(self, rows: list[FileRow]) -> None:
        if not rows or self.running:
            return
        self.running = True
        self._set_busy(True)
        for r in rows:
            r.set_state("queued")
        thread = threading.Thread(target=self._worker, args=(rows,), daemon=True)
        thread.start()

    def _worker(self, rows: list[FileRow]) -> None:
        # Runs off the UI thread. All UI changes are posted back via self.events
        # and applied on the main thread in _drain_events.
        try:
            with pub2pdf.PublisherEngine() as engine:
                for r in rows:
                    self.events.put(("state", r, "converting", ""))
                    target_dir = self.output_dir or r.pub_file.parent
                    pdf = target_dir / (r.pub_file.stem + ".pdf")
                    try:
                        target_dir.mkdir(parents=True, exist_ok=True)
                        engine.convert(r.pub_file, pdf)
                        self.events.put(("done", r, str(pdf), ""))
                    except Exception as e:  # noqa: BLE001 — surface any COM error per file
                        self.events.put(("state", r, "failed", _short(e)))
        except Exception as e:  # noqa: BLE001 — engine failed to start at all
            self.events.put(("fatal", None, "", _short(e)))
        self.events.put(("finished", None, "", ""))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, row, a, b = self.events.get_nowait()
                if kind == "state":
                    row.set_state(a, b)
                elif kind == "done":
                    row.pdf_path = Path(a)
                    row.set_state("done")
                elif kind == "fatal":
                    self.subtitle.configure(
                        text=f"Publisher couldn't start: {b}", text_color="#ffc2c2")
                elif kind == "finished":
                    self.running = False
                    self._set_busy(False)
                self._update_summary()
        except queue.Empty:
            pass
        self._after_id = self.after(100, self._drain_events)

    def _set_busy(self, busy: bool) -> None:
        # Convert is re-enabled by _update_summary (only if there's work left),
        # so don't force it "normal" here.
        self.convert_btn.configure(text="Converting…" if busy else "Convert")
        if busy:
            self.convert_btn.configure(state="disabled")
        self.browse_btn.configure(state="disabled" if busy else "normal")

    def _update_summary(self) -> None:
        total = len(self.rows)
        done = sum(r.state == "done" for r in self.rows)
        failed = sum(r.state == "failed" for r in self.rows)
        parts = [f"{total} file{'s' if total != 1 else ''}"]
        if done:
            parts.append(f"{done} done")
        if failed:
            parts.append(f"{failed} failed")
        self.summary.configure(
            text="   ·   ".join(parts) if total else "",
            text_color="#ff8a8a" if failed else "#9aa0a6")

        # Convert is live only when there's something to do and Publisher is here.
        actionable = any(r.state in ("queued", "failed") for r in self.rows)
        if not self.running:
            self.convert_btn.configure(
                state="normal" if (actionable and self.publisher) else "disabled")

        # Show retry / open only when they'd do something.
        self.retry_btn.grid_forget()
        self.open_btn.grid_forget()
        col = 2
        if failed and not self.running:
            self.retry_btn.grid(row=0, column=col, padx=(0, 8)); col += 1
        if done and not self.running:
            self.open_btn.grid(row=0, column=col, padx=(0, 8))


def _short(exc: Exception) -> str:
    msg = str(exc).strip().splitlines()
    text = msg[0] if msg else exc.__class__.__name__
    return (text[:80] + "…") if len(text) > 80 else text


def main() -> None:
    if sys.platform != "win32":
        sys.exit("pub2pdf app runs on Windows (it drives Microsoft Publisher).")
    App().mainloop()


if __name__ == "__main__":
    main()
