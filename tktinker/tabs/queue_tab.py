from __future__ import annotations

import sys
import tkinter
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

if __name__ == "__main__":
    raise SystemExit(
        "This module is part of the 'tabs' package and is not meant to be run directly.\n"
        "Run the app from the project root with: python main.py"
    )
from ._typing_base import AppBase



if TYPE_CHECKING:
    from tkinter.font import Font

    from conversion_engine import ConversionEngine


class _QueueTabAppBase(AppBase):
    """Typing-only base for the Queue tab mixin.

    The queue tab relies on state stored on the main app (current job, pending
    order, progress dictionaries, colors, etc.). Pylance checks this module on
    its own, so we declare the required surface area here.
    """

    queue_frame: ttk.Frame

    # Theme/style
    _normal_bg: str
    _result_text: str
    _result_muted: str
    _result_selected_bg: str
    _result_selected_text: str
    _progress_bg: str
    font_normal: Font

    # Engine and queue state
    engine: ConversionEngine
    _queue_order: list[str]
    _queue_paths: set[str]
    _in_progress: set[str]
    _current_job_path: str | None
    _progress: dict[str, float]
    _display_progress: dict[str, float]
    _target_by_path: dict[str, str]

    # Drag state
    _queue_drag_from: int | None
    _queue_drag_to: int | None
    _queue_drag_over_current: bool
    _queue_drag_ghost: list[int]
    _queue_drag_ghost_text: str

    # Shared helpers implemented by the main app (or other mixins)
    def _toggle_job_bar(self) -> None: ...
    def _update_job_bar(self) -> None: ...
    def _open_logs(self) -> None: ...
    if TYPE_CHECKING:
        # Provided by another mixin at runtime (currently ResultTabMixin).
        def _rounded_rect(
            self,
            c: tkinter.Canvas,
            x1: int,
            y1: int,
            x2: int,
            y2: int,
            r: int,
            **kwargs,
        ) -> int: ...
    @staticmethod
    def _blend_hex(fg: str, bg: str, alpha: float) -> str: ...
    def _ellipsize(self, text: str, max_px: int) -> str: ...



class QueueTabMixin(_QueueTabAppBase):


    # -------------------- Queue Tab --------------------
    def _build_queue_tab(self) -> None:
        """Create the Queue tab UI and wire up its scroll/drag handlers."""
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

        self.queue_bar_logs = ttk.Button(header, text="Logs", width=7, command=self._open_logs)
        self.queue_bar_logs.grid(row=0, column=1, sticky="e", padx=(0, 8))

        self.queue_bar_toggle = ttk.Button(header, text="Hide", width=7, command=self._toggle_job_bar)
        self.queue_bar_toggle.grid(row=0, column=2, sticky="e")

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
        """Update scroll region when the queue list size changes."""
        try:
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))
        except Exception:
            pass

    def _bind_queue_mousewheel(self, _event=None) -> None:
        """Bind global mouse-wheel handlers while cursor is over the queue list."""
        self.root.bind_all("<MouseWheel>", self._on_queue_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_queue_mousewheel)
        self.root.bind_all("<Button-5>", self._on_queue_mousewheel)

    def _unbind_queue_mousewheel(self, _event=None) -> None:
        """Unbind global mouse-wheel handlers when cursor leaves the queue list."""
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_queue_mousewheel(self, event) -> None:
        """Scroll the queue list for mouse wheels and trackpads."""
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
        """Reflow queue rows when the tab resizes."""
        try:
            self.queue_canvas.itemconfigure(self._queue_list_window, width=event.width)
        except Exception:
            pass
        self._layout_queue_rows(event.width)

    def _layout_queue_rows(self, width: int) -> None:
        """Re-render each queue row for the given list width."""
        if width <= 50:
            width = 900
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
        """Render a single row in the Queue list."""
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
        """Synchronize display order with engine order (running first)."""
        pending_paths = [p for (p, _t) in self.engine.pending_snapshot()]

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
        """Return (src_path, target_ext, status) in current processing order."""
        items: list[tuple[str, str, str]] = []
        pending = self.engine.pending_snapshot()

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
        self.engine.enable_parallel_one_shot()

    def _queue_drag_ghost_clear(self) -> None:
        """Remove the drag ghost overlay from the canvas."""
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
        """Show/update the drag ghost overlay under the pointer."""
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
        """Rebuild the queue list UI from the current engine state."""
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
            w = int(self.queue_canvas.winfo_width()) if hasattr(self, "queue_canvas") else 0
            if w <= 50:
                w = 900
            self._layout_queue_rows(w)
            self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))
        except Exception:
            pass

    def _on_queue_drag_start(self, _event=None, path: str | None = None) -> None:
        """Begin a drag-reorder gesture for a pending queue item."""
        if not path:
            self._queue_drag_from = None
            return
        # The current converting item is always fixed at the top.
        if self._current_job_path and str(path) == str(self._current_job_path):
            self._queue_drag_from = None
            return
        pending_paths = [p for (p, _t) in self.engine.pending_snapshot()]

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
                    for p, t in self.engine.pending_snapshot():
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
        """Handle drag motion: live-reorder pending items and update ghost."""
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
            pending_paths = [p for (p, _t) in self.engine.pending_snapshot()]
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
                self.engine.reorder_pending(from_idx, to_idx)
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
        """Finalize drag-reorder and optionally enable parallel mode."""
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
