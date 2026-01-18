"""UI tab mixins.

These mixins keep ConvertableApp small(er) by moving tab-specific UI code
into separate modules, while still attaching methods to the main app class.
"""

from .convert_tab import ConvertTabMixin
from .queue_tab import QueueTabMixin
from .result_tab import ResultTabMixin

__all__ = [
    "ConvertTabMixin",
    "QueueTabMixin",
    "ResultTabMixin",
]
