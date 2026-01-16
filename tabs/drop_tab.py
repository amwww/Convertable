from __future__ import annotations

from tkinter import ttk


class DropTabMixin:
    # -------------------- Drop Tab --------------------
    def _build_drop_tab(self) -> None:
        """Create the Drop tab UI (simple drag-and-drop instructions)."""
        instructions = ttk.Label(
            self.drop_frame,
            text="Drag and drop file(s) onto this window.",
            justify="center",
        )
        instructions.pack(expand=True)
