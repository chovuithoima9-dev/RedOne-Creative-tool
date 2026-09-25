"""Video upscaler service using Real-ESRGAN NCNN-Vulkan.

Workflow:
  1. ffmpeg: extract frames from video → temp dir
  2. realesrgan-ncnn-vulkan: upscale each frame (GPU-accelerated)
  3. ffmpeg: merge upscaled frames + original audio → output video

The NCNN-Vulkan binary is downloaded via the feature store into
addons/video-upscale/. It supports Intel/AMD/NVIDIA GPUs via Vulkan.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from ..config import USER_DATA_ROOT, EXT_DIR
from .ffmpeg_utils import subprocess_no_window_kwargs

log = logging.getLogger("redone.upscaler")

# Type for progress callback: (percent, stage, message)
ProgressCb = Callable[[float, str, str], None]

# Models bundled with NCNN release (in models/ dir inside the zip)
# - realesrgan-x4plus           : best for general real-world content (photos, video frames)
# - realesr-animevideov3        : optimized for anime video (x2/x3/x4)
# - realesrgan-x4plus-anime     : anime images only
DEFAULT_MODEL = "realesrgan-x4plus"
DEFAULT_SCALE = 2
DEFAULT_DENOISE = -1  # -1 = use model default (only realesr-general-x4v3 supports -d)


def _find_ncnn_exe() -> Path | None:
    """Locate realesrgan-ncnn-vulkan executable in addons."""
    candidates = [
        EXT_DIR / "video-upscale" / "realesrgan-ncnn-vulkan.exe",
        EXT_DIR / "video-upscale" / "realesrgan-ncnn-vulkan",
        # The upstream zip contains a nested "video-upscale/" directory
        EXT_DIR / "video-upscale" / "video-upscale" / "realesrgan-ncnn-vulkan.exe",
        EXT_DIR / "video-upscale" / "video-upscale" / "realesrgan-ncnn-vulkan",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_ffmpeg() -> str | None:
    """Locate ffmpeg — reuse the app-wide finder (PATH + imageio_ffmpeg)."""
    from .ffmpeg_utils import find_ffmpeg
    return find_ffmpeg()


def _find_ffprobe() -> str | None:
    """Locate ffprobe — reuse the app-wide finder."""
    from .ffmpeg_utils import find_ffprobe
    return find_ffprobe()


def is_upscaler_available() -> bool:
    """Check if either NCNN EXE or Topaz engine is available."""
    ncnn_ready = _find_ncnn_exe() is not None and _find_ffmpeg() is not None
    try:
        from .topaz_upscaler import is_topaz_available
        return ncnn_ready or is_topaz_available()
    except Exception:
        return ncnn_ready


async def get_video_info(video_path: str) -> dict:
    """Get video info (duration, fps, resolution) via ffprobe, falls back to ffmpeg."""
    ffprobe = _find_ffprobe()
    if ffprobe:
        try:
            from .ffmpeg_utils import subprocess_no_window_kwargs
            proc = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
                "-show_entries", "format=duration",
                "-of", "json",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_no_window_kwargs(),
            )
            stdout, _ = await proc.communicate()
            import json
            data = json.loads(stdout.decode())
            stream = (data.get("streams") or [{}])[0]
            fmt = data.get("format") or {}
            # Parse frame rate like "30/1"
            fps_str = stream.get("r_frame_rate", "30/1")
            parts = fps_str.split("/")
            fps = float(parts[0]) / float(parts[1]) if len(parts) == 2 else 30.0
            
            duration = 0.0
            try:
                duration = float(fmt.get("duration", 0))
            except (TypeError, ValueError):
                pass

            nb_frames = 0
            try:
                nb_frames = int(stream.get("nb_frames"))
            except (TypeError, ValueError):
                if duration > 0:
                    nb_frames = int(duration * fps)

            return {
                "width": int(stream.get("width", 0)),
                "height": int(stream.get("height", 0)),
                "fps": round(fps, 2),
                "duration": duration,
                "nb_frames": nb_frames,
            }
        except Exception as e:
            log.warning("ffprobe failed: %s", e)

    # Fallback to ffmpeg -i parsing
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return {}
    try:
        from .ffmpeg_utils import subprocess_no_window_kwargs
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-i", str(video_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_no_window_kwargs(),
        )
        _, stderr_data = await proc.communicate()
        import re
        text = (stderr_data or b"").decode("utf-8", errors="replace")
        dim_m = re.search(r"(\d{3,5})x(\d{3,5})", text)
        fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
        dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        
        width = int(dim_m.group(1)) if dim_m else 0
        height = int(dim_m.group(2)) if dim_m else 0
        fps = float(fps_m.group(1)) if fps_m else 25.0
        
        duration = 0.0
        if dur_m:
            h, m, s = float(dur_m.group(1)), float(dur_m.group(2)), float(dur_m.group(3))
            duration = h * 3600 + m * 60 + s
            
        nb_frames = int(duration * fps) if duration > 0 else 0
        
        return {
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "duration": duration,
            "nb_frames": nb_frames,
        }
    except Exception as e:
        log.warning("ffmpeg probe fallback failed: %s", e)
        return {}


async def upscale_video(
    input_path: str,
    output_path: Optional[str] = None,
    resolution: str = "FHD",  # "FHD", "2K", "4K"
    model: str = DEFAULT_MODEL,
    denoise: float = DEFAULT_DENOISE,
    progress: Optional[ProgressCb] = None,
    task_id: Optional[str] = None,
) -> str:
    """Upscale a video file using Real-ESRGAN NCNN-Vulkan or Topaz Proteus.

    Args:
        input_path: Path to input video
        output_path: Output path (auto-generated if None)
        resolution: Output quality ("FHD", "2K", or "4K")
        model: Model name
        denoise: Denoise strength 0~1 (-1 = model default)
        progress: Optional callback (percent, stage, message)
        task_id: Batch or task ID for cancellation tracking

    Returns:
        Path to the upscaled video file.
    """
    vp = Path(input_path)
    if not vp.exists():
        raise FileNotFoundError(f"Video không tồn tại: {input_path}")

    # ── Handle Topaz Proteus Model ──
    if model == "topaz-proteus":
        from .topaz_upscaler import upscale_with_proteus
        return await upscale_with_proteus(
            input_path=input_path,
            output_path=output_path,
            resolution=resolution,
            denoise=denoise,
            task_id=task_id,
            progress=progress,
        )

    ncnn_exe = _find_ncnn_exe()
    if not ncnn_exe:
        raise FileNotFoundError(
            "realesrgan-ncnn-vulkan chưa được cài. "
            "Vui lòng cài tính năng 'Video Upscale' từ Kho tính năng."
        )

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg không tìm thấy trên PATH")

    # Determine resolution value
    if resolution not in ("FHD", "2K", "4K"):
        resolution = "FHD"

    # Determine model scale factor for NCNN Vulkan
    if model == "realesr-animevideov3":
        if resolution == "2K":
            model_native_scale = 3
        elif resolution == "4K":
            model_native_scale = 4
        else:
            model_native_scale = 2
    else:
        model_native_scale = 4

    # Verify model files exist in addons/video-upscale/models/
    models_dir = ncnn_exe.parent / "models"
    if model == "realesr-animevideov3":
        param_file = models_dir / f"realesr-animevideov3-x{model_native_scale}.param"
        bin_file = models_dir / f"realesr-animevideov3-x{model_native_scale}.bin"
    else:
        param_file = models_dir / f"{model}.param"
        bin_file = models_dir / f"{model}.bin"

    if not param_file.exists() or not bin_file.exists():
        raise FileNotFoundError(
            f"Thiếu file model '{model}' (.bin/.param) trong thư mục: {models_dir.name}/. "
            f"Vui lòng chọn model realesrgan-x4plus (cho đời thực) hoặc realesr-animevideov3 (cho anime) để chạy."
        )

    # Get video info for progress tracking
    info = await get_video_info(input_path)
    total_frames = info.get("nb_frames", 0)
    fps = info.get("fps", 30.0)
    original_w = info.get("width", 0)
    original_h = info.get("height", 0)

    if output_path is None:
        from ..config import OUTPUT_DIR
        out_dir = OUTPUT_DIR / "video" / "upscaled"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = vp.stem
        output_path = str(out_dir / f"{stem}_upscaled_{resolution}{vp.suffix}")

    _emit = progress or (lambda *_: None)

    log.info(
        "Upscale: %s → %s (model=%s, %dx%d, ~%d frames)",
        vp.name, resolution, model, original_w, original_h, total_frames,
    )

    # Create temp directories
    tmp_base = Path(tempfile.mkdtemp(prefix="redone_upscale_"))
    frames_dir = tmp_base / "frames"
    upscaled_dir = tmp_base / "upscaled"
    frames_dir.mkdir()
    upscaled_dir.mkdir()

    try:
        # ── Step 1: Extract frames ──────────────────────────────────
        _emit(5, "extracting", "Đang trích xuất frames từ video...")
        extract_cmd = [
            ffmpeg, "-y",
            "-i", str(vp),
            "-qscale:v", "2",  # high quality JPEG frames
            str(frames_dir / "frame_%08d.jpg"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *extract_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_no_window_kwargs(),
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg extract frames thất bại: "
                # TAIL, not head: ffmpeg prints its build banner first and the
                # actual reason last, so [:500] only ever showed the banner and
                # made these failures impossible to diagnose from the log.
                f"{stderr.decode(errors='replace').strip()[-1500:]}"
            )

        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        actual_frames = len(frame_files)
        if actual_frames == 0:
            raise RuntimeError("Không trích xuất được frame nào từ video")

        log.info("Extracted %d frames", actual_frames)
        _emit(15, "upscaling", f"Đã trích xuất {actual_frames} frames. Bắt đầu upscale...")

        # ── Step 2: NCNN upscale ────────────────────────────────────
        ncnn_cmd = [
            str(ncnn_exe),
            "-i", str(frames_dir),
            "-o", str(upscaled_dir),
            "-n", model,
            "-s", str(model_native_scale),
            "-f", "jpg",
        ]
        # Note: The compiled realesrgan-ncnn-vulkan executable does not support -d flag, so we skip it.

        proc = await asyncio.create_subprocess_exec(
            *ncnn_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_no_window_kwargs(),
        )

        # Poll for progress by counting output files while NCNN runs
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
                break  # process finished
            except asyncio.TimeoutError:
                pass  # still running, poll progress
            try:
                done_files = len(list(upscaled_dir.glob("frame_*.jpg")))
                pct = 15 + (done_files / actual_frames * 65) if actual_frames else 15
                _emit(
                    min(pct, 80), "upscaling",
                    f"Upscaling... {done_files}/{actual_frames} frames",
                )
            except Exception:
                pass

        # Process finished — collect stderr for error reporting
        stderr_data = b""
        if proc.stderr:
            try:
                stderr_data = await proc.stderr.read()
            except Exception:
                pass

        if proc.returncode != 0:
            err_text = stderr_data.decode(errors="replace")[:500] if stderr_data else "unknown error"
            raise RuntimeError(f"realesrgan-ncnn-vulkan thất bại: {err_text}")

        upscaled_files = sorted(upscaled_dir.glob("frame_*.jpg"))
        if len(upscaled_files) == 0:
            raise RuntimeError("NCNN không tạo được frame upscaled nào")

        log.info("Upscaled %d frames", len(upscaled_files))
        _emit(82, "merging", "Đang ghép frames thành video...")

        # ── Step 3: Merge frames + audio → output ───────────────────
        if original_w >= original_h:
            # Landscape
            if resolution == "4K":
                target_h = 2160
            elif resolution == "2K":
                target_h = 1440
            else:
                target_h = 1080
            target_w = int(round(original_w * (target_h / original_h) / 2) * 2) if original_h > 0 else original_w
        else:
            # Portrait
            if resolution == "4K":
                target_w = 2160
            elif resolution == "2K":
                target_w = 1440
            else:
                target_w = 1080
            target_h = int(round(original_h * (target_w / original_w) / 2) * 2) if original_w > 0 else original_h

        ncnn_output_w = original_w * model_native_scale
        ncnn_output_h = original_h * model_native_scale

        merge_cmd = [
            ffmpeg, "-y",
            "-framerate", str(fps),
            "-i", str(upscaled_dir / "frame_%08d.jpg"),
            "-i", str(vp),       # original video for audio
            "-map", "0:v:0",     # video from upscaled frames
            "-map", "1:a?",      # audio from original (if exists)
        ]
        
        if (ncnn_output_w != target_w or ncnn_output_h != target_h) and original_w > 0 and original_h > 0:
            merge_cmd.extend(["-vf", f"scale={target_w}:{target_h}"])

        merge_cmd.extend([
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",        # high quality
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output_path),
        ])
        proc = await asyncio.create_subprocess_exec(
            *merge_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_no_window_kwargs(),
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg merge thất bại: "
                # TAIL, not head: ffmpeg prints its build banner first and the
                # actual reason last, so [:500] only ever showed the banner and
                # made these failures impossible to diagnose from the log.
                f"{stderr.decode(errors='replace').strip()[-1500:]}"
            )

        out_p = Path(output_path)
        if not out_p.exists():
            raise RuntimeError("Output video không được tạo")

        out_size = out_p.stat().st_size // 1024 // 1024
        _emit(100, "done", f"Hoàn tất! {out_size}MB — {target_w}x{target_h}")
        log.info(
            "Upscale done: %s → %s (%dMB, %dx%d)",
            vp.name, out_p.name, out_size,
            target_w, target_h,
        )
        return str(output_path)

    finally:
        # Cleanup temp files
        try:
            shutil.rmtree(tmp_base, ignore_errors=True)
        except Exception:
            pass


async def upscale_image_file(
    input_path: str | Path,
    output_path: str | Path,
    scale: int = 4,
    model: str = "realesrgan-x4plus",
) -> Path:
    """Upscale a single image file to 2x (2K) or 4x (4K) using Real-ESRGAN NCNN Vulkan.

    GPU-accelerated via Vulkan (~1.8s on RTX 4060 Ti). Falls back to PIL Lanczos
    if NCNN executable or Vulkan is not available.
    """
    inp = Path(input_path).resolve()
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if not inp.exists():
        raise FileNotFoundError(f"Input image not found: {inp}")

    exe = _find_ncnn_exe()
    if exe and exe.exists():
        models_dir = exe.parent / "models"
        s_val = str(max(2, min(4, scale)))
        cmd = [
            str(exe),
            "-i", str(inp),
            "-o", str(out),
            "-s", s_val,
            "-m", str(models_dir),
            "-n", model,
        ]
        log.info(f"[upscaler] Running NCNN Vulkan image upscale: {inp.name} -> {out.name} (scale={s_val})")
        proc = await asyncio.create_subprocess_exec(*cmd, **subprocess_no_window_kwargs())
        rc = await proc.wait()
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            log.info(f"[upscaler] NCNN Vulkan image upscale succeeded: {out.stat().st_size} bytes")
            return out
        else:
            log.warning(f"[upscaler] NCNN Vulkan failed (rc={rc}), falling back to PIL Lanczos...")

    # PIL Lanczos high-fidelity fallback
    from PIL import Image
    with Image.open(inp) as im:
        w, h = im.size
        target_w = w * scale
        target_h = h * scale
        res = im.resize((target_w, target_h), Image.LANCZOS)
        fmt = "JPEG" if out.suffix.lower() in (".jpg", ".jpeg") else "PNG"
        if fmt == "JPEG" and res.mode in ("RGBA", "P"):
            res = res.convert("RGB")
        res.save(out, fmt, quality=95)
    return out
