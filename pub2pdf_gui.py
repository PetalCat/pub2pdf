#!/usr/bin/env python3
"""pub2pdf desktop app — drop Publisher files, or scan your drives, get faithful PDFs.

A thin GUI shell over the faithful Publisher COM engine in pub2pdf.py. The bar
for "done" is: a City clerk who has never seen it drops a file or a folder on the
window and gets PDFs next to the originals, without reading anything.

Design (spec by Janet, build by Glenn):
  - Drop zone is the primary surface; the file list appears only after files land.
  - Scan: a user-curated, persisted list of places (drives, folders, UNC paths) to
    look for .pub files. Empty on first run. Unreachable places are skipped, not
    fatal. Results stream in with a running count and a Stop.
  - Remembers what's been converted (path + size + modified-time), so a re-scan
    shows what's new vs already done; only new is ticked, done can be forced.
  - Per-file state you can read: Queued / Converting / Done / Failed(reason).
  - Failures survive the run — see which failed and retry just those.
  - Publisher missing = a plain sentence on the startup screen, never a crash.

Faithful engine only. There is deliberately no approximate fallback here — that
is a conscious CLI opt-in (pub2pdf.py --engine libreoffice), not something a user
gets by accident.
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import tkinterdnd2

import pub2pdf

APP_NAME = "pub2pdf"
ACCENT = "#2f6df6"
COL = {
    "pending": ("#2f2f2f", "#9a9a9a"),     # at rest, not yet converted (not "queued")
    "queued": ("#4a4a4a", "#e8e8e8"),      # in line for a run that's actually happening
    "converting": ("#8a5a00", "#ffdf9e"),
    "done": ("#1f5f37", "#b8f0c9"),
    "failed": ("#6e1f1f", "#ffc2c2"),
}
# Directory names never worth walking for a user's .pub files. Pruned at every
# level of a scan so "add all of C:" doesn't spend minutes in C:\Windows.
SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
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


def file_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def stat_sig(path: Path):
    """(size, mtime) for the done-history key, or None if it can't be read."""
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return None


def list_drives() -> list[tuple[str, str]]:
    """(root, kind) for each present drive: local disk / removable / network / CD."""
    kinds = {2: "Removable", 3: "Local disk", 4: "Network", 5: "CD/DVD"}
    out: list[tuple[str, str]] = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        for i in range(26):
            if bitmask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                out.append((root, kinds.get(get_type(ctypes.c_wchar_p(root)), "Drive")))
    except Exception:  # noqa: BLE001 — drive enumeration is best-effort
        pass
    return out


def iter_pub_files(root: str, cancel: threading.Event):
    """Yield .pub paths under root, pruning system dirs. Permission errors and
    unreadable subtrees are skipped, never fatal."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if cancel.is_set():
            return
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith("$")]
        for fn in filenames:
            if fn.lower().endswith(".pub"):
                yield Path(dirpath) / fn


class DnDTk(ctk.CTk, tkinterdnd2.TkinterDnD.DnDWrapper):
    """A customtkinter root that also speaks native drag-and-drop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = tkinterdnd2.TkinterDnD._require(self)


class FileRow(ctk.CTkFrame):
    """One row: a tick to include it, the filename, an inline note, a status chip."""

    def __init__(self, master, pub_file: Path, selected: bool, note: str = ""):
        super().__init__(master, fg_color="transparent")
        self.pub_file = pub_file
        self.pdf_path: Path | None = None
        # from_history: already converted on a prior day (shown, unticked). Distinct
        # from converted_now, which is set only when THIS session converts it — so
        # the summary can report this run's outcome without counting old history.
        self.from_history = bool(note)
        self.converted_now = False
        self.state = "done" if note else "pending"

        self.grid_columnconfigure(1, weight=1)
        self.sel = ctk.BooleanVar(value=selected)
        self.check = ctk.CTkCheckBox(self, text="", width=24, variable=self.sel)
        self.check.grid(row=0, column=0, padx=(10, 4), pady=4)
        self.name = ctk.CTkLabel(self, text=pub_file.name, anchor="w", text_color="#dcdcdc")
        self.name.grid(row=0, column=1, sticky="ew", padx=(2, 8), pady=6)
        self.detail = ctk.CTkLabel(self, text=note, anchor="e", text_color="#8b8b8b")
        self.detail.grid(row=0, column=2, sticky="e", padx=(0, 8))
        chip_state = "done" if note else "pending"
        self.chip = ctk.CTkLabel(self, text="Done" if note else "Not converted", width=110,
                                 corner_radius=8, fg_color=COL[chip_state][0],
                                 text_color=COL[chip_state][1])
        self.chip.grid(row=0, column=3, padx=(0, 10), pady=6)

    @property
    def selected(self) -> bool:
        return bool(self.sel.get())

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        label = {"pending": "Not converted", "queued": "Queued",
                 "converting": "Converting…", "done": "Done", "failed": "Failed"}[state]
        bg, fg = COL[state]
        self.chip.configure(text=label, fg_color=bg, text_color=fg)
        # The chip carries the state — one colour, one place. Filename stays neutral.
        # A failure adds its reason inline (muted red); success unticks the row.
        if state == "failed" and detail:
            self.detail.configure(text=detail, text_color="#b98a8a")
        elif state == "done":
            self.detail.configure(text=detail or "converted", text_color="#8b8b8b")
            self.sel.set(False)
        else:
            self.detail.configure(text="", text_color="#8b8b8b")


