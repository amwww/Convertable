import os
import mimetypes
import filetype as ft
from PIL import Image
from pydub import AudioSegment
from moviepy.editor import VideoFileClip

class FileConverter:

    def __init__(self, outputPath: str):
        self.outputPath = outputPath

    def ensureOutputPath(self):
        os.makedirs(self.outputPath, exist_ok=True)

    def buildOutputPath(self, inputFile: str, outputFiletype: str) -> str:
        base = os.path.splitext(os.path.basename(inputFile))[0]
        ext = outputFiletype.lower().lstrip('.')
        return os.path.join(self.outputPath, f"{base}.{ext}")

    def convertImage(self, inputFile: str, outputFiletype: str) -> str:
        self.ensureOutputPath()
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        with Image.open(inputFile) as img:
            fmt = outputFiletype.upper()
            if fmt in {"JPEG", "JPG"} and img.mode in {"RGBA", "P"}:
                img = img.convert("RGB")
            img.save(outputFile, format=fmt)

        return outputFile

    def convertAudio(self, inputFile: str, outputFiletype: str) -> str:
        self.ensureOutputPath()
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        audio = AudioSegment.from_file(inputFile)
        audio.export(outputFile, format=outputFiletype.lower().lstrip('.'))

        return outputFile

    def convertVideo(self, inputFile: str, outputFiletype: str) -> str:
        self.ensureOutputPath()
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        ext = outputFiletype.lower().lstrip('.')
        with VideoFileClip(inputFile) as clip:
            if ext in {"mp4", "m4v"}:
                clip.write_videofile(outputFile, codec="libx264", audio_codec="aac")
            else:
                clip.write_videofile(outputFile)

        return outputFile

    def convertFile(self, inputFile: str, outputFiletype: str) -> str:
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
            return self.convertImage(inputFile, outputExt)
        if inputCategory == "audio":
            return self.convertAudio(inputFile, outputExt)
        if inputCategory == "video":
            return self.convertVideo(inputFile, outputExt)

        raise ValueError("Conversion category not implemented.")


def convertFile(inputFile, outputFiletype, outputPath): #Just convience you can use this one
    converter = FileConverter(outputPath)
    return converter.convertFile(inputFile, outputFiletype)


def main():
    testFile = '/Users/adrian/Desktop/eh v2/Convertable/convertableIcon.png'
    converter = FileConverter('/Users/adrian/Desktop/eh v2/Convertable/testing/outputs')
    output = converter.convertFile(testFile, 'jpeg')
    print(f'Converted to: {output}')


if __name__ == "__main__":
    main()
