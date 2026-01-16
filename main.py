"""Convertable: Tkinter drag-and-drop file converter.

`ConvertableApp` owns the window, session state, and shared UI behaviors.
Tab-specific UI code lives in `tabs/` as mixins.
"""

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
from pathlib import Path
from typing import Iterable
from datetime import datetime
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import filedialog

import utils

from app_helpers import human_size, parse_dnd_files, resource_base_dir, set_window_icon, to_dropped_file
from conversion_engine import ConversionEngine
from models import ConversionJob, ConversionResultItem, DroppedFile

from tkinterdnd2 import DND_FILES, TkinterDnD

from tabs import ConvertTabMixin, DropTabMixin, QueueTabMixin, ResultTabMixin

class ConvertableApp(DropTabMixin, ConvertTabMixin, QueueTabMixin, ResultTabMixin):
    def __init__(self) -> None:
        """Initialize the main window, state, engine, and all UI tabs."""
        self.root = TkinterDnD.Tk()
        self.root.title("Convertable")
        self.root.geometry("900x560")
        self.root.resizable(True, True)

        default_font = ("Inter", 14)
        self.root.option_add("*Font", default_font)
        set_window_icon(self.root)

        self.dropped: list[DroppedFile] = []
        self.jobs: list[ConversionJob] = []
        self.results: list[ConversionResultItem] = []

        self._in_progress: set[str] = set()
        self._progress: dict[str, float] = {}
        self._display_progress: dict[str, float] = {}
        self._target_by_path: dict[str, str] = {}
        self._animate_active: bool = False
        self._last_progress_ts: dict[str, float] = {}
        self._job_started_ts: dict[str, float] = {}
        self._batch_started_ts: float | None = None

        # Background conversion engine (pending queue + worker threads).
        self.engine = ConversionEngine(output_dir_getter=lambda: self._output_dir, debug_log=self._debug_log)

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

        self.engine.start()
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

    def _source_category(self, dropped: DroppedFile | None) -> str | None:
        """Return a coarse category for a dropped file.

        Returns: "image", "audio", "video", or `None` if unsupported.
        """
        # None means unsupported / not convertible.
        if dropped is None:
            return None
        if dropped.mime == "inode/directory":
            return None
        ext = Path(dropped.path).suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".bmp", ".tiff", ".tif", ".gif", ".svg", ".pdf"}:
            return "image"
        if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"}:
            return "audio"
        if ext in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
            return "video"

        mime = (dropped.mime or "").lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"

        return None

    def _is_source_supported(self, dropped: DroppedFile | None) -> bool:
        """Return True if the given dropped file is convertible."""
        return self._source_category(dropped) is not None

    def _on_close(self) -> None:
        """Handle app shutdown (confirm if converting, then cleanup + exit)."""

        # If a conversion is actively running, warn before exiting.
        try:
            pending_count, active_workers = self.engine.worker_stats()
        except Exception:
            pending_count, active_workers = 0, 0

        if int(active_workers) > 0:
            try:
                msg = (
                    "A conversion is currently running.\n\n"
                    "Closing the app will cancel the active conversion."
                )
                if int(pending_count) > 0:
                    msg += f"\n\nPending in queue: {int(pending_count)}"
                msg += "\n\nQuit anyway?"
                ok = messagebox.askyesno("Quit Convertable?", msg, parent=self.root)
            except Exception:
                ok = True

            if not ok:
                try:
                    self._debug_log("Close cancelled by user (conversion running)")
                except Exception:
                    pass
                return

        try:
            self._debug_log("App closing")
            try:
                self.engine.stop()
            except Exception:
                pass

            # Kill any active ffmpeg processes promptly.
            try:
                utils.terminate_active_processes()
            except Exception:
                pass

            shutil.rmtree(self._session_output_dir, ignore_errors=True)
        finally:
            self.root.destroy()

    def _debug_log(self, msg: str) -> None:
        """Write a message to the persistent debug log file."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self._debug_log_path, "a", encoding="utf-8") as fp:
                fp.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    # -------------------- Shared --------------------
    def _find_dropped_by_path(self, path: str) -> DroppedFile | None:
        """Look up a dropped file model by absolute path."""
        for f in self.dropped:
            if f.path == path:
                return f
        return None

    def _on_drop(self, event) -> None:
        """Handle OS drag-and-drop onto the window."""
        files = parse_dnd_files(self.root, event.data)

        # Add unique paths only.
        existing = {f.path for f in self.dropped}
        new_paths = [p for p in files if p not in existing]

        for p in new_paths:
            self.dropped.append(to_dropped_file(p))
        self._refresh_all_lists()

        # Switch to Convert tab and highlight newly-added files.
        self.notebook.select(self.convert_frame)
        if new_paths:
            self._set_selected_paths(new_paths)
            self._scroll_to_path(new_paths[0])

    def _refresh_all_lists(self) -> None:
        """Refresh the main lists after state changes."""
        self._refresh_convert_list()
        self._refresh_result_list()

    def _refresh_convert_list(self) -> None:
        """Rebuild the Convert list from `self.dropped` and current selection."""
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
            t_size = c.create_text(0, int(row_h / 2), text=human_size(f.size_bytes), anchor="e", fill=self._result_muted, font=self.font_normal)
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
        """Return the newest result index for a given source path, if any."""
        for idx in range(len(self.results) - 1, -1, -1):
            try:
                if self.results[idx].source_path == source_path:
                    return idx
            except Exception:
                continue
        return None

    def _scroll_to_result_index(self, idx: int) -> None:
        """Scroll the Result list to make row `idx` visible."""
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
        """Handle double-click on a Convert row.

        If a result already exists for the source, jump to it; otherwise queue a
        conversion for that single file.
        """
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
        """Show a small context menu for a Convert row."""
        menu: tkinter.Menu | None = None
        try:
            menu = tkinter.Menu(self.root, tearoff=0)
            menu.add_command(label="Show in Finder", command=lambda p=path: self._reveal_in_finder(p))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                if menu is not None:
                    menu.grab_release()
            except Exception:
                pass

    def _reveal_in_finder(self, path: str) -> None:
        """Reveal a file in Finder (macOS) or best-effort on other OSes."""
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], check=False)
            return
        # Best-effort fallback
        try:
            subprocess.run(["xdg-open", os.path.dirname(path)], check=False)
        except Exception:
            pass

    def _scroll_to_path(self, path: str) -> None:
        """Scroll the Convert list to bring `path` into view."""
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
        """Update selection based on click modifiers (shift/cmd/ctrl)."""
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
        """Load the small "X" icon used to remove a file from the Convert list."""
        base_dir = resource_base_dir()
        svg_path = base_dir / "assets" / "x.svg"

        # Try to render the SVG using CairoSVG (preferred).
        try:
            import cairosvg

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
        """Rebuild the Result list rows for the current `self.results`."""
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
                drag_source_register = getattr(row_canvas, "drag_source_register", None)
                dnd_bind = getattr(row_canvas, "dnd_bind", None)
                if callable(drag_source_register) and callable(dnd_bind):
                    drag_source_register(DND_FILES)

                    def _drag_init(e, i=idx):
                        return self._on_result_drag_init(e, i)

                    dnd_bind("<<DragInitCmd>>", _drag_init)
            except Exception:
                pass

        self._layout_result_rows(width)
        self._refresh_result_row_visuals()

    def _refresh_convert_progress(self) -> None:
        """Refresh Convert visuals after progress/state changes."""
        self._refresh_row_visuals()
        self._update_job_bar()

    def _update_job_bar(self) -> None:
        """Update the queue/job summary cards shown on Convert + Queue tabs."""
        total = len(self._queue_paths)
        done = len(self._queue_done)
        failed = len(self._queue_failed)
        running = len(self._in_progress)

        pending_count, active_workers = self.engine.worker_stats()
        worker_limit = self.engine.worker_limit()

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

        def _fmt_duration(seconds: float | int | None) -> str:
            """Format a duration in seconds as m:ss or h:mm:ss."""
            if seconds is None:
                return "--"
            try:
                s = int(max(0, float(seconds)))
            except Exception:
                return "--"
            hh = s // 3600
            mm = (s % 3600) // 60
            ss = s % 60
            if hh > 0:
                return f"{hh}:{mm:02d}:{ss:02d}"
            return f"{mm}:{ss:02d}"

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
                stats += f" · Data: {human_size(done_bytes)}/{human_size(total_bytes)}"

            # Timing/ETA (best-effort): derive from batch start and overall progress.
            elapsed_s: float | None = None
            if self._batch_started_ts:
                elapsed_s = max(0.0, now - float(self._batch_started_ts))
            else:
                # Fallback: use the earliest per-file start timestamp we have.
                starts = [float(self._job_started_ts.get(p, 0.0)) for p in self._queue_paths]
                starts = [t for t in starts if t > 0]
                if starts:
                    elapsed_s = max(0.0, now - min(starts))

            if running or pending_count:
                if elapsed_s is not None:
                    stats += f" · Elapsed: {_fmt_duration(elapsed_s)}"
                if overall > 0.02 and elapsed_s is not None and overall < 1.0:
                    eta_s = elapsed_s * ((1.0 - overall) / max(1e-6, overall))
                    # Avoid absurd numbers when progress is tiny.
                    if 0 <= eta_s <= 7 * 24 * 3600:
                        stats += f" · ETA: {_fmt_duration(eta_s)}"
                    else:
                        stats += " · ETA: --"
                elif running or pending_count:
                    stats += " · ETA: --"
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

                # Per-file runtime (uses queue-time as a fallback start).
                try:
                    started = float(self._job_started_ts.get(cur, 0.0))
                except Exception:
                    started = 0.0
                if started > 0:
                    cur_txt += f" · Running: {_fmt_duration(now - started)}"
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
        """Start the periodic progress UI tick if not already running."""
        if self._animate_active:
            return
        self._animate_active = True
        self.root.after(60, self._tick_progress_animation)

    def _tick_progress_animation(self) -> None:
        """Progress animation tick: ease displayed progress toward real progress."""
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

    def _process_ui_events(self) -> None:
        """Apply conversion-engine events to UI state on the Tk thread."""
        try:
            # Drain UI events from worker thread.
            changed = False
            while True:
                try:
                    evt = self.engine.get_event_nowait()
                except queue.Empty:
                    break
                kind = evt[0]
                if kind == "start":
                    _k, path, target_ext = evt
                    if self._batch_started_ts is None:
                        self._batch_started_ts = time.time()
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
                    if not self.engine.shutdown_event.is_set():
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
                self.engine.revert_to_sequential_if_idle(in_progress_empty=(not self._in_progress))
        except Exception as e:
            self._on_tk_exception(type(e), e, e.__traceback__)
        finally:
            if not self.engine.shutdown_event.is_set():
                self.root.after(60, self._process_ui_events)

    def _on_tk_exception(self, exc, val, tb) -> None:
        """Global Tk callback exception hook (prevents silent UI loop death)."""
        try:
            import traceback

            text = "".join(traceback.format_exception(exc, val, tb))
        except Exception:
            text = f"{exc}: {val}"
        print("[Convertable] Tk callback exception:\n" + text)

    def run(self) -> None:
        """Run the application (Tk main loop)."""
        self.root.mainloop()

def create_window() -> None:
    """CLI entry point."""
    ConvertableApp().run()

if __name__ == "__main__":
    create_window()