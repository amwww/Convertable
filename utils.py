import os
import mimetypes
import subprocess
import time
import select
import sys
import tempfile
import io
from pathlib import Path
import threading
import xml.etree.ElementTree as ET
import filetype as ft
from PIL import Image, ImageChops, UnidentifiedImageError
from pydub import AudioSegment
from moviepy.editor import VideoFileClip
from typing import Callable

ProgressCallback = Callable[[float], None]
CancelCallback = Callable[[], bool]

_ACTIVE_PROCS: set[subprocess.Popen] = set()
_ACTIVE_PROCS_LOCK = threading.Lock()


def _register_active_proc(proc: subprocess.Popen) -> None:
    try:
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.add(proc)
    except Exception:
        pass


def _unregister_active_proc(proc: subprocess.Popen) -> None:
    try:
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_PROCS.discard(proc)
    except Exception:
        pass


def terminate_active_processes() -> None:
    """Best-effort kill of any subprocesses started for conversions (ffmpeg)."""
    try:
        with _ACTIVE_PROCS_LOCK:
            procs = list(_ACTIVE_PROCS)
    except Exception:
        procs = []

    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


def _svg_intrinsic_px_size(svg_path: str) -> tuple[int | None, int | None]:
    """Best-effort intrinsic SVG size in pixels.

    Prefers explicit width/height attributes; falls back to viewBox.
    Uses 96 DPI for physical units, per common SVG conventions.
    """

    def _parse_len(val: str | None) -> float | None:
        if not val:
            return None
        s = str(val).strip().lower()
        if not s or s.endswith("%"):
            return None

        # Extract number + unit.
        num = ""
        unit = ""
        for ch in s:
            if (ch.isdigit() or ch in {".", "+", "-"}) and not unit:
                num += ch
            else:
                unit += ch
        try:
            n = float(num)
        except Exception:
            return None
        unit = unit.strip()
        if unit in {"", "px"}:
            return n
        # Convert physical units to px at 96 DPI.
        if unit == "in":
            return n * 96.0
        if unit == "pt":
            return n * (96.0 / 72.0)
        if unit == "pc":
            return n * 16.0
        if unit == "cm":
            return n * (96.0 / 2.54)
        if unit == "mm":
            return n * (96.0 / 25.4)
        return None

    try:
        root = ET.parse(svg_path).getroot()
        w = _parse_len(root.attrib.get("width"))
        h = _parse_len(root.attrib.get("height"))
        if w and h and w > 0 and h > 0:
            return int(round(w)), int(round(h))

        vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
        if vb:
            parts = [p for p in vb.replace(",", " ").split() if p]
            if len(parts) == 4:
                try:
                    vb_w = float(parts[2])
                    vb_h = float(parts[3])
                    if vb_w > 0 and vb_h > 0:
                        return int(round(vb_w)), int(round(vb_h))
                except Exception:
                    pass
    except Exception:
        pass
    return None, None


def _svg_bytes_with_padded_viewbox(svg_path: str, pad_frac: float = 0.02) -> bytes | None:
    """Return modified SVG bytes with an expanded viewBox to reduce clipping.

    Many renderers clip strictly to the viewBox. Some SVGs have strokes/filters that extend
    slightly outside it, which can look like the image is "cut off" after rasterization.
    """
    try:
        raw = Path(svg_path).read_bytes()
    except Exception:
        return None

    try:
        # Parse bytes so we preserve encoding and avoid failing on XML declarations.
        root = ET.fromstring(raw)
    except Exception:
        return None

    # Helper to parse viewBox numbers.
    def _parse_vb(vb: str) -> tuple[float, float, float, float] | None:
        parts = [p for p in vb.replace(",", " ").split() if p]
        if len(parts) != 4:
            return None
        try:
            return float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
        except Exception:
            return None

    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    vb_vals = _parse_vb(vb) if vb else None

    # If no viewBox, synthesize one from width/height when possible.
    if vb_vals is None:
        w_px, h_px = _svg_intrinsic_px_size(svg_path)
        if w_px and h_px and w_px > 0 and h_px > 0:
            vb_vals = (0.0, 0.0, float(w_px), float(h_px))

    if vb_vals is None:
        return None

    x, y, w, h = vb_vals
    if w <= 0 or h <= 0:
        return None

    pad = max(1.0, min(w, h) * float(pad_frac))
    x2 = x - pad
    y2 = y - pad
    w2 = w + pad * 2.0
    h2 = h + pad * 2.0

    root.attrib["viewBox"] = f"{x2:g} {y2:g} {w2:g} {h2:g}"

    try:
        return ET.tostring(root, encoding="utf-8")
    except Exception:
        return None


