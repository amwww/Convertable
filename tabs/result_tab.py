from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import sys
import tkinter
from pathlib import Path
from typing import TYPE_CHECKING
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk

from models import ConversionResultItem
from tkinterdnd2 import DND_FILES

from tabs._typing_base import AppBase


if TYPE_CHECKING:
    from tkinter.font import Font


class _ResultTabAppBase(AppBase):
    """Typing-only base for the Result tab mixin.

    The Result tab renders `self.results` and relies on app-owned colors,
    selection state, and helper methods like `_ellipsize`.
    """

    result_frame: ttk.Frame

    # Theme/style
    _result_bg: str
    _result_text: str
    _result_muted: str
    _result_selected_bg: str
    _result_selected_text: str
    font_normal: Font

    # Session state
    results: list[ConversionResultItem]
    selected_result_index: int | None
    _result_rows: list[dict[str, object]]

    # Save/paths/debug
    _save_dir: str | None
    _session_output_dir: str
    _debug_log_path: str

    # Shared helpers
    def _ellipsize(self, text: str, max_px: int) -> str: ...
    def _refresh_result_list(self) -> None: ...



class ResultTabMixin(_ResultTabAppBase):

    # -------------------- Result Tab --------------------
    def _build_result_tab(self) -> None:
        """Create the Result tab UI (scrolling list + Save As button)."""
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
        """Provide drag payload for DND_FILES (dragging an output file out)."""
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
        """Bind global mouse-wheel handlers while cursor is over the results list."""
        self.root.bind_all("<MouseWheel>", self._on_result_mousewheel)
        # Linux
        self.root.bind_all("<Button-4>", self._on_result_mousewheel)
        self.root.bind_all("<Button-5>", self._on_result_mousewheel)

    def _unbind_result_mousewheel(self, _event=None) -> None:
        """Unbind global mouse-wheel handlers when cursor leaves the results list."""
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_result_mousewheel(self, event) -> None:
        """Scroll the results list for mouse wheels and trackpads."""
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
        """Reflow result rows when the tab resizes."""
        self.result_canvas.itemconfigure(self._result_list_window, width=event.width)
        self._layout_result_rows(event.width)

    def _rounded_rect(self, c: tkinter.Canvas, x1: int, y1: int, x2: int, y2: int, r: int, **kwargs) -> int:
        """Draw a rounded rectangle on a Tk canvas and return the item id."""
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
        """Re-render each result row for the given list width."""
        for row in self._result_rows:
            c = row.get("canvas")
            if not isinstance(c, tkinter.Canvas):
                continue
            c.configure(width=width)
            self._render_result_row(row, width)

    def _render_result_row(self, row: dict[str, object], width: int) -> None:
        """Render a single row in the Result list."""
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
        """Select a result row."""
        self.selected_result_index = idx
        self._refresh_result_row_visuals()

    def _on_result_row_double_click(self, idx: int) -> None:
        """Select a result row and open a preview for its file."""
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
        """Open the given file with the platform default preview/app."""
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
        """Re-render visuals for all result rows (selection highlight)."""
        width = self.result_canvas.winfo_width() if hasattr(self, "result_canvas") else 0
        if width <= 0:
            width = 900
        for row in self._result_rows:
            self._render_result_row(row, width)

    def _unique_dest_path(self, dest_dir: str, filename: str) -> str:
        """Return a non-existing path in `dest_dir`, adding " (n)" if needed."""
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
        """Copy the selected output file to a user-chosen destination."""
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
