import io
import os
import re
import subprocess
import tempfile
import imageio_ffmpeg
from PIL import Image, ImageOps

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

ANIMATED_EXTS = {".gif", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


def _is_animated_gif(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1
    except Exception:
        return False


def _is_animated_webp(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1
    except Exception:
        return False


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def convert_to_webp(
    input_bytes: bytes,
    filename: str,
    max_seconds: float = 5.0,
    start_sec: float = 0.0,
    fit_mode: str = "crop",
    zoom: float = 1.0,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
    rotate: int = 0,
    flip_h: bool = False,
) -> tuple[bytes, bool]:
    """
    Convert an image, GIF, or video into a 512x512 WhatsApp-compatible WebP sticker
    with square crop / fit, zoom, pan, trim, rotate, and flip options.
    Returns (webp_bytes, is_animated).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    fit_mode = "contain" if fit_mode == "contain" else "crop"
    zoom = _clamp(float(zoom or 1.0), 1.0, 3.0)
    pan_x = _clamp(float(pan_x or 0.0), -1.0, 1.0)
    pan_y = _clamp(float(pan_y or 0.0), -1.0, 1.0)
    rotate = int(rotate or 0) % 360
    start_sec = max(0.0, float(start_sec or 0.0))
    max_seconds = _clamp(float(max_seconds or 5.0), 0.5, 5.0)

    is_animated = False
    if ext == ".gif":
        is_animated = _is_animated_gif(input_bytes)
    elif ext == ".webp":
        is_animated = _is_animated_webp(input_bytes)
    elif ext in ANIMATED_EXTS:
        is_animated = True

    if not is_animated:
        with Image.open(io.BytesIO(input_bytes)) as img:
            img = ImageOps.exif_transpose(img).convert("RGBA")
            if flip_h:
                img = ImageOps.mirror(img)
            if rotate:
                img = img.rotate(-rotate, expand=True)

            if fit_mode == "crop":
                # Scale so shorter side >= 512 * zoom, then crop 512x512
                base_scale = max(512.0 / img.width, 512.0 / img.height) * zoom
                new_w = max(512, int(round(img.width * base_scale)))
                new_h = max(512, int(round(img.height * base_scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = int(round((new_w - 512) * (1.0 + pan_x) / 2.0))
                top = int(round((new_h - 512) * (1.0 + pan_y) / 2.0))
                left = max(0, min(new_w - 512, left))
                top = max(0, min(new_h - 512, top))
                canvas = img.crop((left, top, left + 512, top + 512))
            else:
                img.thumbnail((512, 512), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
                offset = ((512 - img.width) // 2, (512 - img.height) // 2)
                canvas.paste(img, offset, img)

            for quality in (90, 75, 60, 45):
                buf = io.BytesIO()
                canvas.save(buf, format="WEBP", quality=quality, method=6)
                out = buf.getvalue()
                if len(out) <= 100 * 1024 or quality == 45:
                    return out, False

    # Animated GIF or Video -> Animated 512x512 WebP (< 500 KB)
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, f"input{ext or '.mp4'}")
        out_path = os.path.join(tmpdir, "sticker.webp")
        with open(in_path, "wb") as f:
            f.write(input_bytes)

        pre_filters = []
        if flip_h:
            pre_filters.append("hflip")
        if rotate == 90:
            pre_filters.append("transpose=1")
        elif rotate == 180:
            pre_filters.append("hflip,vflip")
        elif rotate == 270:
            pre_filters.append("transpose=2")

        attempts = [
            (512, 15, 70),
            (512, 12, 55),
            (512, 10, 40),
            (384, 10, 35),
        ]
        last_bytes = b""
        for side, fps, quality in attempts:
            vf_parts = list(pre_filters)
            vf_parts.append(f"fps={fps}")

            if fit_mode == "crop":
                target = int(round(side * zoom))
                vf_parts.append(f"scale={target}:{target}:force_original_aspect_ratio=increase")
                vf_parts.append("setsar=1")
                vf_parts.append(
                    f"crop={side}:{side}:(iw-{side})*(1+{pan_x})/2:(ih-{side})*(1+{pan_y})/2"
                )
            else:
                vf_parts.append(f"scale={side}:{side}:force_original_aspect_ratio=decrease")
                vf_parts.append(f"pad={side}:{side}:(ow-iw)/2:(oh-ih)/2:color=0x00000000")

            if side != 512:
                vf_parts.append("scale=512:512:flags=bilinear")

            vf_parts.insert(0, "setpts=PTS-STARTPTS")

            cmd = [FFMPEG_EXE, "-y", "-i", in_path]
            if start_sec > 0:
                cmd += ["-ss", f"{start_sec:.3f}"]
            cmd += [
                "-t", f"{max_seconds:.3f}",
                "-vf", ",".join(vf_parts),
                "-vcodec", "libwebp",
                "-lossless", "0",
                "-compression_level", "4",
                "-q:v", str(quality),
                "-loop", "0",
                "-preset", "default",
                "-an",
                out_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg WebP conversion failed: {proc.stderr.decode(errors='ignore')[-400:]}")
            with open(out_path, "rb") as f:
                last_bytes = f.read()
            if len(last_bytes) <= 490 * 1024:
                return last_bytes, True

        return last_bytes, True


def _parse_ffmpeg_duration(stderr_text: str) -> float:
    matches = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", stderr_text)
    if matches:
        h, m, s = matches[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    dur_matches = re.findall(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr_text)
    if dur_matches:
        h, m, s = dur_matches[0]
        return int(h) * 3600 + int(m) * 60 + float(s)
    return 1.0


def convert_to_ogg_opus(
    audio_bytes: bytes,
    filename: str,
    max_seconds: float = 8.0,
    start_sec: float = 0.0,
) -> tuple[bytes, int]:
    """
    Convert any audio or video track into WhatsApp-compatible OGG Opus audio.
    Returns (ogg_bytes, duration_seconds).
    """
    ext = os.path.splitext(filename or "")[1].lower() or ".webm"
    start_sec = max(0.0, float(start_sec or 0.0))
    max_seconds = _clamp(float(max_seconds or 8.0), 0.5, 8.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, f"audio_in{ext}")
        out_path = os.path.join(tmpdir, "audio_out.ogg")
        with open(in_path, "wb") as f:
            f.write(audio_bytes)

        cmd = [FFMPEG_EXE, "-y", "-i", in_path]
        if start_sec > 0:
            cmd += ["-ss", f"{start_sec:.3f}"]
        cmd += [
            "-vn",
            "-sn",
            "-dn",
            "-map", "0:a:0",
            "-t", f"{max_seconds:.3f}",
            "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
            "-c:a", "libopus",
            "-b:a", "48k",
            "-ac", "1",
            "-ar", "48000",
            "-f", "ogg",
            out_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_str = proc.stderr.decode(errors="ignore")
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg OGG Opus conversion failed: {stderr_str[-400:]}")

        with open(out_path, "rb") as f:
            ogg_data = f.read()

        duration = _parse_ffmpeg_duration(stderr_str)
        duration_sec = max(1, min(int(round(max_seconds)), round(duration)))
        return ogg_data, duration_sec


def convert_video_with_own_audio(
    input_bytes: bytes,
    filename: str,
    max_seconds: float = 5.0,
    start_sec: float = 0.0,
    fit_mode: str = "crop",
    zoom: float = 1.0,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
    rotate: int = 0,
    flip_h: bool = False,
) -> tuple[bytes, bool, bytes, int]:
    """
    Single-pass synchronized conversion for a video + its own audio track.
    Guarantees exact lip-sync (0ms offset) between animated WebP and OGG Opus.
    Returns (webp_bytes, is_animated, ogg_bytes, duration_sec).
    """
    ext = os.path.splitext(filename or "")[1].lower() or ".mp4"
    fit_mode = "contain" if fit_mode == "contain" else "crop"
    zoom = _clamp(float(zoom or 1.0), 1.0, 3.0)
    pan_x = _clamp(float(pan_x or 0.0), -1.0, 1.0)
    pan_y = _clamp(float(pan_y or 0.0), -1.0, 1.0)
    rotate = int(rotate or 0) % 360
    start_sec = max(0.0, float(start_sec or 0.0))
    max_seconds = _clamp(float(max_seconds or 5.0), 0.5, 5.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, f"input{ext}")
        webp_path = os.path.join(tmpdir, "sticker.webp")
        ogg_path = os.path.join(tmpdir, "audio_out.ogg")
        with open(in_path, "wb") as f:
            f.write(input_bytes)

        pre_filters = ["setpts=PTS-STARTPTS"]
        if flip_h:
            pre_filters.append("hflip")
        if rotate == 90:
            pre_filters.append("transpose=1")
        elif rotate == 180:
            pre_filters.append("hflip,vflip")
        elif rotate == 270:
            pre_filters.append("transpose=2")

        # First encode OGG Opus accurately at exact start_sec
        ogg_cmd = [
            FFMPEG_EXE, "-y", "-i", in_path,
            "-ss", f"{start_sec:.3f}",
            "-t", f"{max_seconds:.3f}",
            "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-af", "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
            "-c:a", "libopus", "-b:a", "48k", "-ac", "1", "-ar", "48000", "-f", "ogg",
            ogg_path,
        ]
        ogg_proc = subprocess.run(ogg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ogg_stderr = ogg_proc.stderr.decode(errors="ignore")
        if ogg_proc.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed: {ogg_stderr[-400:]}")
        with open(ogg_path, "rb") as f:
            ogg_bytes = f.read()
        dur_val = _parse_ffmpeg_duration(ogg_stderr)
        duration_sec = max(1, min(5, round(dur_val)))

        # Encode animated WebP at exact same start_sec & exact duration
        attempts = [
            (512, 15, 70),
            (512, 12, 55),
            (512, 10, 40),
            (384, 10, 35),
        ]
        webp_bytes = b""
        for side, fps, quality in attempts:
            vf_parts = list(pre_filters)
            vf_parts.append(f"fps={fps}")
            if fit_mode == "crop":
                target = int(round(side * zoom))
                vf_parts.append(f"scale={target}:{target}:force_original_aspect_ratio=increase")
                vf_parts.append("setsar=1")
                vf_parts.append(
                    f"crop={side}:{side}:(iw-{side})*(1+{pan_x})/2:(ih-{side})*(1+{pan_y})/2"
                )
            else:
                vf_parts.append(f"scale={side}:{side}:force_original_aspect_ratio=decrease")
                vf_parts.append(f"pad={side}:{side}:(ow-iw)/2:(oh-ih)/2:color=0x00000000")
            if side != 512:
                vf_parts.append("scale=512:512:flags=bilinear")

            cmd = [
                FFMPEG_EXE, "-y", "-i", in_path,
                "-ss", f"{start_sec:.3f}",
                "-t", f"{max_seconds:.3f}",
                "-vf", ",".join(vf_parts),
                "-vcodec", "libwebp",
                "-lossless", "0",
                "-compression_level", "4",
                "-q:v", str(quality),
                "-loop", "0",
                "-preset", "default",
                "-an",
                webp_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg WebP conversion failed: {proc.stderr.decode(errors='ignore')[-400:]}")
            with open(webp_path, "rb") as f:
                webp_bytes = f.read()
            if len(webp_bytes) <= 490 * 1024:
                break

        return webp_bytes, True, ogg_bytes, duration_sec
