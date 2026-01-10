import os
import sys
import tkinter
import mimetypes
import base64
import threading
import queue
import time
import tempfile
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from datetime import datetime
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import messagebox

import utils

from tkinterdnd2 import DND_FILES, TkinterDnD

@dataclass(frozen=True)
class DroppedFile:
    path: str
    name: str
    size_bytes: int | None
    mime: str
    ext: str

@dataclass
class ConversionJob:
    source_path: str
    source_name: str
    target_ext: str
    status: str = "Queued"


@dataclass
class ConversionResultItem:
    source_path: str
    source_name: str
    output_path: str
    target_ext: str

def _resource_base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

def _set_window_icon(root: tkinter.Tk) -> None:
    icon_path = _resource_base_dir() / "assets" / "icon.png"
    icon_image = tkinter.PhotoImage(file=str(icon_path))
    root.iconphoto(True, icon_image)
    root._icon_image = icon_image  # type: ignore[attr-defined]

def _human_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"

def _detect_kind(ext: str, is_dir: bool) -> str:
    if is_dir:
        return "Folder"
    lowered = ext.lower()
    guessed, _encoding = mimetypes.guess_type(f"file{lowered}", strict=False)
    return guessed or "application/octet-stream"

def _to_dropped_file(path: str) -> DroppedFile:
    p = Path(path)
    is_dir = p.is_dir()
    ext = p.suffix.upper() if p.suffix else "—"
    size_bytes: int | None
    if is_dir:
        size_bytes = None
    else:
        try:
            size_bytes = os.path.getsize(p)
        except OSError:
            size_bytes = None
    return DroppedFile(
        path=str(p),
        name=p.name or str(p),
        size_bytes=size_bytes,
        mime=("inode/directory" if is_dir else _detect_kind(p.suffix, is_dir=is_dir)),
        ext=ext,
    )


def _parse_dnd_files(root: tkinter.Tk, event_data: str) -> list[str]:
    # event.data is a Tcl list of file paths; splitlist handles spaces/braces.
    return [str(p) for p in root.tk.splitlist(event_data) if p]

