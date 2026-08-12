#!/usr/bin/env python3
"""pub2pdf desktop app — drop Publisher files, or scan your drives, get faithful PDFs.

A thin GUI shell over the faithful Publisher COM engine in pub2pdf.py. The bar
for "done" is: a City clerk who has never seen it drops a file or a folder on the
window and gets PDFs next to the originals, without reading anything.

UI craft (Eli's lab bar, adapted to customtkinter):
  - Follows the Windows light/dark theme; every colour is a (light, dark) pair.
  - Contrast measured: body/secondary/muted text ≥7:1 (AAA) both themes; saturated
    chip/button text ≥4.5:1 (AA-large, the reasonable ease for coloured labels).
  - 8pt spacing grid, small 6px radius, hairline surfaces (no chunky borders),
    monoline icons (no emoji), minimal copy. Motion deliberately skipped — in
    tkinter it is a fight for little, so state changes are instant.
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
from pub2pdf_icons import icon

APP_NAME = "pub2pdf"

# Palette — (light, dark) so the app follows the Windows appearance mode.
BG        = ("#eef1f5", "#0f1216")
SURFACE   = ("#ffffff", "#161a20")
SURFACE2  = ("#e7ebf1", "#1e232b")
HAIRLINE  = ("#d3dae3", "#2a313b")
TEXT      = ("#0e1319", "#f3f6fa")
TEXT2     = ("#39424e", "#c2ccd8")
TEXT3     = ("#4f5866", "#9ba8b7")
ACCENT    = ("#1f5fe0", "#2a63d6")      # primary fill, white text
ACCENT_TX = ("#1f5fe0", "#7aa7ff")      # accent as text / hairline
ON_ACCENT = "#ffffff"
REASON    = ("#b3121f", "#ff9ba0")      # failure reason text on a surface
WARN_TX   = ("#b45309", "#ffcf7a")      # amber accent (Stop) on a surface
RADIUS = 6

# Status chips: solid semantic fills, IDENTICAL in both themes so a failure reads
# equally urgent day and night; pending/queued stay quiet and theme-aware.
COL = {                                  # (bg, fg)
    "pending":    (("#e7ebf1", "#242a33"),  ("#5c6675", "#9ba8b7")),
    "queued":     (("#d7dde5", "#2b323c"),  TEXT2),
    "converting": ("#e0870c", "#241700"),
    "done":       ("#1a8043", "#ffffff"),
    "failed":     ("#c62330", "#ffffff"),
}
CHIP_LABEL = {"pending": "Not converted", "queued": "Queued",
              "converting": "Converting", "done": "Done", "failed": "Failed"}

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
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return None


def iter_pub_candidates(root: str, cancel: threading.Event):
    """Yield (path, status) for every .pub under root, status one of
    publisher/other/unreadable, pruning system dirs. The caller keeps the real
    Publisher files and counts 'other' (mostly SSH keys) and 'unreadable'
    (permissions) separately. Permission errors on directories are skipped."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if cancel.is_set():
            return
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith("$")]
        for fn in filenames:
            if fn.lower().endswith(pub2pdf.PUB_EXTS):
                p = Path(dirpath) / fn
                yield p, pub2pdf.classify_pub(p)


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
        self.from_history = bool(note)
        self.converted_now = False
        self.state = "done" if note else "pending"

        self.grid_columnconfigure(1, weight=1)
        self.sel = ctk.BooleanVar(value=selected)
        self.check = ctk.CTkCheckBox(self, text="", width=22, checkbox_width=20,
                                     checkbox_height=20, corner_radius=RADIUS,
                                     fg_color=ACCENT, hover_color=ACCENT,
                                     border_color=HAIRLINE, border_width=2, variable=self.sel)
        self.check.grid(row=0, column=0, padx=(16, 8), pady=8)
        self.name = ctk.CTkLabel(self, text=pub_file.name, anchor="w", text_color=TEXT)
        self.name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=8)
        self.detail = ctk.CTkLabel(self, text=note, anchor="e", text_color=TEXT3)
        self.detail.grid(row=0, column=2, sticky="e", padx=(0, 8))
        cs = "done" if note else "pending"
        self.chip = ctk.CTkLabel(self, text=CHIP_LABEL[cs], width=112, height=26,
                                 corner_radius=RADIUS, fg_color=COL[cs][0], text_color=COL[cs][1])
        self.chip.grid(row=0, column=3, padx=(0, 16), pady=8)

    @property
    def selected(self) -> bool:
        return bool(self.sel.get())

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        bg, fg = COL[state]
        self.chip.configure(text=CHIP_LABEL[state], fg_color=bg, text_color=fg)
        if state == "failed" and detail:
            self.detail.configure(text=detail, text_color=REASON)
        elif state == "done":
            self.detail.configure(text=detail or "converted", text_color=TEXT3)
            self.sel.set(False)
        else:
            self.detail.configure(text="", text_color=TEXT3)


