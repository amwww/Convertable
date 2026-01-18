from __future__ import annotations

import mimetypes
import os
import sys
import tkinter
from pathlib import Path

from models import DroppedFile


_ICON_IMAGE: tkinter.PhotoImage | None = None


def resource_base_dir() -> Path:
    """Return the directory used as the app's resource root.

    When packaged with PyInstaller, files are extracted under `sys._MEIPASS`.
    During development, we use the project directory.
    """
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

def set_window_icon(root: tkinter.Tk) -> None:
    """Set the app window icon.

    Tk requires keeping a reference to the PhotoImage object alive; we store it
    in a module-level cache rather than attaching an untyped attribute to `root`.
    """
    global _ICON_IMAGE
    icon_path = resource_base_dir() / "assets" / "icon.png"
    _ICON_IMAGE = tkinter.PhotoImage(file=str(icon_path))
    root.iconphoto(True, _ICON_IMAGE)

def human_size(num_bytes: int | None) -> str:
    """Format a byte count as a human-readable string (e.g. "1.2 MB")."""
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
    """Infer a file's MIME type from its extension (best-effort)."""
    if is_dir:
        return "Folder"
    lowered = ext.lower()
    guessed, _encoding = mimetypes.guess_type(f"file{lowered}", strict=False)
    return guessed or "application/octet-stream"

def to_dropped_file(path: str) -> DroppedFile:
    """Build a `DroppedFile` model from a filesystem path."""
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

def parse_dnd_files(root: tkinter.Misc, event_data: str) -> list[str]:
    """Parse `tkinterdnd2` event data into a list of paths.

    `event_data` is a Tcl list; `splitlist()` handles braces and whitespace.
    """
    # event.data is a Tcl list of file paths; splitlist handles spaces/braces.
    return [str(p) for p in root.tk.splitlist(event_data) if p]