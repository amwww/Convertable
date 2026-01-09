import os
import sys
import tkinter
import mimetypes
import base64
import threading
import queue
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from tkinter import ttk
from tkinter import font as tkfont

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
        self._animate_active: bool = False
        self._output_dir = str((Path.home() / "Convertable" / "Output").resolve())

        self.remove_icon = self._load_remove_icon()

        self.font_normal = tkfont.nametofont("TkDefaultFont")

        # Column minimum widths (in pixels). Stats should not be the first thing to clip.
        self._min_size_px = self.font_normal.measure("999.9 MB") + 16
        self._min_mime_px = self.font_normal.measure("application/octet-stream") + 16
        self._min_ext_px = self.font_normal.measure(".JPEG") + 16
        self._min_remove_px = 14 + 16

        self._normal_bg, self._selected_bg = self._selection_colors()

        self.selected_paths: list[str] = []
        self._selection_anchor: str | None = None
        self._convert_rows: dict[str, dict[str, object]] = {}

        self._worker = threading.Thread(target=self._conversion_worker, daemon=True)
        self._worker.start()
        self.root.after(60, self._process_ui_events)

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
        self.convert_frame.rowconfigure(0, weight=1)
        self.convert_frame.columnconfigure(0, weight=1)

        # Scrollable file list
        list_host = ttk.Frame(self.convert_frame)
        list_host.grid(row=0, column=0, sticky="nsew")
        list_host.rowconfigure(0, weight=1)
        list_host.columnconfigure(0, weight=1)

        self.convert_canvas = tkinter.Canvas(list_host, highlightthickness=0)
        self.convert_canvas.grid(row=0, column=0, sticky="nsew")
        self.convert_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.convert_canvas.yview)
        self.convert_scroll.grid(row=0, column=1, sticky="ns")
        self.convert_canvas.configure(yscrollcommand=self.convert_scroll.set)

        self.convert_list_frame = ttk.Frame(self.convert_canvas)
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
        actions.grid(row=1, column=0, sticky="ew")

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

        selected_set = set(self.selected_paths)
        for path, widgets in self._convert_rows.items():
            is_selected = path in selected_set
            bg = self._selected_bg if is_selected else self._normal_bg

            row = widgets.get("row")
            if isinstance(row, tkinter.Frame):
                row.configure(bg=bg)

            for key in ("name", "size", "mime", "ext"):
                w = widgets.get(key)
                if isinstance(w, tkinter.Label):
                    w.configure(bg=bg)

            remove = widgets.get("remove")
            if isinstance(remove, tkinter.Label):
                remove.configure(bg=bg)

            name_group = widgets.get("name_group")
            if isinstance(name_group, tkinter.Frame):
                name_group.configure(bg=bg)

            spacer = widgets.get("spacer")
            if isinstance(spacer, tkinter.Label):
                spacer.configure(bg=bg)

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
        self._refresh_all_lists()

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
            self._task_queue.put((src_path, target_ext))

        # Keep user on this page; show progress fill.
        self._start_progress_animation()
        self._refresh_convert_progress()

    # -------------------- Result Tab --------------------
    def _build_result_tab(self) -> None:
        container = ttk.Frame(self.result_frame)
        container.pack(fill="both", expand=True)

        columns = ("name", "target", "output")
        self.result_tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        self.result_tree.heading("name", text="File")
        self.result_tree.heading("target", text="To")
        self.result_tree.heading("output", text="Output")
        self.result_tree.column("name", width=600, anchor="w")
        self.result_tree.column("target", width=100, anchor="center")
        self.result_tree.column("output", width=260, anchor="w")
        self.result_tree.pack(fill="both", expand=True)

        tree_scroll = ttk.Scrollbar(container, orient="vertical", command=self.result_tree.yview)
        tree_scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self.result_tree.configure(yscrollcommand=tree_scroll.set)

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
            progress_fill = tkinter.Frame(row, bg="#c9f7d4")
            progress_fill.place(x=0, y=0, relheight=1.0, relwidth=0.0)
            progress_fill.lower()

            name_label = tkinter.Label(name_group, text=f.name, anchor="w", bg=self._normal_bg, font=self.font_normal)
            name_label.pack(side="left")

            # Spacer column takes remaining width so stats stay visible.
            spacer = tkinter.Label(row, text="", bg=self._normal_bg)
            spacer.grid(row=0, column=1, sticky="ew")

            # Remove icon sits right before size (aligned with stats)
            remove_label = tkinter.Label(row, image=self.remove_icon, bg=self._normal_bg)
            remove_label.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=6)
            remove_label.bind("<Button-1>", lambda _e, p=f.path: (self._remove_paths({p}), "break")[1])

            size_label = tkinter.Label(row, text=_human_size(f.size_bytes), anchor="e", bg=self._normal_bg, font=self.font_normal)
            size_label.grid(row=0, column=3, sticky="e", padx=(0, 12), pady=6)

            mime_label = tkinter.Label(row, text=f.mime, anchor="w", bg=self._normal_bg, font=self.font_normal)
            mime_label.configure(anchor="e")
            mime_label.grid(row=0, column=4, sticky="e", padx=(0, 12), pady=6)

            ext_label = tkinter.Label(row, text=f.ext, anchor="e", bg=self._normal_bg, font=self.font_normal)
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

        self._refresh_convert_progress()

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
        for item in self.result_tree.get_children(""):
            self.result_tree.delete(item)
        for idx, res in enumerate(self.results, start=1):
            self.result_tree.insert("", "end", iid=str(idx), values=(res.source_name, res.target_ext, res.output_path))

    def _refresh_convert_progress(self) -> None:
        for path, widgets in self._convert_rows.items():
            fill = widgets.get("progress_fill")
            if not isinstance(fill, tkinter.Frame):
                continue
            p = float(self._progress.get(path, 0.0))
            if p < 0:
                p = 0.0
            if p > 1:
                p = 1.0
            fill.place_configure(relwidth=p)

    def _start_progress_animation(self) -> None:
        if self._animate_active:
            return
        self._animate_active = True
        self.root.after(60, self._tick_progress_animation)

    def _tick_progress_animation(self) -> None:
        # Smoothly fill progress for active jobs even if the converter doesn't report progress.
        active = False
        for path in list(self._in_progress):
            active = True
            current = float(self._progress.get(path, 0.0))
            if current < 0.95:
                self._progress[path] = min(0.95, current + 0.01)

        self._refresh_convert_progress()
        if active:
            self.root.after(60, self._tick_progress_animation)
        else:
            self._animate_active = False

    def _conversion_worker(self) -> None:
        while True:
            src_path, target_ext = self._task_queue.get()
            try:
                # Best-effort progress callback (may only update in coarse steps).
                def _progress_cb(v: float) -> None:
                    self._ui_events.put(("progress", src_path, float(v)))

                out_path = utils.convertFile(src_path, target_ext, self._output_dir, progress=_progress_cb)
                self._ui_events.put(("done", src_path, out_path, target_ext))
            except Exception as e:
                self._ui_events.put(("error", src_path, str(e)))
            finally:
                self._task_queue.task_done()

    def _process_ui_events(self) -> None:
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
                changed = True
            elif kind == "done":
                _k, path, out_path, target_ext = evt
                self._progress[path] = 1.0
                if path in self._in_progress:
                    self._in_progress.remove(path)
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
                self._progress[path] = 0.0
                changed = True

        if changed:
            self._refresh_convert_progress()
        self.root.after(60, self._process_ui_events)

    def run(self) -> None:
        self.root.mainloop()

def create_window() -> None:
    ConvertableApp().run()

if __name__ == "__main__":
    create_window()