def _svg_has_explicit_size(svg_path: str) -> bool:
    """True when the root <svg> has parseable width+height."""
    try:
        raw = Path(svg_path).read_bytes()
        root = ET.fromstring(raw)
        if not root.attrib.get("width") or not root.attrib.get("height"):
            return False
        w, h = _svg_intrinsic_px_size(svg_path)
        return bool(w and h)
    except Exception:
        return False


def _letterbox_to_size(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale uniformly to fit within (target_w, target_h) and pad to exact size."""
    try:
        tw = int(target_w)
        th = int(target_h)
        if tw <= 0 or th <= 0:
            return img
    except Exception:
        return img

    try:
        iw, ih = img.size
    except Exception:
        return img

    if iw <= 0 or ih <= 0:
        return img

    # Add a tiny inset to reduce the chance of edge clipping (e.g. strokes/filters that
    # sit exactly on the SVG viewport boundary). Keep it small so we don't noticeably
    # shrink typical icons.
    inset = int(round(min(tw, th) * 0.01))
    inset = max(0, min(4, inset))
    avail_w = max(1, tw - inset * 2)
    avail_h = max(1, th - inset * 2)

    # Compute uniform scale.
    scale = min(float(avail_w) / float(iw), float(avail_h) / float(ih))
    if scale <= 0:
        return img

    new_w = max(1, int(round(iw * scale)))
    new_h = max(1, int(round(ih * scale)))
    # Clamp to available box to avoid any float/rounding overshoot.
    new_w = min(avail_w, new_w)
    new_h = min(avail_h, new_h)

    if new_w == iw and new_h == ih and iw == tw and ih == th:
        return img

    resampling = getattr(Image, "Resampling", Image)
    resample_filter = getattr(resampling, "LANCZOS", getattr(Image, "BICUBIC", 3))
    resized = img.resize((new_w, new_h), resample_filter)

    # Pad to exact size, centered.
    mode = "RGBA" if (resized.mode in {"RGBA", "LA"} or "transparency" in getattr(resized, "info", {})) else "RGB"
    background = (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0)
    canvas = Image.new(mode, (tw, th), background)
    x = inset + int((avail_w - new_w) / 2)
    y = inset + int((avail_h - new_h) / 2)
    if mode == "RGBA":
        canvas.paste(resized.convert("RGBA"), (x, y), resized.convert("RGBA"))
    else:
        canvas.paste(resized.convert("RGB"), (x, y))
    return canvas


def _center_crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop an image to match the aspect ratio of (target_w, target_h)."""
    try:
        tw = int(target_w)
        th = int(target_h)
        if tw <= 0 or th <= 0:
            return img
    except Exception:
        return img

    try:
        w, h = img.size
    except Exception:
        return img
    if w <= 0 or h <= 0:
        return img

    target_aspect = float(tw) / float(th)
    cur_aspect = float(w) / float(h)

    # If we're already close enough, don't crop.
    if abs(cur_aspect - target_aspect) < 1e-3:
        return img

    if cur_aspect > target_aspect:
        # Too wide -> crop width.
        new_w = max(1, int(round(h * target_aspect)))
        new_w = min(new_w, w)
        left = int((w - new_w) / 2)
        return img.crop((left, 0, left + new_w, h))
    else:
        # Too tall -> crop height.
        new_h = max(1, int(round(w / target_aspect)))
        new_h = min(new_h, h)
        top = int((h - new_h) / 2)
        return img.crop((0, top, w, top + new_h))


def _crop_thumbnail_to_target(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Crop a renderer thumbnail to the target aspect ratio using content bounds.

    Content-aware crop: Quick Look can add uneven padding. Using a simple per-pixel
    color from corners, find the bounding box of non-background pixels, then crop to the
    target aspect ratio around the content center.
    """
    try:
        tw = int(target_w)
        th = int(target_h)
        if tw <= 0 or th <= 0:
            return img
    except Exception:
        return img

    rgba = img.convert("RGBA")
    w, h = rgba.size
    if w <= 0 or h <= 0:
        return img

    # Detect padding per-side (Quick Look can pad unevenly, e.g. only on the right).
    try:
        small = rgba.resize((128, 128), Image.BILINEAR)
        sw, sh = small.size
        band = max(1, min(12, int(min(sw, sh) * 0.08)))
        spx = small.load()

        def _mode(pixels: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
            if not pixels:
                return rgba.getpixel((0, 0))
            counts: dict[tuple[int, int, int, int], int] = {}
            for p in pixels:
                counts[p] = counts.get(p, 0) + 1
            return max(counts, key=counts.get)

        left_bg = _mode([spx[x, y] for x in range(band) for y in range(sh)])
        right_bg = _mode([spx[sw - 1 - x, y] for x in range(band) for y in range(sh)])
        top_bg = _mode([spx[x, y] for y in range(band) for x in range(sw)])
        bottom_bg = _mode([spx[x, sh - 1 - y] for y in range(band) for x in range(sw)])
    except Exception:
        left_bg = right_bg = top_bg = bottom_bg = rgba.getpixel((0, 0))

    max_scan = 512
    scale = min(1.0, max_scan / max(w, h))
    dw = max(1, int(w * scale))
    dh = max(1, int(h * scale))
    scan = rgba.resize((dw, dh), Image.BILINEAR)
    px = scan.load()

    tol = 18
    min_frac = 0.01
    step = 2

    def _like(p: tuple[int, int, int, int], bg: tuple[int, int, int, int]) -> bool:
        return max(abs(p[0] - bg[0]), abs(p[1] - bg[1]), abs(p[2] - bg[2])) <= tol

    def _col_not_bg_frac(x: int, bg: tuple[int, int, int, int]) -> float:
        cnt = 0
        total = 0
        for y in range(0, dh, step):
            total += 1
            if not _like(px[x, y], bg):
                cnt += 1
        return (cnt / total) if total else 0.0

    def _row_not_bg_frac(y: int, bg: tuple[int, int, int, int]) -> float:
        cnt = 0
        total = 0
        for x in range(0, dw, step):
            total += 1
            if not _like(px[x, y], bg):
                cnt += 1
        return (cnt / total) if total else 0.0

    left_s = 0
    for x in range(dw):
        if _col_not_bg_frac(x, left_bg) >= min_frac:
            left_s = x
            break

    right_s = dw - 1
    for x in range(dw - 1, -1, -1):
        if _col_not_bg_frac(x, right_bg) >= min_frac:
            right_s = x
            break

    top_s = 0
    for y in range(dh):
        if _row_not_bg_frac(y, top_bg) >= min_frac:
            top_s = y
            break

    bottom_s = dh - 1
    for y in range(dh - 1, -1, -1):
        if _row_not_bg_frac(y, bottom_bg) >= min_frac:
            bottom_s = y
            break

    if right_s <= left_s or bottom_s <= top_s:
        return _center_crop_to_aspect(rgba, tw, th)

    inv = 1.0 / scale if scale > 0 else 1.0
    pad = 2
    left = max(0, int(left_s * inv) - pad)
    top = max(0, int(top_s * inv) - pad)
    right = min(w, int((right_s + 1) * inv) + pad)
    bottom = min(h, int((bottom_s + 1) * inv) + pad)

    trimmed = rgba.crop((left, top, right, bottom))
    return _center_crop_to_aspect(trimmed, tw, th)

class ConversionCancelled(RuntimeError):
    pass

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

    def convertImage(
        self,
        inputFile: str,
        outputFiletype: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> str:
        self.ensureOutputPath()
        if cancel and cancel():
            raise ConversionCancelled("Conversion cancelled")
        if progress:
            progress(0.1)
        outputFile = self.buildOutputPath(inputFile, outputFiletype)

        # SVG is a vector format; rasterize it first.
        if Path(inputFile).suffix.lower() == ".svg":
            if cancel and cancel():
                raise ConversionCancelled("Conversion cancelled")
            if progress:
                progress(0.25)

            target_w, target_h = _svg_intrinsic_px_size(inputFile)
            has_explicit_size = _svg_has_explicit_size(inputFile)
            # Only pad viewBox when the SVG doesn't declare its own size; padding can make
            # explicit-size artwork look like it no longer reaches the edges.
            padded_svg_bytes = None if has_explicit_size else _svg_bytes_with_padded_viewbox(inputFile)

            # Prefer CairoSVG when available: it respects width/height/viewBox and produces
            # a correctly-sized raster for most SVGs.
            try:
                import cairosvg  # type: ignore

                if cancel and cancel():
                    raise ConversionCancelled("Conversion cancelled")
                if progress:
                    progress(0.55)

                if target_w and target_h and target_w > 0 and target_h > 0:
                    if padded_svg_bytes:
                        png_bytes = cairosvg.svg2png(
                            bytestring=padded_svg_bytes,
                            output_width=int(target_w),
                            output_height=int(target_h),
                        )
                    else:
                        png_bytes = cairosvg.svg2png(
                            url=inputFile,
                            output_width=int(target_w),
                            output_height=int(target_h),
                        )
                else:
                    if padded_svg_bytes:
                        png_bytes = cairosvg.svg2png(bytestring=padded_svg_bytes)
                    else:
                        png_bytes = cairosvg.svg2png(url=inputFile)

                if not isinstance(png_bytes, (bytes, bytearray)):
                    raise TypeError("CairoSVG did not return bytes")

                with Image.open(io.BytesIO(png_bytes)) as img:
                    img.load()
                    if cancel and cancel():
                        raise ConversionCancelled("Conversion cancelled")
                    fmt = outputFiletype.upper().lstrip(".")
                    if fmt in {"JPEG", "JPG"}:
                        if img.mode in {"RGBA", "LA"}:
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                            img = bg
                        elif img.mode == "P":
                            img = img.convert("RGB")
                    img.save(outputFile, format=fmt)

                if progress:
                    progress(1.0)
                return outputFile
            except ConversionCancelled:
                raise
            except Exception:
                # Fall back to Quick Look below.
                pass

            # macOS: use built-in Quick Look to rasterize SVG (no Homebrew/system deps).
            if sys.platform == "darwin":
                try:
                    with tempfile.TemporaryDirectory(prefix="convertable-svg-") as td:
                        svg_for_render = inputFile
                        if padded_svg_bytes:
                            try:
                                tmp_svg = os.path.join(td, "_convertable_padded.svg")
                                with open(tmp_svg, "wb") as fp:
                                    fp.write(padded_svg_bytes)
                                svg_for_render = tmp_svg
                            except Exception:
                                svg_for_render = inputFile

                        max_dim = 2048
                        if target_w and target_h:
                            # Render larger than needed to preserve quality, then resize.
                            max_dim = int(max(target_w, target_h) * 2)
                        max_dim = max(64, min(4096, int(max_dim)))
                        cmd = [
                            "qlmanage",
                            "-t",
                            "-s",
                            str(max_dim),
                            "-o",
                            td,
                            svg_for_render,
                        ]
                        # qlmanage prints to stderr/stdout; suppress unless it fails.
                        proc = subprocess.run(
                            cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=20,
                            check=False,
                        )
                        if proc.returncode == 0:
                            # qlmanage writes a PNG thumbnail into the output dir.
                            thumbs = sorted(Path(td).glob("*.png"))
                            if thumbs:
                                if progress:
                                    progress(0.55)
                                with Image.open(str(thumbs[0])) as img:
                                    img.load()
                                    if cancel and cancel():
                                        raise ConversionCancelled("Conversion cancelled")
                                    if target_w and target_h and target_w > 0 and target_h > 0:
                                        try:
                                            img = _crop_thumbnail_to_target(img, int(target_w), int(target_h))
                                            resampling = getattr(Image, "Resampling", Image)
                                            resample_filter = getattr(resampling, "LANCZOS", getattr(Image, "BICUBIC", 3))
                                            img = img.resize((int(target_w), int(target_h)), resample_filter)
                                        except Exception:
                                            pass
                                    fmt = outputFiletype.upper().lstrip(".")
                                    if fmt in {"JPEG", "JPG"}:
                                        # JPEG has no alpha; use a white background.
                                        if img.mode in {"RGBA", "LA"}:
                                            bg2 = Image.new("RGB", img.size, (255, 255, 255))
                                            bg2.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                                            img = bg2
                                        elif img.mode == "P":
                                            img = img.convert("RGB")
                                    img.save(outputFile, format=fmt)
                                if progress:
                                    progress(1.0)
                                return outputFile
                except ConversionCancelled:
                    raise
                except Exception:
                    # Fall back to CairoSVG below.
                    pass

            raise RuntimeError(
                "SVG conversion needs an SVG renderer. CairoSVG is recommended for accurate sizing."
            )

        def _save_from_pil(img: Image.Image) -> None:
            fmt = outputFiletype.upper().lstrip(".")
            if fmt in {"JPEG", "JPG"} and img.mode in {"RGBA", "P"}:
                img = img.convert("RGB")
            img.save(outputFile, format=fmt)

        try:
            with Image.open(inputFile) as img:
                if cancel and cancel():
                    raise ConversionCancelled("Conversion cancelled")
                _save_from_pil(img)
        except (UnidentifiedImageError, OSError):
            # Pillow doesn't support HEIC/HEIF by default.
            # On macOS, use the built-in `sips` tool to transcode to PNG first.
            lower = inputFile.lower()
            if sys.platform == "darwin" and (lower.endswith(".heic") or lower.endswith(".heif")):
                if progress:
                    progress(0.2)
                with tempfile.TemporaryDirectory(prefix="convertable-heic-") as td:
                    if cancel and cancel():
                        raise ConversionCancelled("Conversion cancelled")
                    tmp_png = os.path.join(td, "_convertable_input.png")
                    proc = subprocess.run(
                        ["sips", "-s", "format", "png", inputFile, "--out", tmp_png],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    if proc.returncode != 0 or not os.path.exists(tmp_png):
                        raise RuntimeError(
                            f"Failed to convert HEIC via sips (exit={proc.returncode}): {proc.stderr.strip()}"
                        )
                    if progress:
                        progress(0.45)
                    with Image.open(tmp_png) as img:
                        if cancel and cancel():
                            raise ConversionCancelled("Conversion cancelled")
                        _save_from_pil(img)
            else:
                raise

        if progress:
            progress(1.0)

        return outputFile

    def convertAudio(
        self,
        inputFile: str,
        outputFiletype: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> str:
        self.ensureOutputPath()
        if cancel and cancel():
            raise ConversionCancelled("Conversion cancelled")
        if progress:
            progress(0.02)

        outputFile = self.buildOutputPath(inputFile, outputFiletype)
        ext = outputFiletype.lower().lstrip(".")

        ffmpeg = self._get_ffmpeg_exe()
        duration = self._ffprobe_duration_seconds(ffmpeg, inputFile)

        codec_args: list[str]
        if ext == "mp3":
            codec_args = ["-c:a", "libmp3lame", "-q:a", "2"]
        elif ext == "wav":
            codec_args = ["-c:a", "pcm_s16le"]
        elif ext == "m4a":
            codec_args = ["-c:a", "aac", "-b:a", "192k"]
        else:
            # Generic fallback: let ffmpeg pick reasonable defaults for the container.
            codec_args = []

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
            "-vn",
            "-map",
            "0:a:0",
            *codec_args,
            "-progress",
            "pipe:1",
            "-nostats",
            outputFile,
        ]

        self._run_ffmpeg_with_progress(cmd, duration, progress, cancel=cancel)

        if progress:
            progress(1.0)

        return outputFile

    def convertVideo(
        self,
        inputFile: str,
        outputFiletype: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> str:
        self.ensureOutputPath()
        if cancel and cancel():
            raise ConversionCancelled("Conversion cancelled")
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
            self._run_ffmpeg_with_progress(cmd, duration, progress, cancel=cancel)
        else:
            # Fallback for other formats.
            if cancel and cancel():
                raise ConversionCancelled("Conversion cancelled")
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

    def _run_ffmpeg_with_progress(
        self,
        cmd: list[str],
        duration_s: float | None,
        progress: ProgressCallback | None,
        cancel: CancelCallback | None = None,
    ) -> None:
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
        _register_active_proc(proc)
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
                if cancel and cancel():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise ConversionCancelled("Conversion cancelled")
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
            _unregister_active_proc(proc)
            if log_fp is not None:
                try:
                    log_fp.write(f"EXIT_CODE={proc.poll()}\n")
                    log_fp.flush()
                    log_fp.close()
                except Exception:
                    pass

    def convertFile(
        self,
        inputFile: str,
        outputFiletype: str,
        progress: ProgressCallback | None = None,
        cancel: CancelCallback | None = None,
    ) -> str:
        # filetype.guess() does not reliably detect some text-based formats (e.g. SVG).
        input_path = Path(inputFile)
        suffix = input_path.suffix.lower()
        if suffix == ".svg":
            inputExt = "svg"
            inputMime = "image/svg+xml"
            inputCategory = "image"
        else:
            kind = ft.guess(inputFile)
            if kind is None:
                # Fallback to extension-based mime guess.
                guessed, _enc = mimetypes.guess_type(str(input_path), strict=False)
                inputMime = guessed or ""
                inputExt = suffix.lstrip(".")
                inputCategory = inputMime.split("/", 1)[0] if "/" in inputMime else None
            else:
                inputExt = kind.extension
                inputMime = kind.mime or ""
                inputCategory = inputMime.split("/", 1)[0] if "/" in inputMime else None

        if not inputCategory:
            raise ValueError("Could not detect input file type.")

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
            return self.convertImage(inputFile, outputExt, progress=progress, cancel=cancel)
        if inputCategory == "audio":
            return self.convertAudio(inputFile, outputExt, progress=progress, cancel=cancel)
        if inputCategory == "video":
            return self.convertVideo(inputFile, outputExt, progress=progress, cancel=cancel)

        raise ValueError("Conversion category not implemented.")

def convertFile(
    inputFile: str,
    outputFiletype: str,
    outputPath: str,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
) -> str:
    """Convenience wrapper for one-off conversions."""
    converter = FileConverter(outputPath)
    return converter.convertFile(inputFile, outputFiletype, progress=progress, cancel=cancel)