def _ghost_button(master, text, command, width=120, icon_name=None, danger=False):
    """A quiet secondary button — hairline outline, no fill, theme-aware."""
    kw = dict(width=width, height=34, corner_radius=RADIUS, fg_color="transparent",
              border_width=1, border_color=HAIRLINE, text_color=TEXT2,
              hover_color=SURFACE2, command=command)
    if danger:
        kw.update(border_color=("#e4b6b8", "#5a2a2e"), text_color=("#a01722", "#ffa8ad"))
    b = ctk.CTkButton(master, text=text, **kw)
    if icon_name:
        b.configure(image=icon(icon_name, 16, light=TEXT2[0], dark=TEXT2[1]), compound="left")
    return b


class ScopeDialog(ctk.CTkToplevel):
    """Manage the persisted 'where to look' list, then start a scan."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.configure(fg_color=BG)
        self.title("Where to look")
        self.geometry("580x440")
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Scan these places", anchor="w", text_color=TEXT,
                     font=ctk.CTkFont(size=17, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=24, pady=(20, 4))
        self.rows_frame = ctk.CTkScrollableFrame(self, fg_color=SURFACE, corner_radius=RADIUS)
        self.rows_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self.rows_frame.grid_columnconfigure(1, weight=1)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=24, pady=(4, 4))
        _ghost_button(bar, "Add folder or drive", self._add_folder, width=180,
                      icon_name="folder").grid(row=0, column=0)
        _ghost_button(bar, "Add network path", self._add_unc, width=170,
                      icon_name="search").grid(row=0, column=1, padx=(8, 0))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=24, pady=(8, 20))
        foot.grid_columnconfigure(0, weight=1)
        self.count_lbl = ctk.CTkLabel(foot, text="", text_color=TEXT2, anchor="w")
        self.count_lbl.grid(row=0, column=0, sticky="w")
        _ghost_button(foot, "Close", self.destroy, width=90).grid(row=0, column=1, padx=(0, 8))
        self.scan_btn = ctk.CTkButton(foot, text="Scan", width=120, height=34,
                                      corner_radius=RADIUS, fg_color=ACCENT,
                                      hover_color=ACCENT_TX, text_color=ON_ACCENT,
                                      command=self._scan)
        self.scan_btn.grid(row=0, column=2)
        self._render()

    def _render(self) -> None:
        for w in self.rows_frame.winfo_children():
            w.destroy()
        locs = self.app.scan_locations
        if not locs:
            ctk.CTkLabel(self.rows_frame, text="Add a folder or drive to scan.",
                         text_color=TEXT3).grid(row=0, column=1, sticky="w", padx=8, pady=16)
        for i, loc in enumerate(locs):
            var = ctk.BooleanVar(value=loc.get("enabled", True))
            def _toggle(idx=i, v=var):
                self.app.scan_locations[idx]["enabled"] = bool(v.get())
                self.app._save_scope(); self._update_count()
            ctk.CTkCheckBox(self.rows_frame, text="", width=22, checkbox_width=20,
                            checkbox_height=20, corner_radius=RADIUS, fg_color=ACCENT,
                            hover_color=ACCENT, border_color=HAIRLINE, border_width=2,
                            variable=var, command=_toggle).grid(row=i, column=0, padx=(10, 6), pady=4)
            gone = not os.path.isdir(loc["path"])
            ctk.CTkLabel(self.rows_frame, text=loc["path"] + ("   unavailable" if gone else ""),
                         anchor="w", text_color=(COL["failed"][1] if gone else TEXT)).grid(
                row=i, column=1, sticky="ew", padx=4, pady=4)
            ctk.CTkButton(self.rows_frame, text="", width=30, height=30, corner_radius=RADIUS,
                          fg_color="transparent", hover_color=SURFACE2,
                          image=icon("x", 15, light="#a01722", dark="#ffa8ad"),
                          command=lambda idx=i: self._remove(idx)).grid(row=i, column=2, padx=(2, 8))
        self._update_count()

    def _update_count(self) -> None:
        enabled = [l for l in self.app.scan_locations if l.get("enabled", True)]
        reachable = [l for l in enabled if os.path.isdir(l["path"])]
        n, un = len(reachable), len(enabled) - len(reachable)
        txt = f"Will scan {n} location{'s' if n != 1 else ''}"
        if un:
            txt += f"   ·   {un} unavailable"
        self.count_lbl.configure(text=txt)
        self.scan_btn.configure(state="normal" if n else "disabled")

    def _add_path(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
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
        ctk.set_appearance_mode("System")   # follow the Windows light/dark theme
        self.configure(fg_color=BG)
        self.title(f"{APP_NAME} — Publisher to PDF")
        self.geometry("780x620")
        self.minsize(720, 480)
        self._center()
        self._set_window_icon()

        self.settings = load_settings()
        self.output_dir: Path | None = (
            Path(self.settings["output_dir"]) if self.settings.get("output_dir") else None
        )
        self.history: dict = self.settings.get("history", {})
        self.scan_locations: list[dict] = self.settings.get("scan_locations", [])

        self.rows: list[FileRow] = []
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.scanning = False
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
        self.cancel.set()
        self.scan_cancel.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        try:
            self.after_cancel(self._after_id)
        except Exception:  # noqa: BLE001 — teardown, swallow Tcl noise
            pass
        self.destroy()

    def _center(self) -> None:
        self.update_idletasks()
        w, h = 780, 620
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _set_window_icon(self) -> None:
        # Prefer the multi-size .ico (bundled beside the exe / script) so the
        # titlebar and taskbar match the Desktop icon; fall back to a runtime PIL
        # render so the window is never left with the default Tk feather.
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "app.ico")
        try:
            if os.path.isfile(ico):
                self.iconbitmap(ico)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            import pub2pdf_icons
            self._logo_img = pub2pdf_icons.logo_photo(64)   # keep a reference alive
            self.iconphoto(True, self._logo_img)
        except Exception:  # noqa: BLE001
            pass

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 8))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Publisher to PDF", anchor="w", text_color=TEXT,
                     font=ctk.CTkFont(size=23, weight="bold")).grid(row=0, column=0, sticky="w")
        self.subtitle = ctk.CTkLabel(
            header, text="Convert Publisher files to PDF. Your originals stay untouched.",
            anchor="w", text_color=TEXT2)
        self.subtitle.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.scan_btn = ctk.CTkButton(
            header, text="Scan for files", width=150, height=36, corner_radius=RADIUS,
            fg_color=SURFACE2, hover_color=HAIRLINE, text_color=TEXT,
            image=icon("search", 16, light=TEXT[0], dark=TEXT[1]), compound="left",
            command=self._open_scope)
        self.scan_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        # Drop zone — the primary surface. One M3-outlined tile (the allowed exception).
        self.drop = ctk.CTkFrame(self, fg_color=SURFACE, border_width=1,
                                 border_color=HAIRLINE, corner_radius=RADIUS)
        self.drop.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self.drop.grid_columnconfigure(0, weight=1)
        self.drop.grid_rowconfigure(0, weight=1)
        self.drop.grid_rowconfigure(4, weight=1)
        self.drop_icon = ctk.CTkLabel(self.drop, text="",
                                      image=icon("download", 40, light=TEXT3[0], dark=TEXT3[1]))
        self.drop_icon.grid(row=1, column=0, pady=(0, 8))
        self.drop_label = ctk.CTkLabel(self.drop, text="Drop Publisher files or a folder",
                                       text_color=TEXT, font=ctk.CTkFont(size=16))
        self.drop_label.grid(row=2, column=0)
        self.browse_btn = ctk.CTkButton(self.drop, text="Choose files", width=150, height=36,
                                        corner_radius=RADIUS, fg_color=ACCENT,
                                        hover_color=ACCENT_TX, text_color=ON_ACCENT,
                                        command=self._browse)
        self.browse_btn.grid(row=3, column=0, pady=(16, 0), sticky="n")

        self.list = ctk.CTkScrollableFrame(self, fg_color=SURFACE, corner_radius=RADIUS)
        self.list.grid_columnconfigure(0, weight=1)

        self.footer = ctk.CTkFrame(self, fg_color="transparent")
        self.footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 20))
        self.footer.grid_columnconfigure(1, weight=1)

        status = ctk.CTkFrame(self.footer, fg_color="transparent")
        status.grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))
        self.summary = ctk.CTkLabel(status, text="", text_color=TEXT2)
        self.summary.pack(side="left")
        self.failed_lbl = ctk.CTkLabel(status, text="", text_color=REASON, cursor="hand2")
        self.failed_lbl.pack(side="left", padx=(8, 0))
        self.failed_lbl.bind("<Button-1>", lambda e: self._toggle_failed_filter())

        self.out_menu = ctk.CTkOptionMenu(
            self.footer, width=260, height=34, corner_radius=RADIUS, anchor="w",
            values=["Next to each original", "Choose a folder…"],
            command=self._on_output_choice, font=ctk.CTkFont(size=13),
            fg_color=SURFACE2, button_color=SURFACE2, button_hover_color=HAIRLINE,
            text_color=TEXT, dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE2,
            dropdown_text_color=TEXT)
        self.out_menu.grid(row=1, column=0, sticky="w")
        self.out_menu.set(self._out_display())

        # Retry is the safe action — a rotate arrow, never a hazard glyph.
        self.retry_btn = _ghost_button(self.footer, "Retry failed", self._retry_failed, width=120)
        self.retry_btn.configure(text_color=ACCENT_TX, border_color=ACCENT_TX, compound="left",
                                 image=icon("refresh", 16, light=ACCENT_TX[0], dark=ACCENT_TX[1]))
        self.open_btn = _ghost_button(self.footer, "Open folder", self._open_output,
                                      width=120, icon_name="folder")
        self.convert_btn = ctk.CTkButton(self.footer, text="Convert", width=150, height=36,
                                         corner_radius=RADIUS, fg_color=ACCENT,
                                         hover_color=ACCENT_TX, text_color=ON_ACCENT,
                                         command=self._start)
        self.convert_btn.grid(row=1, column=4, sticky="e", padx=(8, 0))
        self.convert_btn.configure(state="disabled")
        self.stop_btn = ctk.CTkButton(self.footer, text="Stop", width=100, height=36,
                                      corner_radius=RADIUS, fg_color="transparent",
                                      border_width=1, border_color=WARN_TX,
                                      text_color=WARN_TX, hover_color=SURFACE2,
                                      command=self._stop)

        for w in (self.drop, self.drop_label, self.drop_icon):
            w.drop_target_register(tkinterdnd2.DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)
            w.dnd_bind("<<DropEnter>>", lambda e: self._hl(True))
            w.dnd_bind("<<DropLeave>>", lambda e: self._hl(False))

    def _hl(self, on: bool) -> None:
        self.drop.configure(border_color=ACCENT_TX if on else HAIRLINE)

    def _out_display(self) -> str:
        where = self.output_dir.name if self.output_dir else "next to originals"
        return f"Save to: {where}"

    def _show_no_publisher(self) -> None:
        self.drop_icon.configure(image=icon("alert", 40, light="#a01722", dark="#ffa8ad"))
        self.drop_label.configure(
            text="Microsoft Publisher isn't installed on this PC.\n"
                 "Ask IT to install Publisher, then reopen this app.", text_color=TEXT)
        self.browse_btn.configure(state="disabled")
        self.convert_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.subtitle.configure(text="This tool converts using Publisher itself.")

    def _save_scope(self) -> None:
        self.settings["scan_locations"] = self.scan_locations
        save_settings(self.settings)

    def _open_scope(self) -> None:
        if not self.publisher or self.scanning or self.running:
            return
        ScopeDialog(self).focus()

    def _history_note(self, path: Path) -> str:
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
        known = {file_key(r.pub_file) for r in self.rows}
        added = 0
        for p in paths:
            k = file_key(p)
            if k in known:
                continue
            known.add(k)
            if not self.rows:
                self._reveal_list()
            note = self._history_note(p)
            row = FileRow(self.list, p, selected=(note == ""), note=note)
            row.grid(sticky="ew", padx=8, pady=2)
            self.rows.append(row)
            added += 1
        if added:
            self._update_summary()
        return added

    def _reveal_list(self) -> None:
        self.drop.grid_remove()
        self.list.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self.list.drop_target_register(tkinterdnd2.DND_FILES)
        self.list.dnd_bind("<<Drop>>", self._on_drop)
        self.subtitle.configure(text="Ticked files convert. New files are ticked; "
                                     "already-converted ones aren't.")

    def _start_scan(self, roots: list[str]) -> None:
        if self.scanning or self.running or not self.publisher:
            return
        self.scanning = True
        self.scan_cancel.clear()
        self._set_busy(True, scanning=True)
        self.subtitle.configure(text=f"Scanning {len(roots)} location"
                                     f"{'s' if len(roots) != 1 else ''}…", text_color=TEXT2)
        self.worker = threading.Thread(target=self._scan_worker, args=(roots,), daemon=True)
        self.worker.start()

    def _scan_worker(self, roots: list[str]) -> None:
        found = skipped = unreadable = 0
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
                for pub, status in iter_pub_candidates(root, self.scan_cancel):
                    if self.scan_cancel.is_set():
                        break
                    if status == "publisher":
                        found += 1
                        self.events.put(("scan_found", None, str(pub), found))
                    elif status == "unreadable":
                        unreadable += 1
                    else:
                        skipped += 1
            except Exception:  # noqa: BLE001 — a bad subtree shouldn't kill the scan
                pass
        self.events.put(("scan_done", (skipped, unreadable), "", found))

    def _stop_scan(self) -> None:
        self.scan_cancel.set()
        self.stop_btn.configure(text="Stopping…", state="disabled")

    def _on_output_choice(self, choice: str) -> None:
        if choice == "Choose a folder…":
            from tkinter import filedialog
            d = filedialog.askdirectory(title="Where should PDFs go?")
            if d:
                self._set_output(Path(d))
        elif choice == "Next to each original":
            self._set_output(None)
        self.out_menu.set(self._out_display())

    def _set_output(self, d: "Path | None") -> None:
        self.output_dir = d
        self.settings["output_dir"] = str(d) if d else ""
        save_settings(self.settings)

    def _open_output(self) -> None:
        done = [r for r in self.rows if r.state == "done" and r.pdf_path]
        if done:
            reveal(done[-1].pdf_path)
        elif self.output_dir:
            reveal(self.output_dir)

    def _convertible(self) -> list[FileRow]:
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
                    self.subtitle.configure(text=f"Publisher couldn't start: {b}",
                                            text_color=REASON)
                elif kind == "finished":
                    self.running = False
                    self._set_busy(False)
                elif kind == "scan_found":
                    self._add([Path(a)])
                    self.subtitle.configure(text=f"Scanning… {b} found", text_color=TEXT2)
                elif kind == "scan_note":
                    if b == "unavailable":
                        self.subtitle.configure(text=f"Skipped, unavailable: {a}",
                                                text_color=REASON)
                elif kind == "scan_done":
                    self.scanning = False
                    self._set_busy(False)
                    skipped, unreadable = row if isinstance(row, tuple) else (0, 0)
                    core = (f"{b} Publisher file{'s' if b != 1 else ''} found"
                            if b else "no Publisher files found")
                    bits = []
                    if skipped:
                        bits.append(f"{skipped} non-Publisher .pub skipped")
                    if unreadable:
                        bits.append(f"{unreadable} unreadable")
                    tail = f" ({', '.join(bits)})" if bits else ""
                    self.subtitle.configure(text=f"Scan complete. {core}{tail}.", text_color=TEXT2)
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
        converted = sum(r.converted_now for r in self.rows)
        failed = sum(r.state == "failed" for r in self.rows)

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
            return "This PDF is open in another program. Close it and retry."
        if "denied" in low or "permission" in low:
            return "Can't write to that folder. Check its permissions."
        if "password" in low or "protected" in low:
            return "This .pub is password-protected. Open it in Publisher first."
        if "cannot locate" in low or "not found" in low:
            return "The .pub file was moved or deleted before it could convert."
        return (desc[:90] + "…") if len(desc) > 90 else desc

    for code in codes:
        if (code & 0xFFFF0000) == 0x80070000:
            win32 = code & 0xFFFF
            if win32 in (32, 33):
                return "This PDF is open in another program. Close it and retry."
            if win32 == 5:
                return "Can't write to that folder. Check its permissions."
            if win32 in (2, 3):
                return "The .pub file or folder was moved or deleted."

    return ("Couldn't save the PDF. It may be open in another program, or the "
            "folder may be read-only. Close any open copy and retry.")


def main() -> None:
    if sys.platform != "win32":
        sys.exit("pub2pdf app runs on Windows (it drives Microsoft Publisher).")
    App().mainloop()


if __name__ == "__main__":
    main()
