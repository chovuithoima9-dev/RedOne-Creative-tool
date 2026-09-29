"""Storyboard generator — a native port of the user's Google Flow
"Storyboard Creator" custom tool.

Two-step pipeline (mirrors the Flow tool exactly):
  1. Gemini turns an idea (+ optional reference images) into N English visual
     prompts — one per scene.
  2. Each prompt is generated into an image (Nano Banana Pro / etc.) reusing
     the EXISTING image task pipeline (`_process_image_task`), which already
     does batch concurrency ("số luồng song song"), cooldown/jitter pacing,
     per-item retry and WebSocket streaming.

The same reference images are fed to BOTH steps (Gemini for prompt writing,
and the image generator as visual references for cross-scene consistency).

Step 1 uses the tool's own simple "storyboard artist" system instruction and
RedOne's own Gemini API key (separate quota from the Flow account). Step 2
runs through the Chrome-extension bridge like all other image generation —
so we keep full control of pacing, the real lever against 403/429.
"""
from __future__ import annotations
import json
import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from ..config import OUTPUT_DIR, TaskStatus, GEMINI_IMAGE_PROMPT_CHAIN
from ..database import db
from ..queue_manager import queue
from ..services.gemini import generate_text, parse_prompt_list
from .image import _process_image_task

log = logging.getLogger("redone.storyboard")
router = APIRouter(prefix="/api/storyboard", tags=["storyboard"])


# The Storyboard Creator tool's own (simple) system instruction — kept
# faithful. N is supplied per request in the user prompt, not hard-coded here.
STORYBOARD_SYSTEM_INSTRUCTION = (
    "Bạn là một chuyên gia kịch bản phân cảnh (storyboard artist). Dựa trên ý "
    "tưởng và các hình ảnh tham chiếu của người dùng, hãy tạo ra một chuỗi phân "
    "cảnh chi tiết, mạch lạc theo trình tự. Mỗi phân cảnh cần một mô tả hình ảnh "
    "bằng tiếng Anh (visual prompt) đủ chi tiết để làm prompt cho AI tạo ảnh — "
    "mô tả chủ thể, hành động, bối cảnh, ánh sáng, góc máy, phong cách. Nếu có "
    "ảnh tham chiếu, bám sát chủ thể/bối cảnh/phong cách của chúng để các cảnh "
    "đồng nhất. Trả về DUY NHẤT một mảng JSON gồm các chuỗi prompt tiếng Anh, "
    "không kèm giải thích."
)


