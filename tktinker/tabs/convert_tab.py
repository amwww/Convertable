from __future__ import annotations

import sys
import time
import tkinter
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import TYPE_CHECKING

from app_helpers import human_size
from ._typing_base import AppBase

if TYPE_CHECKING:
    from tkinter.font import Font

    from conversion_engine import ConversionEngine
    from models import DroppedFile
    from models import ConversionJob


class _ConvertTabAppBase(AppBase):
    """Typing-only base for the Convert tab mixin.

    The tab mixins are designed to be mixed into the main app class.
    At runtime, the main app provides these attributes/methods.

    Pylance type-checks each mixin file in isolation, so we declare the
    required surface area here to avoid "unknown attribute" errors.
    """

    # Core Tk objects
    convert_frame: ttk.Frame

    # Styling/colors/fonts
    _normal_bg: str
    _selected_bg: str
    _result_selected_bg: str
    _progress_bg: str
    _convert_text: str
    _convert_muted: str
    _convert_selected_text: str
    font_normal: Font

    # Column sizing (pixels)
    _min_ext_px: int
    _min_mime_px: int
    _min_size_px: int
    _min_remove_px: int

    # Data/state
    dropped: list[DroppedFile]
    selected_paths: list[str]
    _selection_anchor: str | None
    _convert_rows: dict[str, dict[str, object]]
    _in_progress: set[str]
    _progress: dict[str, float]
    _display_progress: dict[str, float]
    _target_by_path: dict[str, str]
    _last_progress_ts: dict[str, float]
    _job_started_ts: dict[str, float]
    _batch_started_ts: float | None
    _queue_paths: set[str]
    _queue_done: set[str]
    _queue_failed: set[str]
    _queue_order: list[str]
    _current_job_path: str | None
    _current_job_target: str | None
    _job_bar_collapsed: bool

    # Engine + conversion UI plumbing
    engine: ConversionEngine
    jobs: list[ConversionJob]

    # Shared helpers (implemented on the main app)
    def _update_job_bar(self) -> None: ...
    def _refresh_all_lists(self) -> None: ...
    def _refresh_queue_list(self) -> None: ...
    def _refresh_convert_progress(self) -> None: ...
    def _start_progress_animation(self) -> None: ...
    def _sync_queue_order_for_processing(self) -> None: ...
    def _open_logs(self) -> None: ...
    def _find_dropped_by_path(self, path: str) -> DroppedFile | None: ...
    def _is_source_supported(self, dropped: DroppedFile) -> bool: ...
    def _source_category(self, dropped: DroppedFile | None) -> str | None: ...

    if TYPE_CHECKING:
        # Provided by another mixin at runtime (currently ResultTabMixin).
        # Declared here only for static type checking.
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


