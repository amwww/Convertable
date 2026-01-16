from __future__ import annotations

"""Small data models shared between the UI and conversion engine."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DroppedFile:
    """Metadata for a file/folder dropped into the app."""

    path: str
    name: str
    size_bytes: int | None
    mime: str
    ext: str

@dataclass
class ConversionJob:
    """A queued/active conversion request."""

    source_path: str
    source_name: str
    target_ext: str
    status: str = "Queued"

@dataclass
class ConversionResultItem:
    """A completed conversion output shown in the Result tab."""

    source_path: str
    source_name: str
    output_path: str
    target_ext: str