def _prep_ref(src: Path) -> Path:
    """Downscale an oversized reference image to ≤1920px JPEG so BOTH steps
    upload fast — the Gemini prompt step (Files API) and the Flow image-gen
    step otherwise stall/time out on raw 17-19MB video frames. Small images
    (≤2MB) are left untouched. Never raises; returns the (possibly new) path."""
    try:
        if src.stat().st_size <= 2_000_000:
            return src
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        w, h = img.size
        scale = min(1.0, 1920 / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        out = src.with_suffix(".jpg")
        img.save(out, format="JPEG", quality=90, optimize=True)
        if out != src:
            try:
                src.unlink()
            except Exception:
                pass
        log.info(f"Storyboard ref shrunk: {src.name} → {out.name} ({out.stat().st_size // 1024}KB)")
        return out
    except Exception as e:
        log.warning(f"Storyboard ref shrink failed for {src.name}: {e}")
        return src


@router.post("/start")
async def start_storyboard(
    idea: str = Form(""),
    count: int = Form(4),
    model: str = Form("nano_banana_pro"),      # nano_banana_pro | nano_banana_2 | imagen_4
    aspect_ratio: str = Form("16:9"),
    concurrent: int = Form(4),                 # "số luồng song song"
    task_name: Optional[str] = Form(None),
    flow_account_email: Optional[str] = Form(None),
    refs: List[UploadFile] = File(default=[]),
):
    """Generate a storyboard: idea (+ optional refs) → N scene prompts → N
    images. Returns {task_id, prompts}; images stream in over WebSocket via
    the reused image pipeline."""
    idea = (idea or "").strip()
    clean_flow_acc = flow_account_email.strip().lower() if flow_account_email else None
    have_refs = any(u and u.filename for u in (refs or []))
    if not idea and not have_refs:
        raise HTTPException(400, "Cần nhập ý tưởng hoặc thêm ảnh tham chiếu")
    count = max(1, int(count or 4))            # no upper cap — user decides
    concurrent = max(1, min(int(concurrent or 4), 8))

    # Persist reference images — the async image task reads these paths LATER,
    # so they must live in a stable dir (not a tmp we delete on return).
    ref_paths: list[str] = []
    ref_images_for_gemini: list[Path] = []
    if have_refs:
        ref_dir = OUTPUT_DIR / "storyboard_refs" / uuid.uuid4().hex
        ref_dir.mkdir(parents=True, exist_ok=True)
        for up in refs:
            if up and up.filename:
                p = ref_dir / up.filename
                with p.open("wb") as f:
                    shutil.copyfileobj(up.file, f)
                # Shrink in a THREAD — PIL on 10×19MB images would otherwise
                # block the event loop ~10s and freeze the whole app ("đơ").
                p = await asyncio.to_thread(_prep_ref, p)
                ref_paths.append(str(p))
                ref_images_for_gemini.append(p)

    # ── Step 1: Gemini → N visual prompts ──
    ref_note = (
        f"Có {len(ref_images_for_gemini)} ảnh tham chiếu đính kèm — bám sát "
        "phong cách/chủ thể/bối cảnh của chúng.\n\n"
        if ref_images_for_gemini else ""
    )
    user_prompt = (
        f"{ref_note}Ý tưởng: {idea or '(chỉ dựa trên ảnh tham chiếu)'}\n\n"
        f"Hãy tạo ĐÚNG {count} phân cảnh. Trả về DUY NHẤT một mảng JSON gồm "
        f"{count} chuỗi prompt tiếng Anh — mỗi chuỗi là 1 visual prompt hoàn "
        f"chỉnh. Không markdown, không giải thích, không key."
    )
    try:
        result = await generate_text(
            user_prompt,
            images=ref_images_for_gemini or None,
            system_instruction=STORYBOARD_SYSTEM_INSTRUCTION,
            model_chain=GEMINI_IMAGE_PROMPT_CHAIN,
            response_mime_type="application/json",
        )
    except Exception as e:
        raise HTTPException(500, f"Tạo kịch bản thất bại: {e}")

    # Robust parse: tolerates fences/prose, drops stray "["/"]" lines, and
    # truncates to `count` (Gemini sometimes over-generates, e.g. 13 for 10).
    prompts = parse_prompt_list(result["text"], count)
    if not prompts:
        raise HTTPException(500, "Model không trả về phân cảnh nào — thử lại.")

    # ── Firebase tracking (best-effort) ──
    try:
        from ..services import tracking as _tracking
        from ..services.oauth_auth import load_session as _load_sess
        _sess = _load_sess()
        _email = (_sess.get("email") or "") if _sess else ""
        if _email:
            await _tracking.track_event(_email, "storyboard_created")
    except Exception:
        pass

    # ── Step 2: hand the prompts to the existing image pipeline ──
    name = (task_name or "").strip() or f"storyboard_{int(time.time())}"
    from ..services import hub_client
    task_id = db.create_task(
        name=name,
        mode="storyboard",           # so Tasks Manager's eye opens the Storyboard
        #                              tab (the runner ignores mode; it reads
        #                              image_model/aspect/concurrent/items).
        image_model=model,
        aspect_ratio=aspect_ratio,
        concurrent=concurrent,
        total_count=len(prompts),
        status=TaskStatus.PENDING.value,
        user_email=hub_client.current_user_email(),
        idea=idea,
        flow_account_email=clean_flow_acc,
    )
    extra_global = {}
    if ref_paths:
        extra_global["reference_images"] = ref_paths
    if idea:
        extra_global["idea"] = idea
    extra_global = extra_global or None
    for p in prompts:
        db.add_task_item(task_id, p, extra=extra_global)

    position = await queue.enqueue("image", task_id, _process_image_task, flow_account_email=clean_flow_acc)
    log.info(
        f"Storyboard task {task_id}: {len(prompts)} scenes, model={model}, "
        f"concurrent={concurrent}, refs={len(ref_paths)} (model_used={result['model_used']})"
    )
    return {
        "task_id": task_id,
        "name": name,
        "prompts": prompts,
        "idea": idea,
        "model_used": result["model_used"],
        "fallback_log": result["fallback_log"],
        "queue_position": position,
        "queued": position > 0,
        "flow_account_email": clean_flow_acc,
    }
