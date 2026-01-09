import os
import sys
import tkinter
import mimetypes
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from tkinter import ttk
from tkinter import font as tkfont

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

        self.remove_icon = self._load_remove_icon()

        self.font_normal = tkfont.nametofont("TkDefaultFont")
        self.font_bold = self.font_normal.copy()
        self.font_bold.configure(weight="bold")

        self.selected_paths: list[str] = []
        self._convert_rows: dict[str, dict[str, tkinter.Widget]] = {}

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
        self.convert_frame.rowconfigure(1, weight=1)
        self.convert_frame.columnconfigure(0, weight=1)

        header = ttk.Frame(self.convert_frame)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="File").grid(row=0, column=0, sticky="w", padx=(12, 6), pady=(10, 6))
        ttk.Label(header, text="").grid(row=0, column=1, sticky="w", padx=(0, 6), pady=(10, 6))
        ttk.Label(header, text="Size").grid(row=0, column=2, sticky="e", padx=(0, 12), pady=(10, 6))
        ttk.Label(header, text="MIME").grid(row=0, column=3, sticky="w", padx=(0, 12), pady=(10, 6))
        ttk.Label(header, text="Ext").grid(row=0, column=4, sticky="w", padx=(0, 12), pady=(10, 6))

        # Scrollable file list
        list_host = ttk.Frame(self.convert_frame)
        list_host.grid(row=1, column=0, sticky="nsew")
        list_host.rowconfigure(0, weight=1)
        list_host.columnconfigure(0, weight=1)

        self.convert_canvas = tkinter.Canvas(list_host, highlightthickness=0)
        self.convert_canvas.grid(row=0, column=0, sticky="nsew")
        self.convert_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.convert_canvas.yview)
        self.convert_scroll.grid(row=0, column=1, sticky="ns")
        self.convert_canvas.configure(yscrollcommand=self.convert_scroll.set)

        self.convert_list_frame = ttk.Frame(self.convert_canvas)
        self._convert_list_window = self.convert_canvas.create_window((0, 0), window=self.convert_list_frame, anchor="nw")

        self.convert_list_frame.bind("<Configure>", self._on_convert_list_configure)
        self.convert_canvas.bind("<Configure>", self._on_convert_canvas_configure)

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

    def _on_convert_canvas_configure(self, event) -> None:
        # Make inner frame match canvas width so filename column can shrink.
        self.convert_canvas.itemconfigure(self._convert_list_window, width=event.width)

    def _set_selected_paths(self, paths: list[str]) -> None:
        # Preserve order and uniqueness.
        seen: set[str] = set()
        self.selected_paths = []
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            self.selected_paths.append(p)
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

        # Update row visual highlight (bold filename)
        for path, widgets in self._convert_rows.items():
            name_label = widgets.get("name")
            if isinstance(name_label, ttk.Label):
                name_label.configure(font=(self.font_bold if path in set(self.selected_paths) else self.font_normal))

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

        for item_id in sel:
            dropped_file = self._find_dropped_by_path(item_id)
            if dropped_file is None:
                continue
            self.jobs.append(
                ConversionJob(
                    source_path=dropped_file.path,
                    source_name=dropped_file.name,
                    target_ext=target_ext,
                )
            )
        self._refresh_result_list()
        self.notebook.select(self.result_frame)

    # -------------------- Result Tab --------------------
    def _build_result_tab(self) -> None:
        container = ttk.Frame(self.result_frame)
        container.pack(fill="both", expand=True)

        columns = ("name", "target", "status")
        self.result_tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        self.result_tree.heading("name", text="File")
        self.result_tree.heading("target", text="To")
        self.result_tree.heading("status", text="Status")
        self.result_tree.column("name", width=600, anchor="w")
        self.result_tree.column("target", width=100, anchor="center")
        self.result_tree.column("status", width=140, anchor="center")
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
            row = ttk.Frame(self.convert_list_frame)
            row.grid(row=row_idx, column=0, sticky="ew")
            row.columnconfigure(0, weight=1)

            name_label = ttk.Label(row, text=f.name, anchor="w")
            name_label.grid(row=0, column=0, sticky="ew", padx=(12, 6), pady=6)

            remove_label = ttk.Label(row, image=self.remove_icon)
            remove_label.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=6)
            remove_label.bind("<Button-1>", lambda _e, p=f.path: self._remove_paths({p}))

            size_label = ttk.Label(row, text=_human_size(f.size_bytes), anchor="e")
            size_label.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=6)

            mime_label = ttk.Label(row, text=f.mime, anchor="w")
            mime_label.grid(row=0, column=3, sticky="w", padx=(0, 12), pady=6)

            ext_label = ttk.Label(row, text=f.ext, anchor="w")
            ext_label.grid(row=0, column=4, sticky="w", padx=(0, 12), pady=6)

            # Click anywhere on row (except the remove icon) to select.
            def _select(_event=None, p=f.path) -> None:
                self._set_selected_paths([p])

            row.bind("<Button-1>", _select)
            name_label.bind("<Button-1>", _select)
            size_label.bind("<Button-1>", _select)
            mime_label.bind("<Button-1>", _select)
            ext_label.bind("<Button-1>", _select)

            self._convert_rows[f.path] = {
                "row": row,
                "name": name_label,
                "remove": remove_label,
            }

        self._update_convert_selection_ui()

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
        for idx, job in enumerate(self.jobs, start=1):
            self.result_tree.insert("", "end", iid=str(idx), values=(job.source_name, job.target_ext, job.status))

    def run(self) -> None:
        self.root.mainloop()

def create_window() -> None:
    ConvertableApp().run()

if __name__ == "__main__":
    create_window()