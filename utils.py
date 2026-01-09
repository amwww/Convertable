import os
import mimetypes
import filetype as ft
from PIL import Image
from pydub import AudioSegment
from moviepy.editor import VideoFileClip
from typing import Callable


ProgressCallback = Callable[[float], None]

class FileConverter:

    def __init__(self, outputPath: str):
        self.outputPath = outputPath

    def ensureOutputPath(self):
        os.makedirs(self.outputPath, exist_ok=True)

    def buildOutputPath(self, inputFile: str, outputFiletype: str) -> str:
        base = os.path.splitext(os.path.basename(inputFile))[0]
        ext = outputFiletype.lower().lstrip('.')
        return os.path.join(self.outputPath, f"{base}.{ext}")

    def convertImage(self, inputFile: str, outputFiletype: str, progress: ProgressCallback | None = None) -> str:
        self.ensureOutputPath()
        if progress:
            progress(0.1)
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        with Image.open(inputFile) as img:
            fmt = outputFiletype.upper()
            if fmt in {"JPEG", "JPG"} and img.mode in {"RGBA", "P"}:
                img = img.convert("RGB")
            img.save(outputFile, format=fmt)

        if progress:
            progress(1.0)

        return outputFile

    def convertAudio(self, inputFile: str, outputFiletype: str, progress: ProgressCallback | None = None) -> str:
        self.ensureOutputPath()
        if progress:
            progress(0.05)
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        audio = AudioSegment.from_file(inputFile)
        if progress:
            progress(0.5)
        audio.export(outputFile, format=outputFiletype.lower().lstrip('.'))

        if progress:
            progress(1.0)

        return outputFile

    def convertVideo(self, inputFile: str, outputFiletype: str, progress: ProgressCallback | None = None) -> str:
        self.ensureOutputPath()
        if progress:
            progress(0.02)
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        ext = outputFiletype.lower().lstrip('.')
        with VideoFileClip(inputFile) as clip:
            if ext in {"mp4", "m4v"}:
                clip.write_videofile(outputFile, codec="libx264", audio_codec="aac")
            else:
                clip.write_videofile(outputFile)

        if progress:
            progress(1.0)

        return outputFile

    def convertFile(self, inputFile: str, outputFiletype: str, progress: ProgressCallback | None = None) -> str:
        kind = ft.guess(inputFile)
        if kind is None:
            raise ValueError("Could not detect input file type.")

        inputExt = kind.extension
        inputMime = kind.mime or ""
        inputCategory = inputMime.split("/", 1)[0] if "/" in inputMime else None

        outputExt = outputFiletype.lower().lstrip('.')
        fakeName = f"dummy.{outputExt}"
        outputMime, _ = mimetypes.guess_type(fakeName)
        outputMime = outputMime or ""
        outputCategory = outputMime.split("/", 1)[0] if "/" in outputMime else None

        if inputCategory is None:
            raise ValueError(f"Unsupported input file type: {inputExt}")
        if outputCategory is None:
            raise ValueError(f"Unsupported output file type: {outputExt}")
        if inputCategory != outputCategory:
            raise ValueError(
                f"Cannot convert from {inputCategory} ({inputExt}) to {outputCategory} ({outputExt})."
            )

        if inputCategory == "image":
            return self.convertImage(inputFile, outputExt, progress=progress)
        if inputCategory == "audio":
            return self.convertAudio(inputFile, outputExt, progress=progress)
        if inputCategory == "video":
            return self.convertVideo(inputFile, outputExt, progress=progress)

        raise ValueError("Conversion category not implemented.")

def convertFile(inputFile: str, outputFiletype: str, outputPath: str, progress: ProgressCallback | None = None) -> str:
    """Convenience wrapper for one-off conversions."""
    converter = FileConverter(outputPath)
    return converter.convertFile(inputFile, outputFiletype, progress=progress)