class ConvertableApp:
    def __init__(self) -> None:
        self.root = TkinterDnD.Tk()
        self.root.title("Convertable")
        self.root.geometry("900x560")
        self.root.resizable(True, True)

        default_font = ("Inter", 14)
        self.root.option_add("*Font", default_font)
        _set_window_icon(self.root)

        self.dropped: list[DroppedFile] = []
        self.jobs: list[ConversionJob] = []
        self.results: list[ConversionResultItem] = []

        self._task_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._ui_events: queue.Queue[tuple] = queue.Queue()
        self._in_progress: set[str] = set()
        self._progress: dict[str, float] = {}
        self._display_progress: dict[str, float] = {}
        self._target_by_path: dict[str, str] = {}
        self._animate_active: bool = False
        self._last_progress_ts: dict[str, float] = {}
        self._job_started_ts: dict[str, float] = {}

        # Converted outputs are written to a temporary session folder first.
        # They only get copied to the user's disk output folder when they click Save.
        self._session_output_dir = tempfile.mkdtemp(prefix="convertable-")
        self._output_dir = self._session_output_dir
        self._save_dir = str((Path.home() / "Convertable" / "Output").resolve())

        # Persistent debug log (helps diagnose "freezes" that never surface as dialogs).
        self._debug_log_path = str((Path.home() / "Convertable" / "Debug" / "convertable-debug.log").resolve())
        try:
            Path(self._debug_log_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._debug_log(f"App start; session_output_dir={self._session_output_dir}")

        self.remove_icon = self._load_remove_icon()

        self.font_normal = tkfont.nametofont("TkDefaultFont")

        # Column minimum widths (in pixels). Stats should not be the first thing to clip.
        self._min_size_px = self.font_normal.measure("999.9 MB") + 16
        self._min_mime_px = self.font_normal.measure("application/octet-stream") + 16
        self._min_ext_px = self.font_normal.measure(".JPEG") + 16
        self._min_remove_px = 14 + 16

        # Finder-like styling palette (used by Result and now also Convert list).
        self._result_bg = "#1c1c1e"
        self._result_text = "#f2f2f7"
        self._result_muted = "#b0b0b5"
        self._result_selected_bg = "#0a84ff"
        self._result_selected_text = "#ffffff"

        # Convert tab uses the same Finder-like colors.
        self._normal_bg = self._result_bg
        self._selected_bg = self._result_selected_bg
        self._convert_text = self._result_text
        self._convert_muted = self._result_muted
        self._convert_selected_text = self._result_selected_text

        # Darker green for contrast against the dark background.
        self._progress_bg = "#00c936"

        self.selected_result_index: int | None = None
        self._result_rows: list[dict[str, object]] = []

        self.selected_paths: list[str] = []
        self._selection_anchor: str | None = None
        self._convert_rows: dict[str, dict[str, object]] = {}

        self._worker = threading.Thread(target=self._conversion_worker, daemon=True)
        self._worker.start()
        self.root.after(60, self._process_ui_events)

        # If any Tk callback raises, Tk will print to stderr and that scheduled loop may stop.
        # Capture those exceptions so periodic polling/animation can't silently die.
        self.root.report_callback_exception = self._on_tk_exception

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.drop_frame = ttk.Frame(self.notebook)
        self.convert_frame = ttk.Frame(self.notebook)
        self.result_frame = ttk.Frame(self.notebook)

        self.notebook.add(self.drop_frame, text="Drop")
        self.notebook.add(self.convert_frame, text="Convert")
        self.notebook.add(self.result_frame, text="Result")

        self._build_drop_tab()
        self._build_convert_tab()
        self._build_result_tab()

        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        try:
            self._debug_log("App closing")
            shutil.rmtree(self._session_output_dir, ignore_errors=True)
        finally:
            self.root.destroy()

    def _debug_log(self, msg: str) -> None:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self._debug_log_path, "a", encoding="utf-8") as fp:
                fp.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # -------------------- Drop Tab --------------------
    def _build_drop_tab(self) -> None:
        instructions = ttk.Label(
            self.drop_frame,
            text="Drag and drop file(s) onto this window.",
            justify="center",
        )
        instructions.pack(expand=True)

    # -------------------- Convert Tab --------------------
    def _build_convert_tab(self) -> None:
        self.convert_frame.rowconfigure(1, weight=1)
        self.convert_frame.columnconfigure(0, weight=1)

        # Job bar (hidden unless conversions running)
        self.job_bar = ttk.Frame(self.convert_frame)
        self.job_bar.grid(row=0, column=0, sticky="ew")
        self.job_bar.columnconfigure(0, weight=1)
        self.job_bar_label = ttk.Label(self.job_bar, text="")
        self.job_bar_label.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 6))
        self.job_bar_progress = ttk.Progressbar(self.job_bar, orient="horizontal", mode="determinate", maximum=100)
        self.job_bar_progress.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.job_bar.grid_remove()

        # Scrollable file list
        list_host = ttk.Frame(self.convert_frame)
        list_host.grid(row=1, column=0, sticky="nsew")
        list_host.rowconfigure(0, weight=1)
        list_host.columnconfigure(0, weight=1)

        self.convert_canvas = tkinter.Canvas(list_host, highlightthickness=0)
        self.convert_canvas.grid(row=0, column=0, sticky="nsew")
        self.convert_canvas.configure(bg=self._normal_bg)
        self.convert_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.convert_canvas.yview)
        self.convert_scroll.grid(row=0, column=1, sticky="ns")
        self.convert_canvas.configure(yscrollcommand=self.convert_scroll.set)

        # Use a tk Frame so we can reliably apply background colors.
        self.convert_list_frame = tkinter.Frame(self.convert_canvas, bg=self._normal_bg)
        self.convert_list_frame.columnconfigure(0, weight=1)
        self._convert_list_window = self.convert_canvas.create_window((0, 0), window=self.convert_list_frame, anchor="nw")

        self.convert_list_frame.bind("<Configure>", self._on_convert_list_configure)
        self.convert_canvas.bind("<Configure>", self._on_convert_canvas_configure)
        self.convert_list_frame.bind("<Button-1>", self._on_convert_blank_click)

        # Mouse wheel scrolling (trackpad included). Bind only while cursor is over the list.
        self.convert_canvas.bind("<Enter>", self._bind_convert_mousewheel)
        self.convert_canvas.bind("<Leave>", self._unbind_convert_mousewheel)

        # Bottom actions bar (always visible, avoids disappearing buttons on narrow widths)
        actions = ttk.Frame(self.convert_frame)
        actions.grid(row=2, column=0, sticky="ew")

        self.selected_file_label = ttk.Label(actions, text="Select file(s)")
        self.selected_file_label.pack(side="left", padx=12, pady=10)

        ttk.Label(actions, text="Convert to").pack(side="left", padx=(8, 6))
        self._all_convert_options = [
            ".PNG",
            ".JPEG",
            ".WEBP",
            ".MP4",
            ".MOV",
        ]
        self.convert_to_var = tkinter.StringVar(value=self._all_convert_options[0])
        self.convert_to = ttk.Combobox(
            actions,
            textvariable=self.convert_to_var,
            values=self._all_convert_options,
            state="normal",
        )
        self.convert_to.pack(side="left", padx=(0, 10), pady=8)
        self.convert_to.bind("<KeyRelease>", self._filter_convert_options)

        self.convert_btn = ttk.Button(actions, text="Convert", command=self._queue_conversion)
        self.convert_btn.pack(side="right", padx=12, pady=8)

        self._set_action_enabled(False)

    def _set_action_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.convert_btn.configure(state=state)
        self.convert_to.configure(state=("normal" if enabled else "disabled"))

    def _on_convert_list_configure(self, _event=None) -> None:
        self.convert_canvas.configure(scrollregion=self.convert_canvas.bbox("all"))

    def _bind_convert_mousewheel(self, _event=None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_convert_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_convert_mousewheel)
        self.root.bind_all("<Button-5>", self._on_convert_mousewheel)

    def _unbind_convert_mousewheel(self, _event=None) -> None:
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_convert_mousewheel(self, event) -> None:
        # Only scroll when the canvas is actually scrollable.
        if self.convert_canvas is None:
            return
        # Windows/macOS use MouseWheel delta, Linux uses Button-4/5.
        if getattr(event, "num", None) == 4:
            self.convert_canvas.yview_scroll(-1, "units")
            return
        if getattr(event, "num", None) == 5:
            self.convert_canvas.yview_scroll(1, "units")
            return

        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        # On macOS, delta is small and inverted vs "natural" in some configs.
        direction = -1 if delta > 0 else 1
        steps = 1
        if sys.platform.startswith("win"):
            steps = max(1, int(abs(delta) / 120))
        self.convert_canvas.yview_scroll(direction * steps, "units")

    def _on_convert_canvas_configure(self, event) -> None:
        # Make inner frame match canvas width so filename column can shrink.
        self.convert_canvas.itemconfigure(self._convert_list_window, width=event.width)
        self._update_name_clipping(event.width)
        self._refresh_row_visuals()

    def _selection_colors(self) -> tuple[str, str]:
        # Prefer themed selection background.
        style = ttk.Style(self.root)
        selected = style.lookup("Treeview", "selectbackground")
        if not selected:
            selected = style.lookup("Treeview", "background", ("selected",))
        if not selected:
            selected = "#cfe8ff"

        # Normal background: use canvas background if available.
        try:
            normal = self.convert_canvas.cget("background")
        except Exception:
            normal = self.root.cget("bg")
        if not normal:
            normal = "#ffffff"
        return str(normal), str(selected)

    def _on_convert_blank_click(self, event) -> None:
        # Clicking on empty space clears selection.
        if event.widget is self.convert_list_frame:
            self._set_selected_paths([])

    def _ellipsize(self, text: str, max_px: int) -> str:
        if max_px <= 0:
            return ""
        if self.font_normal.measure(text) <= max_px:
            return text
        ell = "…"
        # Binary search best prefix length.
        lo, hi = 0, len(text)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = text[:mid] + ell
            if self.font_normal.measure(candidate) <= max_px:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best or ell

    def _update_name_clipping(self, canvas_width: int) -> None:
        # Space reserved for stats + padding.
        reserved = self._min_remove_px + self._min_size_px + self._min_mime_px + self._min_ext_px
        reserved += 12 * 3  # right padding for size/mime/ext
        reserved += 12 + 6  # left padding on filename + spacing before icon
        # Remove icon sits in its own column near the stats.
        max_name_px = max(60, canvas_width - reserved)

        for widgets in self._convert_rows.values():
            full = widgets.get("full_name")
            name_label = widgets.get("name")
            if isinstance(full, str) and isinstance(name_label, tkinter.Label):
                name_label.configure(text=self._ellipsize(full, max_name_px))

    def _set_selected_paths(self, paths: list[str]) -> None:
        # Preserve order and uniqueness.
        seen: set[str] = set()
        self.selected_paths = []
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            self.selected_paths.append(p)
        if self.selected_paths:
            self._selection_anchor = self.selected_paths[-1]
        self._update_convert_selection_ui()

    def _update_convert_selection_ui(self) -> None:
        # Update label + enabled state
        if not self.selected_paths:
            self.selected_file_label.configure(text="Select file(s)")
            self._set_action_enabled(False)
        elif len(self.selected_paths) == 1:
            f = self._find_dropped_by_path(self.selected_paths[0])
            self.selected_file_label.configure(text=(f.name if f else "Select file(s)"))
            self._set_action_enabled(True)
            if f:
                self._set_convert_options_for_kind(f.mime)
        else:
            self.selected_file_label.configure(text=f"{len(self.selected_paths)} files selected")
            self._set_action_enabled(True)
            self._set_convert_options_for_selection(self.selected_paths)

        self._refresh_row_visuals()

    def _refresh_row_visuals(self) -> None:
        for path in self._convert_rows.keys():
            self._apply_row_visual_state(path)

    def _apply_row_visual_state(self, path: str) -> None:
        widgets = self._convert_rows.get(path)
        if not widgets:
            return

        row = widgets.get("row")
        if not isinstance(row, tkinter.Frame):
            return

        is_selected = path in set(self.selected_paths)
        base_bg = self._selected_bg if is_selected else self._normal_bg

        def _fg_for(key: str, bg: str) -> str:
            # On progress-fill green, force white for legibility.
            if bg == self._progress_bg:
                return self._convert_selected_text
            if is_selected:
                return self._convert_selected_text
            # Name is primary; stats are muted.
            if key == "name":
                return self._convert_text
            if key in {"size", "mime", "ext"}:
                return self._convert_muted
            return self._convert_text

        # Base background for the whole row.
        row.configure(bg=base_bg)

        progress = float(self._display_progress.get(path, self._progress.get(path, 0.0)))
        if progress < 0:
            progress = 0.0
        if progress > 1:
            progress = 1.0

        fill = widgets.get("progress_fill")
        row_w = max(1, row.winfo_width())
        filled_px = int(row_w * progress)

        # Avoid the lingering 1px sliver at 0% by hiding the fill.
        if isinstance(fill, tkinter.Frame):
            if filled_px <= 0:
                fill.place_forget()
            else:
                fill.configure(bg=self._progress_bg)
                fill.place(x=0, y=0, relheight=1.0, width=filled_px)
                fill.lower()

        # Apply progress background to visible UI objects so the fill looks continuous.
        if filled_px <= 0 or is_selected:
            # Selection highlight takes precedence over progress fill.
            for key in ("name", "size", "mime", "ext", "remove", "spacer"):
                w = widgets.get(key)
                if isinstance(w, tkinter.Label):
                    # Remove icon has no meaningful fg; safe to set anyway.
                    w.configure(bg=base_bg, fg=_fg_for(key, base_bg))
            name_group = widgets.get("name_group")
            if isinstance(name_group, tkinter.Frame):
                name_group.configure(bg=base_bg)
            return

        try:
            row_rootx = row.winfo_rootx()
        except Exception:
            row_rootx = 0

        def _bg_for_widget(w: tkinter.Widget) -> str:
            try:
                x0 = w.winfo_rootx() - row_rootx
                x1 = x0 + w.winfo_width()
                mid = int((x0 + x1) / 2)
            except Exception:
                mid = 0
            return self._progress_bg if mid <= filled_px else base_bg

        # Update label backgrounds and text colors based on whether they're over the progress fill.
        for key in ("name", "size", "mime", "ext", "spacer"):
            w = widgets.get(key)
            if isinstance(w, tkinter.Label):
                bg = _bg_for_widget(w)
                w.configure(bg=bg, fg=_fg_for(key, bg))
        # Remove icon background should blend with row; keep it simple.
        rem = widgets.get("remove")
        if isinstance(rem, tkinter.Label):
            rem.configure(bg=base_bg)
        name_group = widgets.get("name_group")
        if isinstance(name_group, tkinter.Frame):
            name_group.configure(bg=base_bg)

        # Frames
        name_group = widgets.get("name_group")
        if isinstance(name_group, tkinter.Frame):
            name_group.configure(bg=_bg_for_widget(name_group))

        # Labels
        for key in ("name", "size", "mime", "ext", "remove", "spacer"):
            w = widgets.get(key)
            if isinstance(w, tkinter.Label):
                w.configure(bg=_bg_for_widget(w))

    def _set_convert_options_for_selection(self, selected_paths: list[str]) -> None:
        mimes: list[str] = []
        for path in selected_paths:
            f = self._find_dropped_by_path(path)
            if f is not None:
                mimes.append(f.mime)

        if mimes and all(m.startswith("image/") for m in mimes):
            self._set_convert_options_for_kind("image/")
            return
        if mimes and all(m.startswith("video/") for m in mimes):
            self._set_convert_options_for_kind("video/")
            return
        self._set_convert_options_for_kind("application/octet-stream")

    def _set_convert_options_for_kind(self, mime: str) -> None:
        if mime.startswith("image/"):
            options = [".PNG", ".JPEG", ".WEBP"]
        elif mime.startswith("video/"):
            options = [".MP4", ".MOV"]
        else:
            options = [".PNG", ".JPEG", ".WEBP", ".MP4", ".MOV"]
        self._all_convert_options = options
        self.convert_to.configure(values=options)
        if self.convert_to_var.get() not in options:
            self.convert_to_var.set(options[0])

    def _filter_convert_options(self, _event=None) -> None:
        typed = self.convert_to_var.get().strip().upper()
        if not typed:
            self.convert_to.configure(values=self._all_convert_options)
            return
        filtered = [v for v in self._all_convert_options if typed in v]
        self.convert_to.configure(values=filtered if filtered else self._all_convert_options)

    def _remove_selected(self) -> None:
        sel = set(self.selected_paths)
        if not sel:
            return
        self._remove_paths(sel)

    def _remove_paths(self, paths: set[str]) -> None:
        self.dropped = [f for f in self.dropped if f.path not in paths]
        self.jobs = [j for j in self.jobs if j.source_path not in paths]
        for p in paths:
            self._in_progress.discard(p)
            self._progress.pop(p, None)
            self._display_progress.pop(p, None)
            self._target_by_path.pop(p, None)
            self._last_progress_ts.pop(p, None)
            self._job_started_ts.pop(p, None)
        self._refresh_all_lists()
        self._update_job_bar()

    def _queue_conversion(self) -> None:
        sel = list(self.selected_paths)
        if not sel:
            return
        target_ext = self.convert_to_var.get().strip().upper()
        if not target_ext.startswith("."):
            target_ext = "." + target_ext

        for src_path in sel:
            if src_path in self._in_progress:
                continue
            dropped_file = self._find_dropped_by_path(src_path)
            if dropped_file is None:
                continue
            self._in_progress.add(src_path)
            self._progress[src_path] = 0.0
            self._display_progress[src_path] = 0.0
            self._target_by_path[src_path] = target_ext
            now = time.time()
            self._last_progress_ts[src_path] = now
            self._job_started_ts[src_path] = now
            self._task_queue.put((src_path, target_ext))

        # Keep user on this page; show progress fill.
        self._start_progress_animation()
        self._refresh_convert_progress()
        self._update_job_bar()

    # -------------------- Result Tab --------------------
    def _build_result_tab(self) -> None:
        container = tkinter.Frame(self.result_frame, bg=self._result_bg)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        list_host = tkinter.Frame(container, bg=self._result_bg)
        list_host.grid(row=0, column=0, sticky="nsew")
        list_host.rowconfigure(0, weight=1)
        list_host.columnconfigure(0, weight=1)

        self.result_canvas = tkinter.Canvas(list_host, highlightthickness=0, bd=0, bg=self._result_bg)
        self.result_canvas.grid(row=0, column=0, sticky="nsew")
        self.result_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.result_canvas.yview)
        self.result_scroll.grid(row=0, column=1, sticky="ns")
        self.result_canvas.configure(yscrollcommand=self.result_scroll.set)

        self.result_list_frame = tkinter.Frame(self.result_canvas, bg=self._result_bg)
        self.result_list_frame.columnconfigure(0, weight=1)
        self._result_list_window = self.result_canvas.create_window((0, 0), window=self.result_list_frame, anchor="nw")

        self.result_list_frame.bind("<Configure>", lambda _e: self.result_canvas.configure(scrollregion=self.result_canvas.bbox("all")))
        self.result_canvas.bind("<Configure>", self._on_result_canvas_configure)

        # Mouse wheel scrolling (trackpad included). Bind only while cursor is over the list.
        self.result_canvas.bind("<Enter>", self._bind_result_mousewheel)
        self.result_canvas.bind("<Leave>", self._unbind_result_mousewheel)

        # Bottom actions bar (Save)
        actions = ttk.Frame(container)
        actions.grid(row=1, column=0, sticky="ew")
        self.save_btn = ttk.Button(actions, text="Save", command=self._save_selected_results)
        self.save_btn.pack(side="right", padx=12, pady=8)

    def _on_result_drag_init(self, _event=None):
        idx = self.selected_result_index
        if idx is None or idx < 0 or idx >= len(self.results):
            return
        out_path = self.results[idx].output_path
        if not out_path or not os.path.exists(out_path):
            return
        # tkdnd expects (actions, types, data)
        return ("copy",), (DND_FILES,), out_path

    def _bind_result_mousewheel(self, _event=None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_result_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_result_mousewheel)
        self.root.bind_all("<Button-5>", self._on_result_mousewheel)

    def _unbind_result_mousewheel(self, _event=None) -> None:
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_result_mousewheel(self, event) -> None:
        if self.result_canvas is None:
            return
        if getattr(event, "num", None) == 4:
            self.result_canvas.yview_scroll(-1, "units")
            return
        if getattr(event, "num", None) == 5:
            self.result_canvas.yview_scroll(1, "units")
            return

        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        direction = -1 if delta > 0 else 1
        steps = 1
        if sys.platform.startswith("win"):
            steps = max(1, int(abs(delta) / 120))
        self.result_canvas.yview_scroll(direction * steps, "units")

    def _on_result_canvas_configure(self, event) -> None:
        self.result_canvas.itemconfigure(self._result_list_window, width=event.width)
        self._layout_result_rows(event.width)

    def _rounded_rect(self, c: tkinter.Canvas, x1: int, y1: int, x2: int, y2: int, r: int, **kwargs) -> int:
        r = max(0, min(r, int((x2 - x1) / 2), int((y2 - y1) / 2)))
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return c.create_polygon(points, smooth=True, splinesteps=12, **kwargs)

    def _layout_result_rows(self, width: int) -> None:
        for row in self._result_rows:
            c = row.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                continue
            c.configure(width=width)
            self._render_result_row(row, width)

    def _render_result_row(self, row: dict[str, object], width: int) -> None:
        c = row.get("canvas")
        if not isinstance(c, tkinter.Canvas):
            return
        idx = row.get("index")
        if not isinstance(idx, int):
            return
        if idx < 0 or idx >= len(self.results):
            return

        res = self.results[idx]
        is_selected = (self.selected_result_index == idx)

        row_h = int(c.cget("height"))
        pad_x = 10
        pad_y = 4
        radius = 8

        # Layout: name | target | output
        target_w = 90
        output_w = 280
        name_w = max(140, width - (pad_x * 2) - target_w - output_w - 20)

        name_x = pad_x + 10
        target_x = pad_x + name_w + 10 + int(target_w / 2)
        output_x = pad_x + name_w + 10 + target_w + 10

        # Selection shape
        sel_id = row.get("sel")
        if isinstance(sel_id, int):
            c.delete(sel_id)
        if is_selected:
            sel_id = self._rounded_rect(
                c,
                pad_x,
                pad_y,
                max(pad_x + 1, width - pad_x),
                max(pad_y + 1, row_h - pad_y),
                radius,
                fill=self._result_selected_bg,
                outline="",
            )
            row["sel"] = sel_id

        if is_selected:
            text_color = self._result_selected_text
            muted = self._result_selected_text
        else:
            text_color = self._result_text
            muted = self._result_muted

        name = self._ellipsize(res.source_name, name_w - 10)
        target = res.target_ext
        output = self._ellipsize(os.path.basename(res.output_path) if res.output_path else "", output_w - 10)

        # Text items
        t_name = row.get("t_name")
        t_target = row.get("t_target")
        t_output = row.get("t_output")
        if isinstance(t_name, int):
            c.coords(t_name, name_x, int(row_h / 2))
            c.itemconfigure(t_name, text=name, fill=text_color)
        if isinstance(t_target, int):
            c.coords(t_target, target_x, int(row_h / 2))
            c.itemconfigure(t_target, text=target, fill=muted)
        if isinstance(t_output, int):
            c.coords(t_output, output_x, int(row_h / 2))
            c.itemconfigure(t_output, text=output, fill=muted)

        # Separator line
        sep = row.get("sep")
        if isinstance(sep, int):
            c.coords(sep, pad_x, row_h - 1, width - pad_x, row_h - 1)

    def _on_result_row_click(self, idx: int) -> None:
        self.selected_result_index = idx
        self._refresh_result_row_visuals()

    def _refresh_result_row_visuals(self) -> None:
        width = self.result_canvas.winfo_width() if hasattr(self, "result_canvas") else 0
        if width <= 0:
            width = 900
        for row in self._result_rows:
            self._render_result_row(row, width)

    def _unique_dest_path(self, dest_dir: str, filename: str) -> str:
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(dest_dir, filename)
        if not os.path.exists(candidate):
            return candidate
        n = 1
        while True:
            cand = os.path.join(dest_dir, f"{base} ({n}){ext}")
            if not os.path.exists(cand):
                return cand
            n += 1

    def _save_selected_results(self) -> None:
        idx = self.selected_result_index
        if idx is None or idx < 0 or idx >= len(self.results):
            return

        res = self.results[idx]
        src = res.output_path
        if not src or not os.path.exists(src):
            return

        os.makedirs(self._save_dir, exist_ok=True)
        dest = self._unique_dest_path(self._save_dir, os.path.basename(src))
        shutil.copy2(src, dest)

        # Update to saved path and delete temp to minimize disk usage.
        try:
            if os.path.commonpath([os.path.abspath(src), os.path.abspath(self._session_output_dir)]) == os.path.abspath(self._session_output_dir):
                os.remove(src)
        except Exception:
            pass

        self.results[idx] = ConversionResultItem(
            source_path=res.source_path,
            source_name=res.source_name,
            output_path=dest,
            target_ext=res.target_ext,
        )
        self._refresh_result_list()

    # -------------------- Shared --------------------
    def _find_dropped_by_path(self, path: str) -> DroppedFile | None:
        for f in self.dropped:
            if f.path == path:
                return f
        return None

    def _on_drop(self, event) -> None:
        files = _parse_dnd_files(self.root, event.data)

        # Add unique paths only.
        existing = {f.path for f in self.dropped}
        new_paths = [p for p in files if p not in existing]

        for p in new_paths:
            self.dropped.append(_to_dropped_file(p))
        self._refresh_all_lists()

        # Switch to Convert tab and highlight newly-added files.
        self.notebook.select(self.convert_frame)
        if new_paths:
            self._set_selected_paths(new_paths)
            self._scroll_to_path(new_paths[0])

    def _refresh_all_lists(self) -> None:
        self._refresh_convert_list()
        self._refresh_result_list()

    def _refresh_convert_list(self) -> None:
        # Clear existing rows
        for child in list(self.convert_list_frame.winfo_children()):
            child.destroy()
        self._convert_rows.clear()

        # Keep selection only for remaining files
        remaining = {f.path for f in self.dropped}
        self.selected_paths = [p for p in self.selected_paths if p in remaining]

        for row_idx, f in enumerate(self.dropped):
            row = tkinter.Frame(self.convert_list_frame, bg=self._normal_bg)
            row.grid(row=row_idx, column=0, sticky="ew")
            # Columns: 0 name_group | 1 spacer(expands) | 2 remove | 3 size | 4 mime | 5 ext
            row.columnconfigure(1, weight=1)
            row.columnconfigure(2, minsize=self._min_remove_px)
            row.columnconfigure(3, minsize=self._min_size_px)
            row.columnconfigure(4, minsize=self._min_mime_px)
            row.columnconfigure(5, minsize=self._min_ext_px)

            # Name group (filename only)
            name_group = tkinter.Frame(row, bg=self._normal_bg)
            name_group.grid(row=0, column=0, sticky="w", padx=(12, 6), pady=6)

            # Progress fill (behind content)
            progress_fill = tkinter.Frame(row, bg=self._progress_bg)
            progress_fill.place(x=0, y=0, relheight=1.0, width=0)
            progress_fill.lower()

            name_label = tkinter.Label(
                name_group,
                text=f.name,
                anchor="w",
                bg=self._normal_bg,
                fg=self._convert_text,
                font=self.font_normal,
            )
            name_label.pack(side="left")

            # Spacer column takes remaining width so stats stay visible.
            spacer = tkinter.Label(row, text="", bg=self._normal_bg, fg=self._convert_text)
            spacer.grid(row=0, column=1, sticky="ew")

            # Remove icon sits right before size (aligned with stats)
            remove_label = tkinter.Label(row, image=self.remove_icon, bg=self._normal_bg)
            remove_label.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=6)
            remove_label.bind("<Button-1>", lambda _e, p=f.path: (self._remove_paths({p}), "break")[1])

            size_label = tkinter.Label(
                row,
                text=_human_size(f.size_bytes),
                anchor="e",
                bg=self._normal_bg,
                fg=self._convert_muted,
                font=self.font_normal,
            )
            size_label.grid(row=0, column=3, sticky="e", padx=(0, 12), pady=6)

            mime_label = tkinter.Label(
                row,
                text=f.mime,
                anchor="w",
                bg=self._normal_bg,
                fg=self._convert_muted,
                font=self.font_normal,
            )
            mime_label.configure(anchor="e")
            mime_label.grid(row=0, column=4, sticky="e", padx=(0, 12), pady=6)

            ext_label = tkinter.Label(
                row,
                text=f.ext,
                anchor="e",
                bg=self._normal_bg,
                fg=self._convert_muted,
                font=self.font_normal,
            )
            ext_label.grid(row=0, column=5, sticky="e", padx=(0, 12), pady=6)

            # Click anywhere on row (except the remove icon) to select.
            def _row_click(ev, p=f.path) -> None:
                self._on_row_click(ev, p)

            row.bind("<Button-1>", _row_click)
            name_label.bind("<Button-1>", _row_click)
            size_label.bind("<Button-1>", _row_click)
            mime_label.bind("<Button-1>", _row_click)
            ext_label.bind("<Button-1>", _row_click)
            spacer.bind("<Button-1>", _row_click)

            # Right-click context menu
            def _ctx(ev, p=f.path) -> None:
                self._show_convert_context_menu(ev, p)

            row.bind("<Button-3>", _ctx)
            row.bind("<Button-2>", _ctx)
            name_label.bind("<Button-3>", _ctx)
            name_label.bind("<Button-2>", _ctx)
            size_label.bind("<Button-3>", _ctx)
            size_label.bind("<Button-2>", _ctx)
            mime_label.bind("<Button-3>", _ctx)
            mime_label.bind("<Button-2>", _ctx)
            ext_label.bind("<Button-3>", _ctx)
            ext_label.bind("<Button-2>", _ctx)
            spacer.bind("<Button-3>", _ctx)
            spacer.bind("<Button-2>", _ctx)

            self._convert_rows[f.path] = {
                "row": row,
                "name": name_label,
                "remove": remove_label,
                "name_group": name_group,
                "spacer": spacer,
                "size": size_label,
                "mime": mime_label,
                "ext": ext_label,
                "progress_fill": progress_fill,
                "full_name": f.name,
            }

        self._update_convert_selection_ui()
        # Apply initial clipping based on current width.
        self.convert_canvas.update_idletasks()
        self._update_name_clipping(self.convert_canvas.winfo_width())
        self.convert_canvas.configure(scrollregion=self.convert_canvas.bbox("all"))

        self._refresh_row_visuals()
        self._update_job_bar()

    def _show_convert_context_menu(self, event, path: str) -> None:
        try:
            menu = tkinter.Menu(self.root, tearoff=0)
            menu.add_command(label="Show in Finder", command=lambda p=path: self._reveal_in_finder(p))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _reveal_in_finder(self, path: str) -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], check=False)
            return
        # Best-effort fallback
        try:
            subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
        except Exception:
            pass

    def _scroll_to_path(self, path: str) -> None:
        widgets = self._convert_rows.get(path)
        if not widgets:
            return
        row = widgets.get("row")
        if not isinstance(row, ttk.Frame):
            return
        self.convert_canvas.update_idletasks()
        y = row.winfo_y()
        height = max(1, self.convert_list_frame.winfo_height())
        self.convert_canvas.yview_moveto(y / height)

    def _on_row_click(self, event, path: str) -> None:
        # Multi-select support:
        # - Click: select single
        # - Shift-click: select range from anchor
        # - Ctrl/Option/Command-click: toggle
        shift = bool(event.state & 0x0001)
        toggle = bool(event.state & 0x0004) or bool(event.state & 0x0008) or bool(event.state & 0x0010) or bool(event.state & 0x0040)

        ordered = [f.path for f in self.dropped]
        if shift and self._selection_anchor in ordered:
            a = ordered.index(self._selection_anchor)
            b = ordered.index(path)
            lo, hi = (a, b) if a <= b else (b, a)
            self._set_selected_paths(ordered[lo : hi + 1])
            return

        if toggle:
            current = list(self.selected_paths)
            if path in current:
                current = [p for p in current if p != path]
            else:
                current.append(path)
            self._selection_anchor = path
            self._set_selected_paths(current)
            return

        self._selection_anchor = path
        self._set_selected_paths([path])

    def _load_remove_icon(self) -> tkinter.PhotoImage:
        base_dir = _resource_base_dir()
        svg_path = base_dir / "assets" / "x.svg"

        # Try to render the SVG using CairoSVG (preferred).
        try:
            import cairosvg  # type: ignore

            svg_text = svg_path.read_text(encoding="utf-8")
            svg_text = svg_text.replace("rgba(0, 0, 0, 1)", "rgba(255, 0, 0, 1)")
            png_bytes = cairosvg.svg2png(
                bytestring=svg_text.encode("utf-8"),
                output_width=14,
                output_height=14,
            )
            if not isinstance(png_bytes, (bytes, bytearray)):
                raise TypeError("SVG render did not return bytes")
            png_b64 = base64.b64encode(png_bytes).decode("ascii")
            return tkinter.PhotoImage(data=png_b64)
        except Exception:
            pass

        # Fallback: draw a small red X (keeps UI functional without SVG support).
        img = tkinter.PhotoImage(width=14, height=14)
        red = "#ff0000"
        for i in range(14):
            img.put(red, (i, i))
            img.put(red, (13 - i, i))
        return img

    def _refresh_result_list(self) -> None:
        # Clear existing rows
        for child in list(self.result_list_frame.winfo_children()):
            child.destroy()
        self._result_rows.clear()

        # Keep selection valid
        if self.selected_result_index is not None:
            if self.selected_result_index < 0 or self.selected_result_index >= len(self.results):
                self.selected_result_index = None

        width = self.result_canvas.winfo_width() if hasattr(self, "result_canvas") else 0
        if width <= 0:
            width = 900

        row_h = 34
        for idx, _res in enumerate(self.results):
            row_canvas = tkinter.Canvas(
                self.result_list_frame,
                height=row_h,
                bg=self._result_bg,
                highlightthickness=0,
                bd=0,
            )
            row_canvas.grid(row=idx, column=0, sticky="ew")

            t_name = row_canvas.create_text(0, int(row_h / 2), anchor="w", text="", fill=self._result_text, font=self.font_normal)
            t_target = row_canvas.create_text(0, int(row_h / 2), anchor="center", text="", fill=self._result_muted, font=self.font_normal)
            t_output = row_canvas.create_text(0, int(row_h / 2), anchor="w", text="", fill=self._result_muted, font=self.font_normal)
            sep = row_canvas.create_line(10, row_h - 1, width - 10, row_h - 1, fill="#2c2c2e")

            row_data: dict[str, object] = {
                "index": idx,
                "canvas": row_canvas,
                "t_name": t_name,
                "t_target": t_target,
                "t_output": t_output,
                "sep": sep,
            }
            self._result_rows.append(row_data)

            def _click(_e, i=idx) -> None:
                self._on_result_row_click(i)

            row_canvas.bind("<Button-1>", _click)

            # Drag-out support per row
            try:
                row_canvas.drag_source_register(DND_FILES)  # type: ignore[attr-defined]
                row_canvas.dnd_bind("<<DragInitCmd>>", self._on_result_drag_init)  # type: ignore[attr-defined]
            except Exception:
                pass

        self._layout_result_rows(width)
        self._refresh_result_row_visuals()

    def _refresh_convert_progress(self) -> None:
        self._refresh_row_visuals()
        self._update_job_bar()

    def _update_job_bar(self) -> None:
        active = list(self._in_progress)
        if not active:
            try:
                self.job_bar.grid_remove()
            except Exception:
                pass
            return

        targets = {self._target_by_path.get(p) for p in active}
        targets.discard(None)
        if len(targets) == 1:
            target_txt = f" to {next(iter(targets))}"
        else:
            target_txt = ""

        total = len(active)
        avg = 0.0
        if total:
            avg = sum(float(self._display_progress.get(p, self._progress.get(p, 0.0))) for p in active) / float(total)

        # If progress stops updating near the end (common while ffmpeg finalizes/muxes),
        # communicate that explicitly rather than looking frozen.
        now = time.time()
        last_ts = 0.0
        for p in active:
            last_ts = max(last_ts, float(self._last_progress_ts.get(p, 0.0)))
        age_s = max(0, int(now - last_ts)) if last_ts else 0

        label = f"Converting {total} file(s){target_txt}"
        if avg >= 0.90 and age_s >= 10:
            label = f"Finalizing {total} file(s){target_txt} (no new progress for {age_s}s)"

        self.job_bar_label.configure(text=label)
        self.job_bar_progress.configure(value=max(0.0, min(100.0, avg * 100.0)))
        self.job_bar.grid()

    def _start_progress_animation(self) -> None:
        if self._animate_active:
            return
        self._animate_active = True
        self.root.after(60, self._tick_progress_animation)

    def _tick_progress_animation(self) -> None:
        try:
            # Periodic refresh while jobs are running.
            # NOTE: We intentionally do NOT "smooth fill" progress here because it makes
            # real stalls look like they're stuck at ~95%.
            active = bool(self._in_progress)
            if active:
                for p in list(self._in_progress):
                    target = float(self._progress.get(p, 0.0))
                    current = float(self._display_progress.get(p, target))
                    if current > target:
                        current = target
                    # Ease toward target (monotonic) for smoother visuals.
                    current = current + (target - current) * 0.35
                    if current > target:
                        current = target
                    self._display_progress[p] = current
            self._refresh_convert_progress()
            if active:
                self.root.after(60, self._tick_progress_animation)
            else:
                self._animate_active = False
        except Exception as e:
            self._on_tk_exception(type(e), e, e.__traceback__)
            # Keep the loop alive even after an error.
            self.root.after(120, self._tick_progress_animation)

    def _conversion_worker(self) -> None:
        while True:
            src_path, target_ext = self._task_queue.get()
            try:
                # Predict output paths so the user can find ffmpeg logs even if the job hangs.
                expected_out = None
                expected_ffmpeg_log = None
                try:
                    preview = utils.FileConverter(self._output_dir)
                    preview.ensureOutputPath()
                    expected_out = preview.buildOutputPath(src_path, target_ext)
                    expected_ffmpeg_log = expected_out + ".ffmpeg.log"
                except Exception:
                    pass

                self._debug_log(
                    f"WORKER start: src={src_path} target={target_ext}"
                    + (f" out={expected_out}" if expected_out else "")
                    + (f" ffmpeg_log={expected_ffmpeg_log}" if expected_ffmpeg_log else "")
                )

                # Best-effort progress callback (may only update in coarse steps).
                last_logged = 0.0
                last_log_t = time.time()
                def _progress_cb(v: float) -> None:
                    self._ui_events.put(("progress", src_path, float(v)))
                    nonlocal last_logged, last_log_t
                    now = time.time()
                    vv = float(v)
                    # Throttle to avoid huge logs.
                    if vv >= 1.0 or (vv - last_logged) >= 0.05 or (now - last_log_t) >= 5.0:
                        last_logged = max(last_logged, vv)
                        last_log_t = now
                        self._debug_log(f"WORKER progress: src={src_path} v={vv:.3f}")

                out_path = utils.convertFile(src_path, target_ext, self._output_dir, progress=_progress_cb)
                self._ui_events.put(("done", src_path, out_path, target_ext))
                self._debug_log(f"WORKER done: src={src_path} out={out_path}")
            except Exception as e:
                self._ui_events.put(("error", src_path, str(e)))
                self._debug_log(f"WORKER error: src={src_path} err={e}")
            finally:
                self._task_queue.task_done()

    def _process_ui_events(self) -> None:
        try:
            # Drain UI events from worker thread.
            changed = False
            while True:
                try:
                    evt = self._ui_events.get_nowait()
                except queue.Empty:
                    break
                kind = evt[0]
                if kind == "progress":
                    _k, path, v = evt
                    self._progress[path] = max(float(self._progress.get(path, 0.0)), float(v))
                    disp = float(self._display_progress.get(path, 0.0))
                    if disp > self._progress[path]:
                        disp = self._progress[path]
                    self._display_progress[path] = disp
                    self._last_progress_ts[path] = time.time()
                    changed = True
                elif kind == "done":
                    _k, path, out_path, target_ext = evt
                    if path in self._in_progress:
                        self._in_progress.remove(path)
                    # Keep green fill after completion.
                    self._progress[path] = 1.0
                    self._display_progress[path] = 1.0
                    self._last_progress_ts[path] = time.time()
                    dropped = self._find_dropped_by_path(path)
                    self.results.append(
                        ConversionResultItem(
                            source_path=path,
                            source_name=(dropped.name if dropped else Path(path).name),
                            output_path=str(out_path),
                            target_ext=str(target_ext),
                        )
                    )
                    changed = True
                    self._refresh_result_list()
                    self._start_progress_animation()
                elif kind == "error":
                    _k, path, _msg = evt
                    if path in self._in_progress:
                        self._in_progress.remove(path)
                    # Keep whatever progress was last shown.
                    self._last_progress_ts[path] = time.time()
                    name = Path(path).name
                    msg = str(_msg).strip() or "Unknown error"
                    self._debug_log(f"UI error: src={path} msg={msg}")
                    # Show an error dialog so "freezes" are diagnosable.
                    try:
                        messagebox.showerror(
                            "Conversion failed",
                            f"{name}\n\n{msg}\n\nDebug log: {self._debug_log_path}",
                        )
                    except Exception:
                        # If the dialog fails for any reason, still emit to console.
                        pass
                    print(f"[Convertable] Conversion failed: {name}\n{msg}")
                    changed = True

            if changed:
                self._refresh_convert_progress()
        except Exception as e:
            self._on_tk_exception(type(e), e, e.__traceback__)
        finally:
            self.root.after(60, self._process_ui_events)

    def _on_tk_exception(self, exc, val, tb) -> None:
        try:
            import traceback

            text = "".join(traceback.format_exception(exc, val, tb))
        except Exception:
            text = f"{exc}: {val}"
        print("[Convertable] Tk callback exception:\n" + text)

    def run(self) -> None:
        self.root.mainloop()

def create_window() -> None:
    ConvertableApp().run()

if __name__ == "__main__":
    create_window()