class ConvertTabMixin(_ConvertTabAppBase):


    # -------------------- Convert Tab --------------------
    def _build_convert_tab(self) -> None:
        """Create the Convert tab UI and wire up its event handlers."""
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

        self.job_bar_logs = ttk.Button(header, text="Logs", width=7, command=self._open_logs)
        self.job_bar_logs.grid(row=0, column=1, sticky="e", padx=(0, 8))

        self.job_bar_toggle = ttk.Button(header, text="Hide", width=7, command=self._toggle_job_bar)
        self.job_bar_toggle.grid(row=0, column=2, sticky="e")

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

        # Empty-state placeholder (shown when there are no dropped items).
        self._convert_empty_frame = tkinter.Frame(list_host, bg=self._normal_bg)
        self._convert_empty_frame.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self._convert_empty_frame.columnconfigure(0, weight=1)
        self._convert_empty_frame.rowconfigure(0, weight=1)
        self._convert_empty_frame.rowconfigure(3, weight=1)

        title = tkinter.Label(
            self._convert_empty_frame,
            text="No files yet",
            bg=self._normal_bg,
            fg=self._convert_text,
        )
        title.grid(row=1, column=0, pady=(0, 4))

        try:
            sub_font = tkfont.Font(root=self.root, font=self.font_normal)
            sub_font.configure(size=max(9, int(sub_font.cget("size")) - 2))
        except Exception:
            sub_font = self.font_normal

        sub = tkinter.Label(
            self._convert_empty_frame,
            text="Drop files here",
            bg=self._normal_bg,
            fg=self._convert_muted,
            font=sub_font,
        )
        sub.grid(row=2, column=0)

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
        self._update_convert_empty_state()

    def _update_convert_empty_state(self) -> None:
        """Toggle Convert empty-state placeholder vs Finder-style list."""
        has_items = bool(getattr(self, "dropped", []))
        if has_items:
            try:
                self._convert_empty_frame.grid_remove()
            except Exception:
                pass
            try:
                self.convert_canvas.grid()
                self.convert_scroll.grid()
            except Exception:
                pass
        else:
            try:
                self.convert_canvas.grid_remove()
                self.convert_scroll.grid_remove()
            except Exception:
                pass
            try:
                self._convert_empty_frame.grid()
            except Exception:
                pass

    def _set_action_enabled(self, enabled: bool) -> None:
        """Enable/disable the Convert controls based on selection validity."""
        state = "normal" if enabled else "disabled"
        self.convert_btn.configure(state=state)
        self.convert_to.configure(state=("normal" if enabled else "disabled"))

    def _on_convert_list_configure(self, _event=None) -> None:
        """Update the canvas scroll-region when the list size changes."""
        self.convert_canvas.configure(scrollregion=self.convert_canvas.bbox("all"))

    def _bind_convert_mousewheel(self, _event=None) -> None:
        """Bind global mouse-wheel handlers while cursor is over the list."""
        self.root.bind_all("<MouseWheel>", self._on_convert_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_convert_mousewheel)
        self.root.bind_all("<Button-5>", self._on_convert_mousewheel)

    def _unbind_convert_mousewheel(self, _event=None) -> None:
        """Unbind global mouse-wheel handlers when cursor leaves the list."""
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_convert_mousewheel(self, event) -> None:
        """Scroll the Convert list for mouse wheels and trackpads."""
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
        """Reflow rows when the Convert tab resizes."""
        # Make inner frame match canvas width so filename column can shrink.
        self.convert_canvas.itemconfigure(self._convert_list_window, width=event.width)
        self._layout_convert_rows(event.width)
        self._refresh_row_visuals()

    def _layout_convert_rows(self, width: int) -> None:
        """Re-render each row for the given list width."""
        if width <= 50:
            width = 900
        for row in self._convert_rows.values():
            c = row.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                continue
            c.configure(width=width)
            self._render_convert_row(row, width)

    def _render_convert_row(self, row: dict[str, object], width: int) -> None:
        """Render a single row in the Convert list.

        This draws the Finder-like selection pill, optional progress bar fill,
        and right-aligned columns (remove icon, size, mime, ext).
        """
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

            if getattr(self, "_ui_debug", False):
                try:
                    self._debug_log(
                        f"UI convert select: path={path} c_w={c.winfo_width()} arg_w={width} "
                        f"x1={x1} x2={x2} y1={y1} y2={y2} ids={sel_ids}"
                    )
                except Exception:
                    pass

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

                if getattr(self, "_ui_debug", False):
                    try:
                        self._debug_log(
                            f"UI convert progress: path={path} p={p:.3f} fill_w={fill_w} track_w={track_w} ids={prog_ids}"
                        )
                    except Exception:
                        pass

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
        size_txt = human_size(dropped.size_bytes)
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
        """Blend `fg` over `bg` with alpha in [0..1] and return `#RRGGBB`."""

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
        """Return (normal_bg, selected_bg) derived from the current theme."""
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
        """Clear selection when the user clicks on empty space."""
        # Clicking on empty space clears selection.
        if event.widget is self.convert_list_frame:
            self._set_selected_paths([])

    def _ellipsize(self, text: str, max_px: int) -> str:
        """Truncate `text` with an ellipsis to fit into `max_px` pixels."""
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
        """Update visible filename truncation based on the current width."""
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
        """Replace current selection with `paths` (deduped, order-preserving)."""
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
        """Update labels/buttons based on selection and supported types."""
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
        """Re-render selection/progress visuals for every row."""
        for path in self._convert_rows.keys():
            self._apply_row_visual_state(path)

    def _apply_row_visual_state(self, path: str) -> None:
        """Apply selection/progress visuals to a single row."""
        widgets = self._convert_rows.get(path)
        if not widgets:
            return

        # Canvas-based Convert rows (Finder-like styling)
        c = widgets.get("canvas")
        if isinstance(c, tkinter.Canvas):
            width = 0
            try:
                width = int(c.winfo_width())
            except Exception:
                width = 0
            if width <= 50:
                try:
                    width = int(self.convert_canvas.winfo_width()) if hasattr(self, "convert_canvas") else 0
                except Exception:
                    width = 0
            if width <= 50:
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
        """Pick the convert target options based on the current selection."""
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
        """Set convert target options based on a MIME prefix (image/audio/video)."""
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
        """Filter combobox options based on the user's typed text."""
        typed = self.convert_to_var.get().strip().upper()
        if not typed:
            self.convert_to.configure(values=self._all_convert_options)
            return
        filtered = [v for v in self._all_convert_options if typed in v]
        self.convert_to.configure(values=filtered if filtered else self._all_convert_options)

    def _remove_selected(self) -> None:
        """Remove currently selected source files from the session."""
        sel = set(self.selected_paths)
        if not sel:
            return
        self._remove_paths(sel)

    def _remove_paths(self, paths: set[str]) -> None:
        """Remove a set of source paths and clean up any queued/in-progress state."""
        self.dropped = [f for f in self.dropped if f.path not in paths]
        self.jobs = [j for j in self.jobs if j.source_path not in paths]

        # Remove any pending tasks for these paths.
        self.engine.remove_pending(paths)

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
        """Enqueue conversion jobs for the current selection."""
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
            self._batch_started_ts = None

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
            self.engine.enqueue(src_path, target_ext)

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
        """Collapse/expand the queue status card shown at the top of the tab."""
        self._job_bar_collapsed = not self._job_bar_collapsed
        self._update_job_bar()
