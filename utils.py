import os
import mimetypes
import subprocess
import time
import select
from pathlib import Path
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
        candidate = os.path.join(self.outputPath, f"{base}.{ext}")
        if not os.path.exists(candidate):
            return candidate
        n = 1
        while True:
            cand = os.path.join(self.outputPath, f"{base} ({n}).{ext}")
            if not os.path.exists(cand):
                return cand
            n += 1

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

        # MOV -> MP4 is the most common path and MoviePy can hang during finalize/mux.
        # Use ffmpeg directly for reliability and real progress.
        if ext in {"mp4", "m4v"}:
            ffmpeg = self._get_ffmpeg_exe()
            duration = self._ffprobe_duration_seconds(ffmpeg, inputFile)
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-loglevel",
                "info",
                "-stats_period",
                "0.1",
                "-i",
                inputFile,
                "-map",
                "0:v:0?",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                outputFile,
            ]
            self._run_ffmpeg_with_progress(cmd, duration, progress)
        else:
            # Fallback for other formats.
            with VideoFileClip(inputFile) as clip:
                clip.write_videofile(outputFile, ffmpeg_params=["-y"])

        if progress:
            progress(1.0)

        return outputFile

    def _get_ffmpeg_exe(self) -> str:
        # Prefer MoviePy/imageio-managed ffmpeg binary when available.
        try:
            import imageio_ffmpeg  # type: ignore

            return str(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass

        # Fallback to system ffmpeg.
        return "ffmpeg"

    def _get_ffprobe_exe(self, ffmpeg_exe: str) -> str:
        # Try sibling ffprobe next to ffmpeg path.
        try:
            p = Path(ffmpeg_exe)
            sib = p.with_name("ffprobe")
            if sib.exists():
                return str(sib)
        except Exception:
            pass

        # Fallback to system ffprobe.
        return "ffprobe"

    def _ffprobe_duration_seconds(self, ffmpeg_exe: str, inputFile: str) -> float | None:
        ffprobe = self._get_ffprobe_exe(ffmpeg_exe)
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            inputFile,
        ]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
            if not out:
                return None
            return float(out)
        except Exception:
            return None

    def _run_ffmpeg_with_progress(self, cmd: list[str], duration_s: float | None, progress: ProgressCallback | None) -> None:
        def _parse_hhmmss(s: str) -> float | None:
            try:
                parts = s.split(":")
                if len(parts) != 3:
                    return None
                hh = int(parts[0])
                mm = int(parts[1])
                ss = float(parts[2])
                return hh * 3600.0 + mm * 60.0 + ss
            except Exception:
                return None

        output_path = str(cmd[-1])
        log_path = output_path + ".ffmpeg.log"

        # Important: merge stderr into stdout to avoid deadlocks (stderr can fill its buffer).
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None

        output_tail: list[str] = []
        last_any_output = time.time()
        last_frac = 0.02

        last_out_time_s: float = 0.0
        last_out_time_advance = time.time()

        last_size_bytes: int | None = None
        last_size_advance = time.time()

        stall_timeout_s = 180
        out_time_stall_timeout_s = 90
        file_stall_timeout_s = 120

        log_fp = None
        try:
            try:
                log_fp = open(log_path, "w", encoding="utf-8")
                log_fp.write("COMMAND:\n" + " ".join(cmd) + "\n")
                log_fp.write(f"DURATION_S={duration_s}\n")
                log_fp.write("---\n")
                log_fp.flush()
            except Exception:
                log_fp = None

            while True:
                if proc.poll() is not None:
                    break

                # Read all currently-available lines without blocking too long.
                r, _w, _x = select.select([proc.stdout], [], [], 0.1)
                if r:
                    while True:
                        r2, _w2, _x2 = select.select([proc.stdout], [], [], 0)
                        if not r2:
                            break
                        raw = proc.stdout.readline()
                        if not raw:
                            break
                        line = raw.strip()
                        if not line:
                            continue

                        last_any_output = time.time()
                        output_tail.append(line)
                        if len(output_tail) > 60:
                            output_tail = output_tail[-60:]

                        if log_fp is not None:
                            try:
                                log_fp.write(line + "\n")
                            except Exception:
                                pass

                        now = time.time()

                        # Parse ffmpeg -progress keys.
                        if line.startswith("out_time_us="):
                            try:
                                out_us = int(line.split("=", 1)[1])
                                out_s = out_us / 1_000_000.0
                                if out_s > last_out_time_s + 0.01:
                                    last_out_time_s = out_s
                                    last_out_time_advance = now
                                if duration_s and duration_s > 0 and progress is not None:
                                    frac = max(0.0, min(1.0, out_s / duration_s))
                                    mapped = 0.02 + frac * 0.97
                                    last_frac = max(last_frac, mapped)
                                    progress(min(0.99, last_frac))
                            except Exception:
                                pass
                            continue

                        if line.startswith("out_time_ms="):
                            try:
                                out_ms = int(line.split("=", 1)[1])
                                out_s = out_ms / 1_000_000.0
                                if out_s > last_out_time_s + 0.01:
                                    last_out_time_s = out_s
                                    last_out_time_advance = now
                                if duration_s and duration_s > 0 and progress is not None:
                                    frac = max(0.0, min(1.0, out_s / duration_s))
                                    mapped = 0.02 + frac * 0.97
                                    last_frac = max(last_frac, mapped)
                                    progress(min(0.99, last_frac))
                            except Exception:
                                pass
                            continue

                        if line.startswith("out_time="):
                            sec = _parse_hhmmss(line.split("=", 1)[1])
                            if sec is not None:
                                if sec > last_out_time_s + 0.01:
                                    last_out_time_s = sec
                                    last_out_time_advance = now
                                if duration_s and duration_s > 0 and progress is not None:
                                    frac = max(0.0, min(1.0, sec / duration_s))
                                    mapped = 0.02 + frac * 0.97
                                    last_frac = max(last_frac, mapped)
                                    progress(min(0.99, last_frac))
                            continue

                        if line.startswith("progress="):
                            if line.split("=", 1)[1] == "end":
                                break
                            if duration_s is None and progress is not None:
                                # No duration: nudge forward a bit on each progress block.
                                last_frac = min(0.99, last_frac + 0.002)
                                progress(last_frac)
                            continue

                else:
                    # No output this tick.
                    if duration_s is None and progress is not None and last_frac < 0.99:
                        last_frac = min(0.99, last_frac + 0.001)
                        progress(last_frac)

                # Stall detector A: out_time stops advancing (works even without duration).
                if last_out_time_s > 0 and time.time() - last_out_time_advance > out_time_stall_timeout_s:
                    proc.kill()
                    tail = "\n".join(output_tail[-20:]).strip()
                    dur_txt = f" / {duration_s:.3f}s" if duration_s else ""
                    raise RuntimeError(
                        "ffmpeg progress stalled (out_time not advancing)\n\n"
                        f"Last out_time: {last_out_time_s:.3f}s{dur_txt}\n\n"
                        f"ffmpeg log: {log_path}\n\n"
                        + ("Last ffmpeg output:\n" + tail if tail else "(no output)")
                    )

                # Stall detector B: output file stops growing.
                try:
                    if os.path.exists(output_path):
                        sz = os.path.getsize(output_path)
                        if last_size_bytes is None:
                            last_size_bytes = sz
                            last_size_advance = time.time()
                        elif sz > (last_size_bytes or 0):
                            last_size_bytes = sz
                            last_size_advance = time.time()
                        elif time.time() - last_size_advance > file_stall_timeout_s:
                            proc.kill()
                            tail = "\n".join(output_tail[-20:]).strip()
                            raise RuntimeError(
                                "ffmpeg stalled (output file not growing)\n\n"
                                f"Output size: {sz} bytes\n\n"
                                f"ffmpeg log: {log_path}\n\n"
                                + ("Last ffmpeg output:\n" + tail if tail else "(no output)")
                            )
                except Exception:
                    pass

                # Stall detector C: no output at all for too long.
                if time.time() - last_any_output > stall_timeout_s:
                    proc.kill()
                    tail = "\n".join(output_tail[-20:]).strip()
                    if tail:
                        raise RuntimeError(
                            "ffmpeg stalled (no output)\n\n"
                            f"ffmpeg log: {log_path}\n\n"
                            "Last ffmpeg output:\n" + tail
                        )
                    raise RuntimeError(f"ffmpeg stalled (no output)\n\nffmpeg log: {log_path}")

            rc = proc.wait()
            if rc != 0:
                tail = "\n".join(output_tail[-20:]).strip()
                raise RuntimeError(tail or f"ffmpeg failed with exit code {rc}\n\nffmpeg log: {log_path}")
        finally:
            if log_fp is not None:
                try:
                    log_fp.write(f"EXIT_CODE={proc.poll()}\n")
                    log_fp.flush()
                    log_fp.close()
                except Exception:
                    pass

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
