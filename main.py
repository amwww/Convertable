import tkinter
from typing import Iterable
from tkinterdnd2 import DND_FILES, TkinterDnD
from pathlib import Path


def _set_window_icon(root: tkinter.Tk) -> None:
    base_dir = Path(__file__).resolve().parent
    icon_path = base_dir / "assets" / "icon.png"
    icon_image = tkinter.PhotoImage(file=str(icon_path))
    root.iconphoto(True, icon_image)
    root._icon_image = icon_image  # type: ignore[attr-defined]

def _format_dropped_files(paths: Iterable[str]) -> str:
    paths = [p for p in paths if p]
    if not paths:
        return "No files dropped."
    if len(paths) == 1:
        return f"Dropped file:\n{paths[0]}"
    return "Dropped files:\n" + "\n".join(paths)

def create_window() -> None:
    root = TkinterDnD.Tk()
    root.title("Convertable")
    root.geometry("600x400")
    root.resizable(False, False)

    _set_window_icon(root)

    default_font = ("Inter", 16)
    root.option_add("*Font", default_font)

    label = tkinter.Label(root, text="Convertable")
    label.pack(pady=(20, 10))

    instructions_text = "Drag and drop file(s) onto this window."

    instructions = tkinter.Label(root, text=instructions_text, justify="center")
    instructions.pack(pady=(0, 16))

    dropped_label = tkinter.Label(root, text="", justify="left", wraplength=560)
    dropped_label.pack(padx=20, pady=(0, 20), fill="x")

    def on_drop(event) -> None:
        # event.data is a Tcl list of file paths; splitlist handles spaces/braces.
        try:
            files = root.tk.splitlist(event.data)
        except Exception:
            files = (str(event.data),)
        dropped_label.config(text=_format_dropped_files(files))

    root.drop_target_register(DND_FILES)
    root.dnd_bind("<<Drop>>", on_drop)

    root.mainloop()

if __name__ == "__main__":
    create_window()