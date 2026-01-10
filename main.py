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
from tkinter import filedialog

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

        # Pending conversion tasks (reorderable for queue prioritization).
        self._pending_tasks: list[tuple[str, str]] = []
        self._pending_cv = threading.Condition()
        self._ui_events: queue.Queue[tuple] = queue.Queue()
        self._shutdown = threading.Event()
        self._in_progress: set[str] = set()
        self._progress: dict[str, float] = {}
        self._display_progress: dict[str, float] = {}
        self._target_by_path: dict[str, str] = {}
        self._animate_active: bool = False
        self._last_progress_ts: dict[str, float] = {}
        self._job_started_ts: dict[str, float] = {}

        # Parallel execution control (sequential by default).
        self._parallel_limit: int = 1
        self._active_conversions: int = 0
        # One-shot parallel: enabled when user drops onto the current job.
        # Reverts to sequential as soon as one of the parallel jobs finishes.
        self._parallel_one_shot: bool = False
        self._parallel_engaged: bool = False

        # Queue/Job card state
        self._job_bar_collapsed: bool = False
        self._queue_paths: set[str] = set()
        self._queue_order: list[str] = []
        self._queue_done: set[str] = set()
        self._queue_failed: set[str] = set()
        self._current_job_path: str | None = None
        self._current_job_target: str | None = None
        self._queue_drag_from: int | None = None
        self._queue_drag_to: int | None = None
        self._queue_drag_ghost: list[int] = []
        self._queue_drag_ghost_text: str = ""
        self._queue_drag_over_current: bool = False

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

        # One always-on worker; an extra worker is spawned on-demand for parallel mode.
        self._worker: threading.Thread = threading.Thread(target=self._conversion_worker, daemon=True)
        self._extra_worker: threading.Thread | None = None
        self._worker.start()
        self.root.after(60, self._process_ui_events)

        # If any Tk callback raises, Tk will print to stderr and that scheduled loop may stop.
        # Capture those exceptions so periodic polling/animation can't silently die.
        self.root.report_callback_exception = self._on_tk_exception

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.drop_frame = ttk.Frame(self.notebook)
        self.convert_frame = ttk.Frame(self.notebook)
        self.queue_frame = ttk.Frame(self.notebook)
        self.result_frame = ttk.Frame(self.notebook)

        self.notebook.add(self.drop_frame, text="Drop")
        self.notebook.add(self.convert_frame, text="Convert")
        self.notebook.add(self.queue_frame, text="Queue")
        self.notebook.add(self.result_frame, text="Result")

        self._build_drop_tab()
        self._build_convert_tab()
        self._build_queue_tab()
        self._build_result_tab()

        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _source_category(self, f: DroppedFile) -> str | None:
        # None means unsupported / not convertible.
        if f.mime == "inode/directory":
            return None
        ext = Path(f.path).suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".bmp", ".tiff", ".tif", ".gif", ".svg"}:
            return "image"
        if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"}:
            return "audio"
        if ext in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
            return "video"

        mime = (f.mime or "").lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"

        return None

    def _is_source_supported(self, f: DroppedFile) -> bool:
        return self._source_category(f) is not None

    def _on_close(self) -> None:
        try:
            self._debug_log("App closing")
            try:
                self._shutdown.set()
            except Exception:
                pass

            # Kill any active ffmpeg processes promptly.
            try:
                utils.terminate_active_processes()
            except Exception:
                pass

            # Wake workers so they can notice shutdown.
            try:
                with self._pending_cv:
                    self._pending_cv.notify_all()
            except Exception:
                pass
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

        # Queue/job stat card (always visible + collapsible)
        self.job_bar = ttk.Frame(self.convert_frame)
        self.job_bar.grid(row=0, column=0, sticky="ew")
        self.job_bar.columnconfigure(0, weight=1)

        header = ttk.Frame(self.job_bar)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header.columnconfigure(0, weight=1)

        self.job_bar_label = ttk.Label(header, text="Queue")
        self.job_bar_label.grid(row=0, column=0, sticky="w")

        self.job_bar_toggle = ttk.Button(header, text="Hide", width=7, command=self._toggle_job_bar)
        self.job_bar_toggle.grid(row=0, column=1, sticky="e")

        self.job_bar_body = ttk.Frame(self.job_bar)
        self.job_bar_body.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.job_bar_body.columnconfigure(0, weight=1)

        self.job_bar_stats = ttk.Label(self.job_bar_body, text="")
        self.job_bar_stats.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.job_bar_current = ttk.Label(self.job_bar_body, text="")
        self.job_bar_current.grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.job_bar_progress = ttk.Progressbar(self.job_bar_body, orient="horizontal", mode="determinate", maximum=100)
        self.job_bar_progress.grid(row=2, column=0, sticky="ew")

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
            ".MP3",
            ".WAV",
            ".M4A",
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
        self._update_job_bar()

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
        self._layout_convert_rows(event.width)
        self._refresh_row_visuals()

    def _layout_convert_rows(self, width: int) -> None:
        for row in self._convert_rows.values():
            c = row.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                continue
            c.configure(width=width)
            self._render_convert_row(row, width)

    def _render_convert_row(self, row: dict[str, object], width: int) -> None:
        c = row.get("canvas")
        if not isinstance(c, tkinter.Canvas):
            return
        path = row.get("path")
        if not isinstance(path, str):
            return

        dropped = self._find_dropped_by_path(path)
        if dropped is None:
            return

        def _item_exists(item_id: int) -> bool:
            try:
                return bool(c.type(item_id))
            except Exception:
                return False

        def _safe_delete(item_id: int) -> None:
            try:
                c.delete(item_id)
            except Exception:
                pass

        def _safe_lower(item_id: int) -> None:
            try:
                if _item_exists(item_id):
                    c.tag_lower(item_id)
            except Exception:
                pass

        def _safe_raise(item_id: int, above: int | None = None) -> None:
            try:
                if not _item_exists(item_id):
                    return
                if above is not None and _item_exists(above):
                    c.tag_raise(item_id, above)
                else:
                    c.tag_raise(item_id)
            except Exception:
                pass

        def _safe_coords(item_id: int, *coords: int) -> None:
            try:
                if _item_exists(item_id):
                    c.coords(item_id, *coords)
            except Exception:
                pass

        def _safe_itemconfigure(item_id: int, **kwargs) -> None:
            try:
                if _item_exists(item_id):
                    c.itemconfigure(item_id, **kwargs)
            except Exception:
                pass

        def _delete_item_or_items(v: object) -> None:
            if isinstance(v, int):
                _safe_delete(v)
                return
            if isinstance(v, (list, tuple)):
                for it in v:
                    if isinstance(it, int):
                        _safe_delete(it)

        def _draw_linked_fill(
            x1: int,
            y1: int,
            x2: int,
            y2: int,
            r: int,
            fill: str,
            round_top: bool,
            round_bottom: bool,
        ) -> list[int]:
            ids: list[int] = []
            if x2 <= x1 or y2 <= y1:
                return ids
            if r <= 0 or (not round_top and not round_bottom):
                rect = c.create_rectangle(x1, y1, x2, y2, fill=fill, outline="")
                return [rect]

            rr = self._rounded_rect(c, x1, y1, x2, y2, r, fill=fill, outline="")
            ids.append(rr)
            if round_top and not round_bottom:
                # Square the bottom corners by overdrawing from below the top radius.
                mask = c.create_rectangle(x1, y1 + r, x2, y2, fill=fill, outline="")
                ids.append(mask)
            elif (not round_top) and round_bottom:
                # Square the top corners by overdrawing up to above the bottom radius.
                mask = c.create_rectangle(x1, y1, x2, y2 - r, fill=fill, outline="")
                ids.append(mask)
            return ids

        selected_set = set(self.selected_paths)
        is_selected = path in selected_set

        row_h = int(c.cget("height"))
        pad_l = 10
        # Keep the highlight/progress bar nearly full-width, but inset the
        # right-aligned content so it doesn't sit on the bar edge.
        bar_pad_r = 10
        content_pad_r = 30
        pad_y = 4
        radius = 8
        gap = 12

        idx = row.get("idx")
        if not isinstance(idx, int):
            idx = -1
        prev_selected = False
        next_selected = False
        if 0 <= idx < len(self.dropped):
            if idx - 1 >= 0:
                prev_selected = self.dropped[idx - 1].path in selected_set
            if idx + 1 < len(self.dropped):
                next_selected = self.dropped[idx + 1].path in selected_set

        # Columns (right-aligned): remove | size | mime | ext
        ext_w = int(self._min_ext_px)
        mime_w = int(self._min_mime_px)
        size_w = int(self._min_size_px)
        remove_w = int(self._min_remove_px)

        ext_left = max(pad_l, width - content_pad_r - ext_w)
        mime_left = max(pad_l, ext_left - gap - mime_w)
        size_left = max(pad_l, mime_left - gap - size_w)
        remove_left = max(pad_l, size_left - gap - remove_w)

        name_x = pad_l + 10
        name_right = max(name_x + 60, remove_left - gap)
        name_w = max(60, name_right - name_x)

        # Progress value (drawn as an inset rounded bar, not as a full-row background)
        real_p = float(self._progress.get(path, 0.0))
        disp_p = float(self._display_progress.get(path, real_p))
        p = max(0.0, min(1.0, disp_p))

        # Selection shape (blue highlight)
        sel_v = row.get("sel")
        if sel_v is not None:
            _delete_item_or_items(sel_v)
            row.pop("sel", None)
        if is_selected:
            x1 = pad_l
            x2 = max(pad_l + 1, width - bar_pad_r)
            y1 = 0 if prev_selected else pad_y
            y2 = row_h if next_selected else max(pad_y + 1, row_h - pad_y)
            sel_ids = _draw_linked_fill(
                x1,
                y1,
                x2,
                y2,
                radius,
                self._result_selected_bg,
                round_top=(not prev_selected),
                round_bottom=(not next_selected),
            )
            row["sel"] = sel_ids
            for it in sel_ids:
                _safe_lower(it)

        # Progress bar (green), same geometry as the rounded selection.
        prog_v = row.get("prog")
        if prog_v is not None:
            _delete_item_or_items(prog_v)
            row.pop("prog", None)

        if p > 0.0:
            inner_left = pad_l
            inner_right = max(inner_left + 1, width - bar_pad_r)
            inner_top = 0 if (is_selected and prev_selected) else pad_y
            inner_bottom = row_h if (is_selected and next_selected) else max(inner_top + 1, row_h - pad_y)

            track_w = max(0, inner_right - inner_left)
            fill_w = int(track_w * p)
            if fill_w > 0 and track_w > 0:
                # Tk canvas has no alpha; simulate ~50% opacity by blending.
                opacity = 1
                if is_selected:
                    opacity = 0.8
                base = self._selected_bg if is_selected else self._normal_bg
                fill_color = self._blend_hex(self._progress_bg, base, opacity)
                r = min(radius, int(fill_w / 2), int((inner_bottom - inner_top) / 2))
                prog_ids = _draw_linked_fill(
                    inner_left,
                    inner_top,
                    inner_left + fill_w,
                    inner_bottom,
                    r,
                    fill_color,
                    round_top=(not (is_selected and prev_selected)),
                    round_bottom=(not (is_selected and next_selected)),
                )
                row["prog"] = prog_ids

                above = None
                sel_now = row.get("sel")
                if isinstance(sel_now, list) and sel_now:
                    above = sel_now[-1]
                elif isinstance(sel_now, int):
                    above = sel_now
                for it in prog_ids:
                    _safe_raise(it, above)

        # Text colors
        is_supported = self._is_source_supported(dropped)
        if is_selected:
            name_color = self._convert_selected_text
            muted = self._convert_selected_text
        elif not is_supported:
            name_color = self._convert_muted
            muted = self._convert_muted
        else:
            name_color = self._convert_text
            muted = self._convert_muted

        name = self._ellipsize(dropped.name, name_w - 10)
        size_txt = _human_size(dropped.size_bytes)
        mime_txt = self._ellipsize(dropped.mime, mime_w - 10)
        ext_txt = dropped.ext

        # Update canvas items
        t_name = row.get("t_name")
        t_size = row.get("t_size")
        t_mime = row.get("t_mime")
        t_ext = row.get("t_ext")
        img_remove = row.get("i_remove")

        if isinstance(t_name, int):
            _safe_coords(t_name, name_x, int(row_h / 2))
            _safe_itemconfigure(t_name, text=name, fill=name_color)
        if isinstance(t_size, int):
            _safe_coords(t_size, size_left + size_w, int(row_h / 2))
            _safe_itemconfigure(t_size, text=size_txt, fill=muted)
        if isinstance(t_mime, int):
            _safe_coords(t_mime, mime_left + mime_w, int(row_h / 2))
            _safe_itemconfigure(t_mime, text=mime_txt, fill=muted)
        if isinstance(t_ext, int):
            _safe_coords(t_ext, ext_left + ext_w, int(row_h / 2))
            _safe_itemconfigure(t_ext, text=ext_txt, fill=muted)
        if isinstance(img_remove, int):
            _safe_coords(img_remove, remove_left + int(remove_w / 2), int(row_h / 2))

        # Keep foreground items above progress/selection.
        for key in ("t_name", "t_size", "t_mime", "t_ext", "i_remove"):
            item = row.get(key)
            if isinstance(item, int):
                _safe_raise(item)

        # Separator line
        sep = row.get("sep")
        if isinstance(sep, int):
            if is_selected and next_selected:
                _safe_itemconfigure(sep, state="hidden")
            else:
                _safe_itemconfigure(sep, state="normal")
                _safe_coords(sep, pad_l, row_h - 1, width - bar_pad_r, row_h - 1)

    @staticmethod
    def _blend_hex(fg: str, bg: str, alpha: float) -> str:
        """Blend fg over bg with alpha in [0..1] and return #RRGGBB."""

        def _parse(h: str) -> tuple[int, int, int]:
            s = h.strip()
            if s.startswith("#"):
                s = s[1:]
            if len(s) != 6:
                return (0, 0, 0)
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))

        a = max(0.0, min(1.0, float(alpha)))
        fr, fg_g, fb = _parse(fg)
        br, bg_g, bb = _parse(bg)

        r = int(round(fr * a + br * (1.0 - a)))
        g = int(round(fg_g * a + bg_g * (1.0 - a)))
        b = int(round(fb * a + bb * (1.0 - a)))
        return f"#{r:02x}{g:02x}{b:02x}"

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
            if f and self._is_source_supported(f):
                self._set_action_enabled(True)
                self._set_convert_options_for_kind(f.mime)
            else:
                self._set_action_enabled(False)
        else:
            self.selected_file_label.configure(text=f"{len(self.selected_paths)} files selected")
            cats: list[str | None] = []
            for p in self.selected_paths:
                f = self._find_dropped_by_path(p)
                cats.append(self._source_category(f) if f else None)

            if any(c is None for c in cats):
                self._set_action_enabled(False)
            else:
                unique = {c for c in cats if c is not None}
                if len(unique) != 1:
                    self._set_action_enabled(False)
                else:
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

        # Canvas-based Convert rows (Finder-like styling)
        c = widgets.get("canvas")
        if isinstance(c, tkinter.Canvas):
            width = self.convert_canvas.winfo_width() if hasattr(self, "convert_canvas") else 0
            if width <= 0:
                width = 900
            self._render_convert_row(widgets, width)
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
            if is_selected:
                # Selection highlight takes precedence over progress fill.
                fill.place_forget()
            elif filled_px <= 0:
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
        cats: list[str | None] = []
        for path in selected_paths:
            f = self._find_dropped_by_path(path)
            cats.append(self._source_category(f) if f else None)

        if cats and all(c == "image" for c in cats):
            self._set_convert_options_for_kind("image/")
            return
        if cats and all(c == "audio" for c in cats):
            self._set_convert_options_for_kind("audio/")
            return
        if cats and all(c == "video" for c in cats):
            self._set_convert_options_for_kind("video/")
            return
        self._set_convert_options_for_kind("application/octet-stream")

    def _set_convert_options_for_kind(self, mime: str) -> None:
        if mime.startswith("image/"):
            options = [".PNG", ".JPEG", ".WEBP"]
        elif mime.startswith("audio/"):
            options = [".MP3", ".WAV", ".M4A"]
        elif mime.startswith("video/"):
            options = [".MP4", ".MOV"]
        else:
            options = [".PNG", ".JPEG", ".WEBP", ".MP3", ".WAV", ".M4A", ".MP4", ".MOV"]
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

        # Remove any pending tasks for these paths.
        try:
            with self._pending_cv:
                self._pending_tasks = [(p, t) for (p, t) in self._pending_tasks if p not in paths]
        except Exception:
            pass

        for p in paths:
            self._in_progress.discard(p)
            self._progress.pop(p, None)
            self._display_progress.pop(p, None)
            self._target_by_path.pop(p, None)
            self._last_progress_ts.pop(p, None)
            self._job_started_ts.pop(p, None)
            self._queue_paths.discard(p)
            self._queue_done.discard(p)
            self._queue_failed.discard(p)
            if p in self._queue_order:
                try:
                    self._queue_order = [x for x in self._queue_order if x != p]
                except Exception:
                    pass
        self._refresh_all_lists()
        self._refresh_queue_list()
        self._update_job_bar()

    def _queue_conversion(self) -> None:
        sel = list(self.selected_paths)
        if not sel:
            return
        target_ext = self.convert_to_var.get().strip().upper()
        if not target_ext.startswith("."):
            target_ext = "." + target_ext

        # If nothing is currently active, start a fresh batch.
        if not self._in_progress:
            self._queue_paths.clear()
            self._queue_order.clear()
            self._queue_done.clear()
            self._queue_failed.clear()
            self._current_job_path = None
            self._current_job_target = None

        for src_path in sel:
            if src_path in self._in_progress:
                continue
            dropped_file = self._find_dropped_by_path(src_path)
            if dropped_file is None:
                continue
            if not self._is_source_supported(dropped_file):
                continue
            self._in_progress.add(src_path)
            self._progress[src_path] = 0.0
            self._display_progress[src_path] = 0.0
            self._target_by_path[src_path] = target_ext
            now = time.time()
            self._last_progress_ts[src_path] = now
            self._job_started_ts[src_path] = now
            with self._pending_cv:
                self._pending_tasks.append((src_path, target_ext))
                self._pending_cv.notify_all()

            # Queue stats tracking
            self._queue_paths.add(src_path)
            if src_path not in self._queue_order:
                self._queue_order.append(src_path)

        self._sync_queue_order_for_processing()

        # Keep user on this page; show progress fill.
        self._start_progress_animation()
        self._refresh_convert_progress()
        self._refresh_queue_list()
        self._update_job_bar()

    def _toggle_job_bar(self) -> None:
        self._job_bar_collapsed = not self._job_bar_collapsed
        self._update_job_bar()

    # -------------------- Queue Tab --------------------
    def _build_queue_tab(self) -> None:
        self.queue_frame.rowconfigure(1, weight=1)
        self.queue_frame.columnconfigure(0, weight=1)

        # Queue/job stat card (same stats as Convert tab)
        self.queue_bar = ttk.Frame(self.queue_frame)
        self.queue_bar.grid(row=0, column=0, sticky="ew")
        self.queue_bar.columnconfigure(0, weight=1)

        header = ttk.Frame(self.queue_bar)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header.columnconfigure(0, weight=1)

        self.queue_bar_label = ttk.Label(header, text="Queue")
        self.queue_bar_label.grid(row=0, column=0, sticky="w")

        self.queue_bar_toggle = ttk.Button(header, text="Hide", width=7, command=self._toggle_job_bar)
        self.queue_bar_toggle.grid(row=0, column=1, sticky="e")

        self.queue_bar_body = ttk.Frame(self.queue_bar)
        self.queue_bar_body.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.queue_bar_body.columnconfigure(0, weight=1)

        self.queue_bar_stats = ttk.Label(self.queue_bar_body, text="")
        self.queue_bar_stats.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.queue_bar_current = ttk.Label(self.queue_bar_body, text="")
        self.queue_bar_current.grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.queue_bar_progress = ttk.Progressbar(self.queue_bar_body, orient="horizontal", mode="determinate", maximum=100)
        self.queue_bar_progress.grid(row=2, column=0, sticky="ew")

        # Reorderable queued-items list (Convert-like UI)
        list_host = ttk.Frame(self.queue_frame)
        list_host.grid(row=1, column=0, sticky="nsew")
        list_host.rowconfigure(0, weight=1)
        list_host.columnconfigure(0, weight=1)

        self.queue_canvas = tkinter.Canvas(list_host, highlightthickness=0)
        self.queue_canvas.grid(row=0, column=0, sticky="nsew")
        self.queue_canvas.configure(bg=self._normal_bg)
        self.queue_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.queue_canvas.yview)
        self.queue_scroll.grid(row=0, column=1, sticky="ns")
        self.queue_canvas.configure(yscrollcommand=self.queue_scroll.set)

        self.queue_list_frame = tkinter.Frame(self.queue_canvas, bg=self._normal_bg)
        self.queue_list_frame.columnconfigure(0, weight=1)
        self._queue_list_window = self.queue_canvas.create_window((0, 0), window=self.queue_list_frame, anchor="nw")

        self.queue_list_frame.bind("<Configure>", self._on_queue_list_configure)
        self.queue_canvas.bind("<Configure>", self._on_queue_canvas_configure)

        # Mouse wheel scrolling (trackpad included). Bind only while cursor is over the list.
        self.queue_canvas.bind("<Enter>", self._bind_queue_mousewheel)
        self.queue_canvas.bind("<Leave>", self._unbind_queue_mousewheel)

        self._refresh_queue_list()
        self._update_job_bar()

    def _on_queue_list_configure(self, _event=None) -> None:
        try:
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))
        except Exception:
            pass

    def _bind_queue_mousewheel(self, _event=None) -> None:
        self.root.bind_all("<MouseWheel>", self._on_queue_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_queue_mousewheel)
        self.root.bind_all("<Button-5>", self._on_queue_mousewheel)

    def _unbind_queue_mousewheel(self, _event=None) -> None:
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_queue_mousewheel(self, event) -> None:
        if getattr(self, "queue_canvas", None) is None:
            return
        if getattr(event, "num", None) == 4:
            self.queue_canvas.yview_scroll(-1, "units")
            return
        if getattr(event, "num", None) == 5:
            self.queue_canvas.yview_scroll(1, "units")
            return

        delta = getattr(event, "delta", 0)
        if delta == 0:
            return
        direction = -1 if delta > 0 else 1
        steps = 1
        if sys.platform.startswith("win"):
            steps = max(1, int(abs(delta) / 120))
        self.queue_canvas.yview_scroll(direction * steps, "units")

    def _on_queue_canvas_configure(self, event) -> None:
        try:
            self.queue_canvas.itemconfigure(self._queue_list_window, width=event.width)
        except Exception:
            pass
        self._layout_queue_rows(event.width)

    def _layout_queue_rows(self, width: int) -> None:
        rows = getattr(self, "_queue_rows", None)
        if not isinstance(rows, list):
            return
        for row in rows:
            c = row.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                continue
            try:
                c.configure(width=width)
            except Exception:
                pass
            self._render_queue_row(row, width)

    def _render_queue_row(self, row: dict[str, object], width: int) -> None:
        c = row.get("canvas")
        if not isinstance(c, tkinter.Canvas):
            return
        path = row.get("path")
        if not isinstance(path, str):
            return

        def _item_exists(item_id: int) -> bool:
            try:
                return bool(c.type(item_id))
            except Exception:
                return False

        def _safe_delete(item_id: int) -> None:
            try:
                c.delete(item_id)
            except Exception:
                pass

        def _safe_raise(item_id: int, above: int | None = None) -> None:
            try:
                if not _item_exists(item_id):
                    return
                if above is not None and _item_exists(above):
                    c.tag_raise(item_id, above)
                else:
                    c.tag_raise(item_id)
            except Exception:
                pass

        def _safe_lower(item_id: int) -> None:
            try:
                if _item_exists(item_id):
                    c.tag_lower(item_id)
            except Exception:
                pass

        def _safe_coords(item_id: int, *coords: int) -> None:
            try:
                if _item_exists(item_id):
                    c.coords(item_id, *coords)
            except Exception:
                pass

        def _safe_itemconfigure(item_id: int, **kwargs) -> None:
            try:
                if _item_exists(item_id):
                    c.itemconfigure(item_id, **kwargs)
            except Exception:
                pass

        def _delete_item_or_items(v: object) -> None:
            if isinstance(v, int):
                _safe_delete(v)
                return
            if isinstance(v, (list, tuple)):
                for it in v:
                    if isinstance(it, int):
                        _safe_delete(it)

        def _draw_linked_fill(
            x1: int,
            y1: int,
            x2: int,
            y2: int,
            r: int,
            fill: str,
        ) -> list[int]:
            ids: list[int] = []
            if x2 <= x1 or y2 <= y1:
                return ids
            if r <= 0:
                rect = c.create_rectangle(x1, y1, x2, y2, fill=fill, outline="")
                return [rect]
            rr = self._rounded_rect(c, x1, y1, x2, y2, r, fill=fill, outline="")
            ids.append(rr)
            # Ensure full coverage (rounded rect draws arcs)
            mask = c.create_rectangle(x1, y1, x2, y2, fill=fill, outline="")
            ids.append(mask)
            return ids

        row_h = int(c.cget("height"))
        pad_l = 10
        pad_y = 4
        radius = 8
        bar_pad_r = 10
        content_pad_r = 20

        is_current = self._current_job_path is not None and str(self._current_job_path) == path

        # Progress background bar
        real_p = float(self._progress.get(path, 0.0))
        disp_p = float(self._display_progress.get(path, real_p))
        p = max(0.0, min(1.0, disp_p))

        prog_v = row.get("prog")
        if prog_v is not None:
            _delete_item_or_items(prog_v)
            row.pop("prog", None)

        if p > 0.0:
            inner_left = pad_l
            inner_right = max(inner_left + 1, width - bar_pad_r)
            inner_top = pad_y
            inner_bottom = max(inner_top + 1, row_h - pad_y)
            track_w = max(0, inner_right - inner_left)
            fill_w = int(track_w * p)
            if fill_w > 0 and track_w > 0:
                base = self._result_selected_bg if is_current else self._normal_bg
                fill_color = self._blend_hex(self._progress_bg, base, 0.85 if is_current else 1.0)
                r = min(radius, int(fill_w / 2), int((inner_bottom - inner_top) / 2))
                prog_ids = _draw_linked_fill(
                    inner_left,
                    inner_top,
                    inner_left + fill_w,
                    inner_bottom,
                    r,
                    fill_color,
                )
                row["prog"] = prog_ids
                for it in prog_ids:
                    _safe_lower(it)

        # Current-job highlight (blue pill like selection)
        sel_v = row.get("sel")
        if sel_v is not None:
            _delete_item_or_items(sel_v)
            row.pop("sel", None)
        if is_current:
            x1 = pad_l
            x2 = max(pad_l + 1, width - bar_pad_r)
            y1 = pad_y
            y2 = max(pad_y + 1, row_h - pad_y)
            sel_ids = _draw_linked_fill(x1, y1, x2, y2, radius, self._result_selected_bg)
            row["sel"] = sel_ids
            for it in sel_ids:
                _safe_lower(it)

        # Text layout
        name_x = pad_l + 10
        right_x = max(name_x + 60, width - content_pad_r)
        max_name_w = max(60, right_x - name_x - 12)

        name = self._ellipsize(Path(path).name, max_name_w)
        t_name = row.get("t_name")
        t_status = row.get("t_status")
        sep = row.get("sep")

        if is_current:
            name_color = self._result_selected_text
            status_color = self._result_selected_text
        else:
            name_color = self._result_text
            status_color = self._result_muted

        if isinstance(t_name, int):
            _safe_coords(t_name, name_x, int(row_h / 2))
            _safe_itemconfigure(t_name, text=name, fill=name_color)
        if isinstance(t_status, int):
            _safe_coords(t_status, right_x, int(row_h / 2))
            _safe_itemconfigure(t_status, fill=status_color)

        # Keep text above fills
        if isinstance(t_name, int):
            _safe_raise(t_name)
        if isinstance(t_status, int):
            _safe_raise(t_status)

        # Separator line
        if isinstance(sep, int):
            _safe_itemconfigure(sep, state="normal")
            _safe_coords(sep, pad_l, row_h - 1, width - bar_pad_r, row_h - 1)

    def _sync_queue_order_for_processing(self) -> None:
        """Keep _queue_order aligned to actual processing order: running first, then pending."""
        try:
            with self._pending_cv:
                pending_paths = [p for (p, _t) in self._pending_tasks]
        except Exception:
            pending_paths = []

        running_paths = [p for p in self._queue_order if p in self._in_progress and p not in pending_paths]

        order: list[str] = []
        for p in running_paths:
            if p not in order:
                order.append(p)
        for p in pending_paths:
            if p not in order:
                order.append(p)

        # Preserve anything else we were tracking for this batch.
        for p in self._queue_order:
            if p not in order and p in self._queue_paths:
                order.append(p)

        self._queue_order = order

        # First running item is treated as the "current" row.
        self._current_job_path = running_paths[0] if running_paths else None

    def _queue_display_items(self) -> list[tuple[str, str, str]]:
        """Return (src_path, target_ext, status) in processing order (running first)."""
        items: list[tuple[str, str, str]] = []
        try:
            with self._pending_cv:
                pending = list(self._pending_tasks)
        except Exception:
            pending = []

        pending_paths = [p for (p, _t) in pending]
        running_paths = [p for p in self._queue_order if p in self._in_progress and p not in pending_paths]

        for p in running_paths:
            tgt = self._target_by_path.get(str(p), "")
            frac = float(self._display_progress.get(p, self._progress.get(p, 0.0)))
            frac = max(0.0, min(1.0, frac))
            pct = int(frac * 100.0)
            items.append((str(p), str(tgt or ""), f"Converting · {pct}%"))

        for src, tgt in pending:
            items.append((str(src), str(tgt), "Queued"))

        return items

    def _enable_parallel_for_batch(self) -> None:
        """Enable 2-way parallel processing for the current batch."""
        try:
            extra_to_start: threading.Thread | None = None
            with self._pending_cv:
                if self._parallel_limit < 2:
                    self._parallel_limit = 2
                self._parallel_one_shot = True
                self._parallel_engaged = False

                if self._extra_worker is None or (not self._extra_worker.is_alive()):
                    extra_to_start = threading.Thread(
                        target=self._conversion_worker,
                        kwargs={"is_extra": True},
                        daemon=True,
                    )
                    self._extra_worker = extra_to_start
                self._pending_cv.notify_all()

            if extra_to_start is not None:
                extra_to_start.start()
        except Exception:
            pass

    def _queue_drag_ghost_clear(self) -> None:
        canvas = getattr(self, "queue_canvas", None)
        if not isinstance(canvas, tkinter.Canvas):
            self._queue_drag_ghost = []
            self._queue_drag_ghost_text = ""
            return
        try:
            for it in list(self._queue_drag_ghost):
                try:
                    canvas.delete(it)
                except Exception:
                    pass
        finally:
            self._queue_drag_ghost = []
            self._queue_drag_ghost_text = ""

    def _queue_drag_ghost_show(self, text: str, y_canvas: float) -> None:
        canvas = getattr(self, "queue_canvas", None)
        if not isinstance(canvas, tkinter.Canvas):
            return

        row_h = 34
        pad_l = 10
        pad_y = 4
        bar_pad_r = 10

        try:
            w = int(canvas.winfo_width())
        except Exception:
            w = 0
        x1 = pad_l
        x2 = max(pad_l + 1, w - bar_pad_r)
        y1 = float(y_canvas) - (row_h / 2.0) + pad_y
        y2 = y1 + row_h - (pad_y * 2)
        if y2 <= y1:
            y2 = y1 + 1

        # Create once; then just move/update.
        if not self._queue_drag_ghost:
            try:
                rect = canvas.create_rectangle(x1, y1, x2, y2, fill=self._result_selected_bg, outline="")
                label = canvas.create_text(
                    x1 + 18,
                    (y1 + y2) / 2.0,
                    text=text,
                    anchor="w",
                    fill=self._result_selected_text,
                    font=self.font_normal,
                )
                self._queue_drag_ghost = [rect, label]
                self._queue_drag_ghost_text = text
            except Exception:
                self._queue_drag_ghost = []
                self._queue_drag_ghost_text = ""
                return
        else:
            rect = self._queue_drag_ghost[0]
            label = self._queue_drag_ghost[1] if len(self._queue_drag_ghost) > 1 else None
            try:
                canvas.coords(rect, x1, y1, x2, y2)
            except Exception:
                pass
            if isinstance(label, int):
                try:
                    canvas.coords(label, x1 + 18, (y1 + y2) / 2.0)
                except Exception:
                    pass
                if text != self._queue_drag_ghost_text:
                    try:
                        canvas.itemconfigure(label, text=text)
                    except Exception:
                        pass
                    self._queue_drag_ghost_text = text

        # Ensure it stays above the embedded list window.
        try:
            for it in self._queue_drag_ghost:
                canvas.tag_raise(it)
        except Exception:
            pass

    def _refresh_queue_list(self) -> None:
        frame = getattr(self, "queue_list_frame", None)
        if not isinstance(frame, tkinter.Frame):
            return

        self._sync_queue_order_for_processing()

        for child in list(frame.winfo_children()):
            child.destroy()

        display = self._queue_display_items()
        self._queue_rows: list[dict[str, object]] = []

        row_h = 34
        for row_idx, (src, tgt, status) in enumerate(display):
            c = tkinter.Canvas(
                frame,
                height=row_h,
                highlightthickness=0,
                bd=0,
                bg=self._normal_bg,
            )
            c.grid(row=row_idx, column=0, sticky="ew")

            # Text + separator (positions set in _render_queue_row)
            t_name = c.create_text(0, int(row_h / 2), text=Path(src).name, anchor="w", fill=self._result_text, font=self.font_normal)
            right_txt = f"→ {tgt}" if tgt else ""
            if status:
                right_txt = (right_txt + ("  " if right_txt else "") + status).strip()
            t_status = c.create_text(0, int(row_h / 2), text=right_txt, anchor="e", fill=self._result_muted, font=self.font_normal)
            sep = c.create_line(10, row_h - 1, 10, row_h - 1, fill="#2c2c2e")

            def _start_drag(ev, p=str(src)) -> str:
                self._on_queue_drag_start(ev, p)
                return "break"

            c.bind("<ButtonPress-1>", _start_drag)

            self._queue_rows.append(
                {
                    "idx": row_idx,
                    "path": str(src),
                    "target": str(tgt),
                    "status": str(status),
                    "canvas": c,
                    "prog": None,
                    "sel": None,
                    "t_name": t_name,
                    "t_status": t_status,
                    "sep": sep,
                }
            )

        try:
            self.queue_canvas.update_idletasks()
            self._layout_queue_rows(self.queue_canvas.winfo_width())
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))
        except Exception:
            pass

    def _on_queue_drag_start(self, _event=None, path: str | None = None) -> None:
        if not path:
            self._queue_drag_from = None
            return
        # The current converting item is always fixed at the top.
        if self._current_job_path and str(path) == str(self._current_job_path):
            self._queue_drag_from = None
            return
        try:
            with self._pending_cv:
                pending_paths = [p for (p, _t) in self._pending_tasks]
        except Exception:
            pending_paths = []

        # Allow dragging a single pending item when there is a running "current" job,
        # so the user can drop it onto the current row to trigger parallel mode.
        has_running_current = bool(self._current_job_path and (str(self._current_job_path) in self._in_progress))

        if len(pending_paths) == 0 or (len(pending_paths) == 1 and not has_running_current):
            # Nothing meaningful to reorder.
            try:
                self.root.bell()
            except Exception:
                pass
            self._debug_log(f"QUEUE DRAG ignored: pending_len={len(pending_paths)}")
            self._queue_drag_from = None
            self._queue_drag_to = None
            return

        if str(path) not in pending_paths:
            self._queue_drag_from = None
            return

        self._queue_drag_from = pending_paths.index(str(path))
        self._queue_drag_to = self._queue_drag_from
        self._queue_drag_over_current = False

        self._debug_log(f"QUEUE DRAG start: path={path} from={self._queue_drag_from} pending_len={len(pending_paths)}")

        # Create ghost label under the cursor.
        try:
            canvas = getattr(self, "queue_canvas", None)
            if isinstance(canvas, tkinter.Canvas) and _event is not None:
                y_root = getattr(_event, "y_root", None)
                if y_root is None:
                    y_root = self.root.winfo_pointery()
                y = int(y_root) - int(canvas.winfo_rooty())
                y_canvas = float(canvas.canvasy(y))
                target = ""
                try:
                    with self._pending_cv:
                        for p, t in self._pending_tasks:
                            if p == str(path):
                                target = str(t)
                                break
                except Exception:
                    pass
                name = Path(str(path)).name
                ghost_txt = f"{name} → {target}" if target else name
                self._queue_drag_ghost_show(ghost_txt, y_canvas)
        except Exception:
            pass

        # Capture drag events during reorder.
        try:
            canvas = getattr(self, "queue_canvas", None)
            if isinstance(canvas, tkinter.Canvas):
                canvas.bind("<B1-Motion>", self._on_queue_drag_motion)
                canvas.bind("<ButtonRelease-1>", self._on_queue_drag_drop)
                try:
                    canvas.grab_set_global()
                except Exception:
                    canvas.grab_set()
            # Also bind globally; on some platforms the grab isn't enough.
            self.root.bind_all("<B1-Motion>", self._on_queue_drag_motion)
            self.root.bind_all("<ButtonRelease-1>", self._on_queue_drag_drop)
        except Exception:
            pass

    def _on_queue_drag_motion(self, event) -> None:
        if self._queue_drag_from is None:
            return
        canvas = getattr(self, "queue_canvas", None)
        if not isinstance(canvas, tkinter.Canvas):
            return

        try:
            y_root = getattr(event, "y_root", None)
            if y_root is None:
                y_root = self.root.winfo_pointery()
            y = int(y_root) - int(canvas.winfo_rooty())
            y_canvas = float(canvas.canvasy(y))
        except Exception:
            return

        # Move ghost under cursor.
        try:
            self._queue_drag_ghost_show(self._queue_drag_ghost_text or "", y_canvas)
        except Exception:
            pass

        row_h = 34
        display_idx = max(0, int(y_canvas // row_h))

        try:
            with self._pending_cv:
                pending_paths = [p for (p, _t) in self._pending_tasks]
                pending_len = len(pending_paths)
        except Exception:
            pending_paths = []
            pending_len = 0

        running_paths = [p for p in self._queue_order if p in self._in_progress and p not in pending_paths]
        offset = len(running_paths)
        # Only the first running row is considered the "current" row.
        self._queue_drag_over_current = bool(offset >= 1 and display_idx == 0)
        if pending_len <= 0:
            return

        if display_idx < offset:
            to_idx = 0
        else:
            to_idx = display_idx - offset
        if to_idx < 0:
            to_idx = 0
        if to_idx >= pending_len:
            to_idx = pending_len - 1

        # Live reorder so it feels draggable.
        from_idx = self._queue_drag_from
        if to_idx != from_idx:
            try:
                with self._pending_cv:
                    item = self._pending_tasks.pop(from_idx)
                    self._pending_tasks.insert(to_idx, item)
                self._queue_drag_from = to_idx
                self._queue_drag_to = to_idx
                self._sync_queue_order_for_processing()
                self._refresh_queue_list()
                self._update_job_bar()
                self._debug_log(f"QUEUE DRAG move: from={from_idx} to={to_idx} pending_len={pending_len}")
            except Exception as e:
                self._debug_log(f"QUEUE DRAG move error: {e}")
        else:
            self._queue_drag_to = to_idx

    def _on_queue_drag_drop(self, event) -> None:
        # Release capture.
        try:
            canvas = getattr(self, "queue_canvas", None)
            if isinstance(canvas, tkinter.Canvas):
                canvas.unbind("<B1-Motion>")
                canvas.unbind("<ButtonRelease-1>")
                try:
                    canvas.grab_release()
                except Exception:
                    pass
            try:
                self.root.unbind_all("<B1-Motion>")
                self.root.unbind_all("<ButtonRelease-1>")
            except Exception:
                pass
        except Exception:
            pass

        from_idx = self._queue_drag_from
        to_idx = self._queue_drag_to
        over_current = bool(self._queue_drag_over_current)
        self._queue_drag_from = None
        self._queue_drag_to = None
        self._queue_drag_over_current = False
        self._queue_drag_ghost_clear()
        if from_idx is None:
            return
        if to_idx is None:
            return

        # Motion already performed live reorders; drop just finalizes.
        self._debug_log(f"QUEUE DRAG drop: to={to_idx} over_current={over_current}")

        # If user dropped onto the current row, interpret as "run this next".
        # With parallel workers, this means "start this in parallel".
        if over_current:
            self._enable_parallel_for_batch()

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

        # Bottom actions bar (Save As)
        actions = ttk.Frame(container)
        actions.grid(row=1, column=0, sticky="ew")
        self.save_btn = ttk.Button(actions, text="Save As", command=self._save_selected_results)
        self.save_btn.pack(side="right", padx=12, pady=8)

    def _on_result_drag_init(self, event=None, idx: int | None = None):
        # DragInitCmd must return (actions, types, data). For DND_FILES on macOS,
        # data should be a Tcl list of file paths.
        if idx is None:
            idx = self.selected_result_index
        if idx is None or idx < 0 or idx >= len(self.results):
            self._debug_log("DRAGINIT: no valid index")
            return

        out_path = self.results[idx].output_path
        if not out_path or not os.path.exists(out_path):
            self._debug_log(f"DRAGINIT: missing output path idx={idx} out={out_path}")
            return

        try:
            data = self.root.tk.call("list", out_path)
        except Exception:
            data = out_path

        self._debug_log(f"DRAGINIT: idx={idx} out={out_path} data={str(data)!r}")
        return ("copy",), (DND_FILES,), data

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
            try:
                c.delete(sel_id)
            except Exception:
                pass
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
            # Selection should sit behind text, like Finder.
            try:
                c.tag_lower(sel_id)
            except Exception:
                pass

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
            try:
                c.tag_raise(t_name)
            except Exception:
                pass
        if isinstance(t_target, int):
            c.coords(t_target, target_x, int(row_h / 2))
            c.itemconfigure(t_target, text=target, fill=muted)
            try:
                c.tag_raise(t_target)
            except Exception:
                pass
        if isinstance(t_output, int):
            c.coords(t_output, output_x, int(row_h / 2))
            c.itemconfigure(t_output, text=output, fill=muted)
            try:
                c.tag_raise(t_output)
            except Exception:
                pass

        # Separator line
        sep = row.get("sep")
        if isinstance(sep, int):
            c.coords(sep, pad_x, row_h - 1, width - pad_x, row_h - 1)

    def _on_result_row_click(self, idx: int) -> None:
        self.selected_result_index = idx
        self._refresh_result_row_visuals()

    def _on_result_row_double_click(self, idx: int) -> None:
        # Keep selection behavior consistent, then preview.
        self._on_result_row_click(idx)
        if idx < 0 or idx >= len(self.results):
            return
        res = self.results[idx]
        preview_path = res.output_path or res.source_path
        if not preview_path:
            return
        self._preview_file(preview_path)

    def _preview_file(self, path: str) -> None:
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)

            def _is_video(p: str) -> bool:
                ext = Path(p).suffix.lower()
                if ext in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
                    return True
                guessed, _enc = mimetypes.guess_type(p, strict=False)
                return bool(guessed and guessed.startswith("video/"))

            if sys.platform == "darwin":
                # Finder-like preview (Quick Look) but avoid qlmanage for videos.
                # On some macOS versions, qlmanage can crash when previewing certain movie files.
                if _is_video(path):
                    subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return

                try:
                    subprocess.Popen(
                        ["qlmanage", "-p", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return

            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
                return

            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self._debug_log(f"Preview failed: path={path} err={e}")
            try:
                messagebox.showerror(
                    "Preview failed",
                    f"{Path(path).name}\n\n{e}\n\nDebug log: {self._debug_log_path}",
                )
            except Exception:
                pass

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

        # Let the user pick a destination (Finder-style on macOS).
        initial_dir = self._save_dir or os.path.dirname(src)
        try:
            os.makedirs(initial_dir, exist_ok=True)
        except Exception:
            initial_dir = os.path.dirname(src)

        default_name = os.path.basename(src) if os.path.basename(src) else (res.source_name + res.target_ext)

        dest = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save As",
            initialdir=initial_dir,
            initialfile=default_name,
            defaultextension=res.target_ext.lower(),
            filetypes=[
                (f"{res.target_ext} file", f"*{res.target_ext.lower()}"),
                ("All files", "*.*"),
            ],
        )

        if not dest:
            # User cancelled.
            return

        dest_dir = os.path.dirname(dest) or "."
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception:
            pass

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
            row_h = 34
            c = tkinter.Canvas(
                self.convert_list_frame,
                height=row_h,
                highlightthickness=0,
                bd=0,
                bg=self._normal_bg,
            )
            c.grid(row=row_idx, column=0, sticky="ew")

            # Progress bar is drawn as a rounded shape during rendering (so it animates smoothly
            # and stays clipped to the rounded bounds). Placeholder id stored in the row dict.
            prog_id: int | None = None

            # Text + icon items (positions set in _render_convert_row)
            t_name = c.create_text(0, int(row_h / 2), text=f.name, anchor="w", fill=self._result_text, font=self.font_normal)
            t_size = c.create_text(0, int(row_h / 2), text=_human_size(f.size_bytes), anchor="e", fill=self._result_muted, font=self.font_normal)
            t_mime = c.create_text(0, int(row_h / 2), text=f.mime, anchor="e", fill=self._result_muted, font=self.font_normal)
            t_ext = c.create_text(0, int(row_h / 2), text=f.ext, anchor="e", fill=self._result_muted, font=self.font_normal)
            i_remove = c.create_image(0, int(row_h / 2), image=self.remove_icon)
            c.itemconfigure(i_remove, tags=("remove",))

            sep = c.create_line(10, row_h - 1, 10, row_h - 1, fill="#2c2c2e")

            def _row_click(ev, p=f.path) -> str | None:
                # Ignore clicks on the remove icon.
                try:
                    current = ev.widget.find_withtag("current")
                    if current and "remove" in ev.widget.gettags(current[0]):
                        return "break"
                except Exception:
                    pass
                self._on_row_click(ev, p)
                return "break"

            def _remove_click(_ev, p=f.path) -> str:
                self._remove_paths({p})
                return "break"

            def _ctx(ev, p=f.path) -> str:
                self._show_convert_context_menu(ev, p)
                return "break"

            def _dbl(ev, p=f.path) -> str | None:
                # Ignore double-clicks on the remove icon.
                try:
                    current = ev.widget.find_withtag("current")
                    if current and "remove" in ev.widget.gettags(current[0]):
                        return "break"
                except Exception:
                    pass
                self._on_convert_row_double_click(p)
                return "break"

            c.bind("<Button-1>", _row_click)
            c.bind("<Double-Button-1>", _dbl)
            c.tag_bind("remove", "<Button-1>", _remove_click)

            c.bind("<Button-3>", _ctx)
            c.bind("<Button-2>", _ctx)

            self._convert_rows[f.path] = {
                "path": f.path,
                "idx": row_idx,
                "canvas": c,
                "prog": prog_id,
                "t_name": t_name,
                "t_size": t_size,
                "t_mime": t_mime,
                "t_ext": t_ext,
                "i_remove": i_remove,
                "sep": sep,
                "full_name": f.name,
            }

        self._update_convert_selection_ui()
        self.convert_canvas.update_idletasks()
        self._layout_convert_rows(self.convert_canvas.winfo_width())
        self.convert_canvas.configure(scrollregion=self.convert_canvas.bbox("all"))

        self._refresh_row_visuals()
        self._update_job_bar()

    def _find_latest_result_index_for_source(self, source_path: str) -> int | None:
        for idx in range(len(self.results) - 1, -1, -1):
            try:
                if self.results[idx].source_path == source_path:
                    return idx
            except Exception:
                continue
        return None

    def _scroll_to_result_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._result_rows):
            return
        row = self._result_rows[idx]
        target = row.get("canvas")
        if not isinstance(target, tkinter.Canvas):
            return
        try:
            self.result_canvas.update_idletasks()
            y = target.winfo_y()
            height = max(1, self.result_list_frame.winfo_height())
            self.result_canvas.yview_moveto(y / height)
        except Exception:
            pass

    def _on_convert_row_double_click(self, source_path: str) -> None:
        # If we've already produced a converted output for this source, jump to it.
        idx = self._find_latest_result_index_for_source(source_path)
        if idx is not None:
            try:
                self.notebook.select(self.result_frame)
            except Exception:
                pass
            # Select + scroll + preview (consistent with Result tab behavior).
            self._on_result_row_double_click(idx)
            self._scroll_to_result_index(idx)
            return

        # Otherwise, queue a conversion for just this file.
        try:
            self.notebook.select(self.convert_frame)
        except Exception:
            pass
        self._selection_anchor = source_path
        self._set_selected_paths([source_path])
        self._queue_conversion()

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
        if isinstance(row, tkinter.Frame):
            target = row
        else:
            c = widgets.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                return
            target = c
        self.convert_canvas.update_idletasks()
        y = target.winfo_y()
        height = max(1, self.convert_list_frame.winfo_height())
        self.convert_canvas.yview_moveto(y / height)

    def _on_row_click(self, event, path: str) -> None:
        f = self._find_dropped_by_path(path)
        if f is not None and not self._is_source_supported(f):
            return

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

            def _dbl(_e, i=idx) -> None:
                self._on_result_row_double_click(i)

            row_canvas.bind("<Button-1>", _click)
            row_canvas.bind("<Double-Button-1>", _dbl)

            # Drag-out support per row
            try:
                row_canvas.drag_source_register(DND_FILES)  # type: ignore[attr-defined]
                def _drag_init(e, i=idx):
                    return self._on_result_drag_init(e, i)

                row_canvas.dnd_bind("<<DragInitCmd>>", _drag_init)  # type: ignore[attr-defined]
            except Exception:
                pass

        self._layout_result_rows(width)
        self._refresh_result_row_visuals()

    def _refresh_convert_progress(self) -> None:
        self._refresh_row_visuals()
        self._update_job_bar()

    def _update_job_bar(self) -> None:
        total = len(self._queue_paths)
        done = len(self._queue_done)
        failed = len(self._queue_failed)
        running = len(self._in_progress)

        try:
            with self._pending_cv:
                pending_count = len(self._pending_tasks)
                active_workers = int(self._active_conversions)
                worker_limit = int(self._parallel_limit)
        except Exception:
            pending_count = 0
            active_workers = running
            worker_limit = 1

        overall = 0.0
        if total:
            overall = sum(float(self._display_progress.get(p, self._progress.get(p, 0.0))) for p in self._queue_paths) / float(total)

        # Approx "data converted" as input bytes processed.
        total_bytes = 0
        done_bytes = 0
        for p in self._queue_paths:
            dropped = self._find_dropped_by_path(p)
            try:
                if dropped is not None and dropped.size_bytes is not None:
                    size_b = int(dropped.size_bytes)
                else:
                    size_b = int(os.path.getsize(p))
            except Exception:
                size_b = 0
            total_bytes += max(0, size_b)
            frac = float(self._display_progress.get(p, self._progress.get(p, 0.0)))
            frac = max(0.0, min(1.0, frac))
            done_bytes += int(size_b * frac)

        def _pct(x: float) -> str:
            return f"{int(max(0.0, min(1.0, x)) * 100)}%"

        # Derive target label when consistent.
        targets = {self._target_by_path.get(p) for p in self._queue_paths}
        targets.discard(None)
        target_txt = f" to {next(iter(targets))}" if len(targets) == 1 else ""

        # Current job
        cur = self._current_job_path
        if not cur and self._in_progress:
            cur = next(iter(self._in_progress))
        cur_idx = None
        if cur and cur in self._queue_order:
            try:
                cur_idx = self._queue_order.index(cur) + 1
            except Exception:
                cur_idx = None
        cur_name = Path(cur).name if cur else ""
        cur_p = float(self._display_progress.get(cur, self._progress.get(cur, 0.0))) if cur else 0.0
        cur_p = max(0.0, min(1.0, cur_p))

        now = time.time()
        age_s = 0
        if cur:
            last_ts = float(self._last_progress_ts.get(cur, 0.0))
            if last_ts:
                age_s = max(0, int(now - last_ts))

        if total == 0:
            label_text = "Queue"
            stats_text = "No jobs queued"
            current_text = ""
            progress_value = 0.0
        else:
            label_text = f"Queue · {done}/{total} complete · {_pct(overall)}{target_txt}"
            stats = f"Completed: {done}/{total}"
            if failed:
                stats += f" · Failed: {failed}"
            if running:
                stats += f" · In progress: {running}"
            if pending_count:
                stats += f" · Pending: {pending_count}"
            if running or pending_count:
                mode = "Parallel" if worker_limit > 1 else "Sequential"
                stats += f" · Workers: {active_workers}/{worker_limit} ({mode})"
            if total_bytes > 0:
                stats += f" · Data: {_human_size(done_bytes)}/{_human_size(total_bytes)}"
            stats_text = stats

            if cur:
                cur_txt = f"Current: {cur_name}"
                if cur_idx is not None:
                    cur_txt += f" ({cur_idx}/{total})"
                cur_txt += f" · {_pct(cur_p)}"
                if running > 1:
                    cur_txt += f" · +{running - 1} more"
                if cur_p >= 0.90 and age_s >= 10:
                    cur_txt += f" · Finalizing (no new progress {age_s}s)"
                current_text = cur_txt
            else:
                current_text = ""

            progress_value = max(0.0, min(100.0, overall * 100.0))

        cards: list[tuple[ttk.Label, ttk.Label, ttk.Label, ttk.Progressbar, ttk.Frame, ttk.Button]] = []
        try:
            cards.append((self.job_bar_label, self.job_bar_stats, self.job_bar_current, self.job_bar_progress, self.job_bar_body, self.job_bar_toggle))
        except Exception:
            pass
        try:
            cards.append((self.queue_bar_label, self.queue_bar_stats, self.queue_bar_current, self.queue_bar_progress, self.queue_bar_body, self.queue_bar_toggle))
        except Exception:
            pass

        for lbl, stats_lbl, cur_lbl, prog, body, toggle_btn in cards:
            try:
                lbl.configure(text=label_text)
                stats_lbl.configure(text=stats_text)
                cur_lbl.configure(text=current_text)
                prog.configure(value=progress_value)
            except Exception:
                pass

            if self._job_bar_collapsed:
                try:
                    body.grid_remove()
                except Exception:
                    pass
                try:
                    toggle_btn.configure(text="Show")
                except Exception:
                    pass
            else:
                try:
                    body.grid()
                except Exception:
                    pass
                try:
                    toggle_btn.configure(text="Hide")
                except Exception:
                    pass

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

    def _conversion_worker(self, is_extra: bool = False) -> None:
        while True:
            with self._pending_cv:
                while True:
                    if self._shutdown.is_set():
                        try:
                            who = "extra" if is_extra else "primary"
                            self._debug_log(f"WORKER {who} exit: shutdown")
                        except Exception:
                            pass
                        return

                    # Extra worker self-terminates whenever we return to sequential mode.
                    if is_extra and self._parallel_limit <= 1:
                        try:
                            self._debug_log("WORKER extra exit: sequential mode")
                        except Exception:
                            pass
                        return

                    if self._pending_tasks and (self._active_conversions < self._parallel_limit):
                        src_path, target_ext = self._pending_tasks.pop(0)
                        self._active_conversions += 1
                        if self._parallel_limit > 1 and self._parallel_one_shot and self._active_conversions >= 2:
                            self._parallel_engaged = True
                        break

                    self._pending_cv.wait()
            try:
                self._ui_events.put(("start", src_path, target_ext))
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

                cancel_cb = (lambda: bool(self._shutdown.is_set()))
                out_path = utils.convertFile(src_path, target_ext, self._output_dir, progress=_progress_cb, cancel=cancel_cb)
                self._ui_events.put(("done", src_path, out_path, target_ext))
                self._debug_log(f"WORKER done: src={src_path} out={out_path}")
            except utils.ConversionCancelled as e:
                # During shutdown, treat cancellations as a clean exit path.
                if self._shutdown.is_set():
                    self._debug_log(f"WORKER cancelled during shutdown: src={src_path} msg={e}")
                    return
                self._ui_events.put(("error", src_path, str(e)))
            except Exception as e:
                self._ui_events.put(("error", src_path, str(e)))
                self._debug_log(f"WORKER error: src={src_path} err={e}")
            finally:
                try:
                    with self._pending_cv:
                        self._active_conversions = max(0, int(self._active_conversions) - 1)

                        # One-shot parallel ends as soon as one of the parallel jobs finishes,
                        # leaving at most one active conversion.
                        if (
                            self._parallel_limit > 1
                            and self._parallel_one_shot
                            and self._parallel_engaged
                            and int(self._active_conversions) <= 1
                        ):
                            self._parallel_limit = 1
                            self._parallel_one_shot = False
                            self._parallel_engaged = False
                            try:
                                self._debug_log("PARALLEL one-shot ended; reverting to sequential")
                            except Exception:
                                pass
                        self._pending_cv.notify_all()
                except Exception:
                    pass

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
                if kind == "start":
                    _k, path, target_ext = evt
                    self._current_job_target = str(target_ext)
                    self._sync_queue_order_for_processing()
                    changed = True
                elif kind == "progress":
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
                    self._queue_done.add(str(path))
                    if self._current_job_path == str(path):
                        self._current_job_path = None
                        self._current_job_target = None
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
                    self._queue_failed.add(str(path))
                    if self._current_job_path == str(path):
                        self._current_job_path = None
                        self._current_job_target = None
                    name = Path(path).name
                    msg = str(_msg).strip() or "Unknown error"
                    self._debug_log(f"UI error: src={path} msg={msg}")
                    # During shutdown, suppress dialogs for cancelled/terminated conversions.
                    if not self._shutdown.is_set():
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
                self._refresh_queue_list()
                self._update_job_bar()

                # When a batch completes, revert to sequential mode.
                try:
                    with self._pending_cv:
                        if not self._in_progress and not self._pending_tasks:
                            self._parallel_limit = 1
                            self._parallel_one_shot = False
                            self._parallel_engaged = False
                            self._pending_cv.notify_all()
                except Exception:
                    pass
        except Exception as e:
            self._on_tk_exception(type(e), e, e.__traceback__)
        finally:
            if not self._shutdown.is_set():
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