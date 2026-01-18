from __future__ import annotations

import tkinter


class AppBase:
    """Typing-only base shared by tab mixins.

    The tab mixins are imported as standalone modules, and Pylance type-checks
    them in isolation. The real attributes are provided by the concrete app
    class (in `main.py`).

    This base collects a few shared attributes to avoid multiple-inheritance
    conflicts (e.g. several mixins each declaring `root`).
    """

    root: tkinter.Misc

    def _debug_log(self, msg: str) -> None:
        """Write a line to the app's debug log (implemented by the app)."""
        ...
