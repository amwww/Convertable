import os
import sys
import tkinter
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from tkinter import ttk

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

        default_font = ("Inter", 14)
        self.root.option_add("*Font", default_font)
        _set_window_icon(self.root)

        self.dropped: list[DroppedFile] = []
        self.jobs: list[ConversionJob] = []

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
        container = ttk.Frame(self.convert_frame)
        container.pack(fill="both", expand=True)

        left = ttk.Frame(container)
        left.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(container, width=260)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        columns = ("name", "size", "mime", "ext")
        self.convert_tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        self.convert_tree.heading("name", text="File")
        self.convert_tree.heading("size", text="Size")
        self.convert_tree.heading("mime", text="MIME")
        self.convert_tree.heading("ext", text="Ext")
        self.convert_tree.column("name", width=430, anchor="w")
        self.convert_tree.column("size", width=110, anchor="e")
        self.convert_tree.column("mime", width=200, anchor="w")
        self.convert_tree.column("ext", width=90, anchor="center")
        self.convert_tree.pack(fill="both", expand=True)

        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.convert_tree.yview)
        tree_scroll.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self.convert_tree.configure(yscrollcommand=tree_scroll.set)

        self.convert_tree.bind("<<TreeviewSelect>>", self._on_convert_selection)

        ttk.Label(right, text="Actions").pack(anchor="w", pady=(12, 8), padx=12)

        self.selected_file_label = ttk.Label(right, text="Select file(s)", wraplength=240, justify="left")
        self.selected_file_label.pack(anchor="w", pady=(0, 12), padx=12)

        ttk.Label(right, text="Convert to").pack(anchor="w", padx=12)
        self._all_convert_options = [
            ".PNG",
            ".JPEG",
            ".WEBP",
            ".MP4",
            ".MOV",
        ]
        self.convert_to_var = tkinter.StringVar(value=self._all_convert_options[0])
        self.convert_to = ttk.Combobox(
            right,
            textvariable=self.convert_to_var,
            values=self._all_convert_options,
            state="normal",
        )
        self.convert_to.pack(fill="x", pady=(2, 12), padx=12)
        self.convert_to.bind("<KeyRelease>", self._filter_convert_options)

        self.remove_btn = ttk.Button(right, text="Remove", command=self._remove_selected)
        self.remove_btn.pack(fill="x", pady=(0, 8), padx=12)

        self.convert_btn = ttk.Button(right, text="Convert", command=self._queue_conversion)
        self.convert_btn.pack(fill="x", padx=12)

        self._set_action_enabled(False)

    def _set_action_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.remove_btn.configure(state=state)
        self.convert_btn.configure(state=state)
        self.convert_to.configure(state=("normal" if enabled else "disabled"))

    def _on_convert_selection(self, _event=None) -> None:
        sel = list(self.convert_tree.selection())
        if not sel:
            self.selected_file_label.configure(text="Select file(s)")
            self._set_action_enabled(False)
            return
        if len(sel) > 1:
            self.selected_file_label.configure(text=f"{len(sel)} files selected")
            self._set_action_enabled(True)
            self._set_convert_options_for_selection(sel)
            return

        item_id = sel[0]
        dropped_file = self._find_dropped_by_path(item_id)
        if dropped_file is None:
            self.selected_file_label.configure(text="Select file(s)")
            self._set_action_enabled(False)
            return

        self.selected_file_label.configure(text=dropped_file.name)
        self._set_action_enabled(True)
        self._set_convert_options_for_kind(dropped_file.mime)

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
        sel = self.convert_tree.selection()
        if not sel:
            return
        selected_paths = set(sel)
        self.dropped = [f for f in self.dropped if f.path not in selected_paths]
        self.jobs = [j for j in self.jobs if j.source_path not in selected_paths]
        self._refresh_all_lists()

    def _queue_conversion(self) -> None:
        sel = list(self.convert_tree.selection())
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
            self.convert_tree.selection_set(new_paths)
            self.convert_tree.focus(new_paths[0])
            self.convert_tree.see(new_paths[0])

    def _refresh_all_lists(self) -> None:
        self._refresh_convert_list()
        self._refresh_result_list()

    def _refresh_convert_list(self) -> None:
        for item in self.convert_tree.get_children(""):
            self.convert_tree.delete(item)
        for f in self.dropped:
            # Use iid as the full path for stable mapping.
            self.convert_tree.insert(
                "",
                "end",
                iid=f.path,
                values=(f.name, _human_size(f.size_bytes), f.mime, f.ext),
            )
        self._set_action_enabled(False)
        self.selected_file_label.configure(text="Select file(s)")

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