class ScopeDialog(ctk.CTkToplevel):
    """Manage the persisted 'where to look' list, then start a scan."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("Where to look for .pub files")
        self.geometry("560x420")
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Scan these places", anchor="w",
                     font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self.rows_frame = ctk.CTkScrollableFrame(self, fg_color="#1c1c1c")
        self.rows_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=6)
        self.rows_frame.grid_columnconfigure(1, weight=1)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 4))
        bar.grid_columnconfigure(3, weight=1)
        ctk.CTkButton(bar, text="Add folder or drive…", command=self._add_folder,
                      fg_color="#2a2a2a", border_width=1, border_color="#4a4a4a",
                      hover_color="#333").grid(row=0, column=0)
        ctk.CTkButton(bar, text="Add network path…", command=self._add_unc,
                      fg_color="#2a2a2a", border_width=1, border_color="#4a4a4a",
                      hover_color="#333").grid(row=0, column=1, padx=(8, 0))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 14))
        foot.grid_columnconfigure(0, weight=1)
        self.count_lbl = ctk.CTkLabel(foot, text="", text_color="#9aa0a6")
        self.count_lbl.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(foot, text="Close", width=90, command=self.destroy,
                      fg_color="transparent", border_width=1,
                      border_color="#3a3a3a").grid(row=0, column=1, padx=(0, 8))
        self.scan_btn = ctk.CTkButton(foot, text="Scan", width=120, fg_color=ACCENT,
                                      command=self._scan)
        self.scan_btn.grid(row=0, column=2)
        self._render()

    def _render(self) -> None:
        for w in self.rows_frame.winfo_children():
            w.destroy()
        locs = self.app.scan_locations
        if not locs:
            ctk.CTkLabel(self.rows_frame, text="Add a folder or drive to scan.",
                         text_color="#7f7f7f").grid(row=0, column=1, sticky="w",
                                                    padx=8, pady=14)
        for i, loc in enumerate(locs):
            var = ctk.BooleanVar(value=loc.get("enabled", True))
            def _toggle(idx=i, v=var):
                self.app.scan_locations[idx]["enabled"] = bool(v.get())
                self.app._save_scope(); self._update_count()
            ctk.CTkCheckBox(self.rows_frame, text="", width=24, variable=var,
                            command=_toggle).grid(row=i, column=0, padx=(6, 2), pady=3)
            avail = "" if os.path.isdir(loc["path"]) else "   (unavailable)"
            ctk.CTkLabel(self.rows_frame, text=loc["path"] + avail, anchor="w",
                         text_color="#dcdcdc" if not avail else "#b98a8a").grid(
                row=i, column=1, sticky="ew", padx=4, pady=3)
            ctk.CTkButton(self.rows_frame, text="✕", width=28, fg_color="transparent",
                          hover_color="#3a2a2a", text_color="#c98a8a",
                          command=lambda idx=i: self._remove(idx)).grid(
                row=i, column=2, padx=(2, 6))
        self._update_count()

    def _update_count(self) -> None:
        # Count only what will actually be scanned — a ticked-but-unreachable
        # location is NOT in the promise. The preflight line's whole job is to
        # tell the truth before you commit.
        enabled = [l for l in self.app.scan_locations if l.get("enabled", True)]
        reachable = [l for l in enabled if os.path.isdir(l["path"])]
        n, unavailable = len(reachable), len(enabled) - len(reachable)
        txt = f"Will scan {n} location{'s' if n != 1 else ''}"
        if unavailable:
            txt += f"   ·   {unavailable} unavailable"
        self.count_lbl.configure(text=txt)
        self.scan_btn.configure(state="normal" if n else "disabled")

    def _add_path(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
        # normpath keeps a drive root's backslash (C:/ -> C:\) and a UNC's double
        # backslash, while dropping trailing separators — the rstrip hack collapsed
        # "C:\" to "C:", which means current-dir-on-C, not the drive root.
        path = os.path.normpath(path)
        keys = {os.path.normcase(l["path"]) for l in self.app.scan_locations}
        if os.path.normcase(path) in keys:
            return
        self.app.scan_locations.append({"path": path, "enabled": True})
        self.app._save_scope()
        self._render()

    def _add_folder(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Add a folder or drive to scan", parent=self)
        if d:
            self._add_path(d)

    def _add_unc(self) -> None:
        dlg = ctk.CTkInputDialog(text="Network path (e.g. \\\\server\\share\\folder):",
                                 title="Add network path")
        val = dlg.get_input()
        if val:
            self._add_path(val)

    def _remove(self, idx: int) -> None:
        del self.app.scan_locations[idx]
        self.app._save_scope()
        self._render()

    def _scan(self) -> None:
        enabled = [l["path"] for l in self.app.scan_locations if l.get("enabled", True)]
        if enabled:
            self.destroy()
            self.app._start_scan(enabled)


class App(DnDTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(f"{APP_NAME} — Publisher to PDF")
        self.geometry("760x600")
        self.minsize(720, 480)
        self._center()

        self.settings = load_settings()
        self.output_dir: Path | None = (
            Path(self.settings["output_dir"]) if self.settings.get("output_dir") else None
        )
        self.history: dict = self.settings.get("history", {})
        self.scan_locations: list[dict] = self.settings.get("scan_locations", [])

        self.rows: list[FileRow] = []
        self.events: queue.Queue = queue.Queue()
        self.running = False          # a conversion is in progress
        self.scanning = False         # a scan is in progress
        self.cancel = threading.Event()
        self.scan_cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.filter_failed = False
        self._filtered = False

        self.publisher = pub2pdf.publisher_exe()
        self._build()
        if not self.publisher:
            self._show_no_publisher()
        self._after_id = self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        # Signal both workers and give the converter a bounded moment to fall out
        # of the `with PublisherEngine()` block so __exit__ runs Quit() and we don't
        # orphan MSPUB.EXE. A wedged call can't hang the close — we destroy anyway.
        self.cancel.set()
        self.scan_cancel.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
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
        # Scan is always reachable, in both the empty and the populated state.
        self.scan_btn = ctk.CTkButton(header, text="Scan for .pub files…", width=170,
                                       fg_color="#2a2a2a", border_width=1,
                                       border_color="#4a4a4a", hover_color="#333",
                                       command=self._open_scope)
        self.scan_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        # Drop zone — the primary surface.
        self.drop = ctk.CTkFrame(self, fg_color="#242424", border_width=2,
                                 border_color="#3a3a3a", corner_radius=14)
        self.drop.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)
        self.drop.grid_columnconfigure(0, weight=1)
        self.drop.grid_rowconfigure(0, weight=1)
        self.drop.grid_rowconfigure(3, weight=1)
        self.drop_label = ctk.CTkLabel(
            self.drop, text="⤓  Drop Publisher files or a folder here",
            font=ctk.CTkFont(size=17))
        self.drop_label.grid(row=1, column=0)
        self.browse_btn = ctk.CTkButton(self.drop, text="Choose files…", width=140,
                                        fg_color=ACCENT, command=self._browse)
        self.browse_btn.grid(row=2, column=0, pady=(12, 0), sticky="n")

        self.list = ctk.CTkScrollableFrame(self, fg_color="#1c1c1c", corner_radius=14)
        self.list.grid_columnconfigure(0, weight=1)

        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 16))
        self.footer.grid_columnconfigure(1, weight=1)

        status = ctk.CTkFrame(self.footer, fg_color="transparent")
        status.grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 8))
        self.summary = ctk.CTkLabel(status, text="", text_color="#9aa0a6")
        self.summary.pack(side="left")
        self.failed_lbl = ctk.CTkLabel(status, text="", text_color="#ff8a8a", cursor="hand2")
        self.failed_lbl.pack(side="left", padx=(6, 0))
        self.failed_lbl.bind("<Button-1>", lambda e: self._toggle_failed_filter())

        self.out_btn = ctk.CTkButton(self.footer, text=self._out_label(), width=240,
                                     fg_color="#2a2a2a", border_width=1,
                                     border_color="#4a4a4a", hover_color="#333333",
                                     anchor="w", command=self._choose_output)
        self.out_btn.grid(row=1, column=0, sticky="w")

        self.retry_btn = ctk.CTkButton(self.footer, text="Retry failed", width=120,
                                       fg_color="transparent", border_width=1,
                                       border_color=ACCENT, text_color="#cdd7ff",
                                       hover_color="#22304f", command=self._retry_failed)
        self.open_btn = ctk.CTkButton(self.footer, text="Open folder", width=120,
                                      fg_color="transparent", border_width=1,
                                      border_color="#3a3a3a", command=self._open_output)
        self.convert_btn = ctk.CTkButton(self.footer, text="Convert", width=140,
                                         fg_color=ACCENT, command=self._start)
        self.convert_btn.grid(row=1, column=4, sticky="e", padx=(8, 0))
        self.convert_btn.configure(state="disabled")
        self.stop_btn = ctk.CTkButton(self.footer, text="Stop", width=100,
                                      fg_color="transparent", border_width=1,
                                      border_color="#8a5a00", text_color="#ffdf9e",
                                      hover_color="#3a2a00", command=self._stop)

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

    def _show_no_publisher(self) -> None:
        self.drop_label.configure(
            text="Microsoft Publisher isn't installed on this PC.\n\n"
                 "Ask IT to install Microsoft Publisher, then reopen this app.",
            text_color="#ffc2c2")
        self.browse_btn.configure(state="disabled")
        self.convert_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.subtitle.configure(text="This tool converts using Publisher itself, "
                                     "so Publisher must be installed.")

    # ---- scope persistence ---------------------------------------------
    def _save_scope(self) -> None:
        self.settings["scan_locations"] = self.scan_locations
        save_settings(self.settings)

    def _open_scope(self) -> None:
        if not self.publisher or self.scanning or self.running:
            return
        ScopeDialog(self).focus()

    # ---- done-history ---------------------------------------------------
    def _history_note(self, path: Path) -> str:
        """If this file matches a prior conversion (path+size+mtime), the display
        note ('converted <date>'), else '' meaning it's new / changed."""
        rec = self.history.get(file_key(path))
        sig = stat_sig(path)
        if rec and sig and rec.get("size") == sig[0] and abs(rec.get("mtime", 0) - sig[1]) < 2:
            return f"converted {rec.get('done_at', '')[:10]}".rstrip()
        return ""

    def _record_done(self, path: Path) -> None:
        sig = stat_sig(path)
        if sig:
            self.history[file_key(path)] = {
                "size": sig[0], "mtime": sig[1],
                "done_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self.settings["history"] = self.history
            save_settings(self.settings)

    # ---- adding files ---------------------------------------------------
    def _on_drop(self, event) -> None:
        if not self.publisher or self.running:
            return
        paths = [Path(p) for p in self.splitlist(event.data)]
        self._hl(False)
        collected: list[Path] = []
        for p in paths:
            if p.is_dir():
                collected += pub2pdf.collect_pub_files(p, recurse=True)
            elif p.suffix.lower() == ".pub" and p.is_file():
                collected.append(p)
        self._add(collected)

    def _browse(self) -> None:
        from tkinter import filedialog
        picked = filedialog.askopenfilenames(
            title="Choose Publisher files",
            filetypes=[("Publisher files", "*.pub"), ("All files", "*.*")])
        if picked:
            self._add([Path(p) for p in picked])

    def _add(self, paths: list[Path]) -> int:
        """Add files as rows, dedupe, apply done-history. Returns count added."""
        known = {file_key(r.pub_file) for r in self.rows}
        added = 0
        for p in paths:
            k = file_key(p)
            if k in known:
                continue
            known.add(k)
            if not self.rows:
                self._reveal_list()
            note = self._history_note(p)          # '' = new, else 'converted <date>'
            row = FileRow(self.list, p, selected=(note == ""), note=note)
            row.grid(sticky="ew", padx=6, pady=2)
            self.rows.append(row)
            added += 1
        if added:
            self._update_summary()
        return added

    def _reveal_list(self) -> None:
        self.drop.grid_remove()
        self.list.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)
        self.list.drop_target_register(tkinterdnd2.DND_FILES)
        self.list.dnd_bind("<<Drop>>", self._on_drop)
        self.subtitle.configure(text="Ticked files convert. New files are ticked; "
                                     "already-converted ones aren't (tick to redo).")

    # ---- scanning (worker thread) --------------------------------------
    def _start_scan(self, roots: list[str]) -> None:
        if self.scanning or self.running or not self.publisher:
            return
        self.scanning = True
        self.scan_cancel.clear()
        self._set_busy(True, scanning=True)
        self.subtitle.configure(text=f"Scanning {len(roots)} location"
                                     f"{'s' if len(roots) != 1 else ''}…",
                                text_color="#9aa0a6")
        self.worker = threading.Thread(target=self._scan_worker, args=(roots,), daemon=True)
        self.worker.start()

    def _scan_worker(self, roots: list[str]) -> None:
        found = 0
        for root in roots:
            if self.scan_cancel.is_set():
                break
            try:
                reachable = os.path.isdir(root)
            except OSError:
                reachable = False
            if not reachable:
                self.events.put(("scan_note", None, root, "unavailable"))
                continue
            self.events.put(("scan_note", None, root, "scanning"))
            try:
                for pub in iter_pub_files(root, self.scan_cancel):
                    if self.scan_cancel.is_set():
                        break
                    found += 1
                    self.events.put(("scan_found", None, str(pub), found))
            except Exception:  # noqa: BLE001 — a bad subtree shouldn't kill the scan
                pass
        self.events.put(("scan_done", None, "", found))

    def _stop_scan(self) -> None:
        self.scan_cancel.set()
        self.stop_btn.configure(text="Stopping…", state="disabled")

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
    def _convertible(self) -> list[FileRow]:
        # Ticked rows that aren't already converting. A ticked done row re-converts.
        return [r for r in self.rows if r.selected and r.state != "converting"]

    def _start(self) -> None:
        if self.running or self.scanning or not self.publisher:
            return
        self._run(self._convertible())

    def _retry_failed(self) -> None:
        self._run([r for r in self.rows if r.state == "failed"])

    def _run(self, rows: list[FileRow]) -> None:
        if not rows or self.running or self.scanning:
            return
        self.running = True
        self.cancel.clear()
        self._set_busy(True)
        for r in rows:
            r.set_state("queued")
        self.worker = threading.Thread(target=self._worker, args=(rows,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.scanning:
            self._stop_scan()
        else:
            self.cancel.set()
            self.stop_btn.configure(text="Stopping…", state="disabled")

    def _worker(self, rows: list[FileRow]) -> None:
        try:
            with pub2pdf.PublisherEngine() as engine:
                for r in rows:
                    if self.cancel.is_set():
                        break
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
        dirty = False
        try:
            while True:
                kind, row, a, b = self.events.get_nowait()
                dirty = True
                if kind == "state":
                    row.set_state(a, b)
                elif kind == "done":
                    row.pdf_path = Path(a)
                    row.converted_now = True
                    row.from_history = False
                    row.set_state("done", "converted just now")
                    self._record_done(row.pub_file)
                elif kind == "fatal":
                    self.subtitle.configure(
                        text=f"Publisher couldn't start: {b}", text_color="#ffc2c2")
                elif kind == "finished":
                    self.running = False
                    self._set_busy(False)
                elif kind == "scan_found":
                    self._add([Path(a)])
                    self.subtitle.configure(text=f"Scanning… {b} found", text_color="#9aa0a6")
                elif kind == "scan_note":
                    if b == "unavailable":
                        self.subtitle.configure(
                            text=f"Skipped (unavailable): {a}", text_color="#b98a8a")
                elif kind == "scan_done":
                    self.scanning = False
                    self._set_busy(False)
                    self.subtitle.configure(
                        text=f"Scan complete — {b} .pub file{'s' if b != 1 else ''} found."
                             if b else "Scan complete — no .pub files found.",
                        text_color="#9aa0a6")
        except queue.Empty:
            pass
        if dirty:
            self._update_summary()
        self._after_id = self.after(100, self._drain_events)

    def _set_busy(self, busy: bool, scanning: bool = False) -> None:
        self.convert_btn.configure(text="Converting…" if (busy and not scanning) else "Convert")
        self.scan_btn.configure(state="disabled" if busy else "normal")
        self.browse_btn.configure(state="disabled" if busy else "normal")
        if busy:
            self.convert_btn.configure(state="disabled")
            self.stop_btn.configure(text="Stop", state="normal")
            self.stop_btn.grid(row=1, column=3, padx=(0, 8))
        else:
            self.stop_btn.grid_forget()

    def _update_summary(self) -> None:
        total = len(self.rows)
        selected = sum(r.selected and r.state != "converting" for r in self.rows)
        converted = sum(r.converted_now for r in self.rows)   # THIS run, not history
        failed = sum(r.state == "failed" for r in self.rows)

        # One axis at a time: selection (files / selected), then this-run outcome
        # (converted). History-done rows carry their date inline, not in this line,
        # and failures ride the separate red label — so nothing double-counts and
        # the line never mixes "already done before" with "just did".
        parts = [f"{total} file{'s' if total != 1 else ''}"]
        if selected:
            parts.append(f"{selected} selected")
        if converted:
            parts.append(f"{converted} converted")
        self.summary.configure(text="   ·   ".join(parts) if total else "")

        if self.filter_failed and not failed:
            self.filter_failed = False
        self.failed_lbl.configure(
            text=(f"·   {failed} failed" + ("   (show all)" if self.filter_failed else ""))
            if failed else "")

        if not self.running and not self.scanning:
            self.convert_btn.configure(
                state="normal" if (selected and self.publisher) else "disabled")

        self.retry_btn.grid_forget()
        self.open_btn.grid_forget()
        col = 2
        if failed and not self.running and not self.scanning:
            self.retry_btn.grid(row=1, column=col, padx=(0, 8)); col += 1
        if converted and not self.running and not self.scanning:
            self.open_btn.grid(row=1, column=col, padx=(0, 8))

        self._apply_filter()

    def _toggle_failed_filter(self) -> None:
        if any(r.state == "failed" for r in self.rows):
            self.filter_failed = not self.filter_failed
            self._update_summary()

    def _apply_filter(self) -> None:
        if self.filter_failed:
            for r in self.rows:
                (r.grid_remove if r.state != "failed" else r.grid)()
            self._filtered = True
        elif self._filtered:
            for r in self.rows:
                r.grid()
            self._filtered = False


def _short(exc: Exception) -> str:
    """Turn an engine/COM exception into a sentence a clerk can act on."""
    args = getattr(exc, "args", ())
    desc, codes = "", []
    if len(args) >= 3 and isinstance(args[2], (tuple, list)):
        info = args[2]
        if len(info) > 2 and info[2]:
            desc = str(info[2]).strip()
        if len(info) > 5 and isinstance(info[5], int):
            codes.append(info[5] & 0xFFFFFFFF)
    if args and isinstance(args[0], int):
        codes.append(args[0] & 0xFFFFFFFF)

    if desc:
        low = desc.lower()
        if any(s in low for s in ("another process", "being used", "sharing violation", "in use")):
            return "This PDF is open in another program — close it and retry."
        if "denied" in low or "permission" in low:
            return "Can't write to that folder — check its permissions."
        if "password" in low or "protected" in low:
            return "This .pub is password-protected — open it in Publisher first."
        if "cannot locate" in low or "not found" in low:
            return "The .pub file was moved or deleted before it could convert."
        return (desc[:90] + "…") if len(desc) > 90 else desc

    for code in codes:
        if (code & 0xFFFF0000) == 0x80070000:
            win32 = code & 0xFFFF
            if win32 in (32, 33):
                return "This PDF is open in another program — close it and retry."
            if win32 == 5:
                return "Can't write to that folder — check its permissions."
            if win32 in (2, 3):
                return "The .pub file or folder was moved or deleted."

    return ("Couldn't save the PDF — it may be open in another program, or the "
            "folder may be read-only. Close any open copy and retry.")


def main() -> None:
    if sys.platform != "win32":
        sys.exit("pub2pdf app runs on Windows (it drives Microsoft Publisher).")
    App().mainloop()


if __name__ == "__main__":
    main()
