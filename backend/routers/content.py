"""Content (Text/Image-to-Video) generation endpoints."""
from __future__ import annotations
import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from ..database import db
from ..config import (
    OUTPUT_DIR, TaskStatus, ItemStatus, ASPECT_RATIOS, RESOLUTIONS,
    video_model_for, interpolation_model_for, get_save_dir, clamp_duration, project_item_stem,
)
from ..ws_hub import hub
from ..queue_manager import queue

log = logging.getLogger("redone.content")
router = APIRouter(prefix="/api/content", tags=["content"])


class StartTaskRequest(BaseModel):
    mode: str  # "t2v" | "i2v"
    prompts: list[str]
    quality: str = "fast"            # omni_flash | lite | fast | quality | lite_lp
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    duration: int = 8                # 4 | 6 | 8 | 10 (10 only for omni_flash)
    concurrent: int = 1
    videos_per_prompt: int = 1       # 1..4 — số video tạo cho mỗi prompt (i2v: cùng 1 ảnh → N video)
    reference_images: Optional[list[str]] = None  # uploaded image paths
    character_images: Optional[dict] = None
    loop: bool = False               # I2V "Loop video": reuse the image as first+last frame
    task_name: Optional[str] = None


_active_tasks: dict[int, asyncio.Task] = {}


def _pick_account() -> Optional[dict]:
    """Pick the active Google Flow account currently opened in the Chrome tab first,
    otherwise fallback to the account with the highest credit in the database."""
    from ..services.browser_bridge import bridge
    active_email = bridge.get_active_account_email()
    if active_email:
        acc = db.get_account_by_email(active_email)
        if acc:
            if not acc.get("enabled"):
                try:
                    db.update_account(acc["id"], enabled=1)
                    acc["enabled"] = 1
                except Exception:
                    pass
            return acc
        else:
            try:
                acc_id = db.add_account(active_email)
                new_acc = db.get_account(acc_id)
                if new_acc:
                    return new_acc
            except Exception:
                pass

    accounts = [a for a in db.get_accounts() if a.get("enabled")]
    if accounts:
        def _tier_score(tier_str: Optional[str]) -> int:
            t = (tier_str or "").upper()
            if "ULTRA" in t:
                return 30
            if "PRO" in t:
                return 20
            return 10

        accounts.sort(key=lambda a: (_tier_score(a.get("tier")), a.get("credit") or 0), reverse=True)
        return accounts[0]
    # Vertex AI uses the baked service account — no Google login needed. On a
    # fresh machine with no accounts, return a synthetic one so gen proceeds.
    # Other modes (extension/playwright) still require a real account.
    from ..services.flow_factory import is_vertex_mode, synthetic_vertex_account
    if is_vertex_mode():
        return synthetic_vertex_account()
    return None


async def _handle_session_dead(task_id, acc, sde):
    """Disable a dead account, mark task as error, broadcast UI notification."""
    log.warning(f"Session dead for account {acc['email']}: {sde.reason}")
    try:
        db.update_account(acc["id"], enabled=0)
    except Exception:
        pass
    db.update_task(task_id, status=TaskStatus.ERROR.value)
    await hub.broadcast("account_session_dead", {
        "account_id": acc["id"],
        "email": acc["email"],
        "reason": sde.reason,
    })
    await hub.broadcast("task_error", {
        "task_id": task_id,
        "error": f"Session account {acc['email']} đã hết hạn — hãy login lại trong tab Tài Khoản",
    })


# Transient errors from Google's poll response that justify an item-level retry
TRANSIENT_PATTERNS = (
    "failed to enqueue",
    "try again",
    "transient",
    "internal error",
    "media not found",
    "unavailable",
)
MAX_ITEM_RETRIES = 3

# Ceiling for ONE item (gen + poll + download + xóa watermark). Nothing
# legitimate comes near this — the slowest real video item is a few minutes.
ITEM_HARD_TIMEOUT = 1800.0


async def run_item_bounded(item: dict, coro, timeout: float = ITEM_HARD_TIMEOUT):
    """Await one item's generator with a hard ceiling.

    A coroutine still running after `timeout` is wedged, not slow. Without a
    bound the batch's gather() never returns and the whole task freezes with
    no error and no log — that was the 2026-08-03 storyboard hang, where
    renew_token waited on a lock it already held.

    Cancelling alone isn't enough: asyncio cancels via BaseException, so the
    generator's own `except Exception` handler never runs and the item would
    sit at GENERATING forever (stuck spinner, invisible to "Gen lại lỗi").
    So flip it to ERROR here.
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        msg = (f"Quá {int(timeout / 60)} phút không phản hồi — đã hủy mục này. "
               "Bấm Gen lại để chạy lại.")
        log.warning("Item %s treo quá %.0fs — hủy để lô chạy tiếp", item.get("id"), timeout)
        db.update_item(item["id"], status=ItemStatus.ERROR.value, error_message=msg)
        await hub.broadcast("item_error", {
            "task_id": item.get("task_id"), "item_id": item["id"], "error": msg,
        })
        return False


def _read_cooldown_range() -> tuple[float, float]:
    """Read (min, max) batch cooldown seconds from settings.

    Defaults to (5, 10) — randomized pauses in that range mimic natural
    user pacing and avoid the constant-gap signature that flagged the old
    fixed-cooldown approach.

    Returns a tuple clamped so:
      - both values are non-negative
      - max >= min (silently swaps if user entered them backwards)
    """
    def _to_float(key: str, default: float) -> float:
        try:
            v = db.get_setting(key, default)
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return default

    cd_min = _to_float("batch_cooldown_min_seconds", 5.0)
    cd_max = _to_float("batch_cooldown_max_seconds", 10.0)
    if cd_max < cd_min:
        cd_min, cd_max = cd_max, cd_min
    return cd_min, cd_max


# ── DB-authoritative progress helpers ───────────────────────────────
# Counts come straight from the task_items table rather than an in-memory
# counter dict. This matters because items can now be regenerated OUT of the
# main batch loop (per-item retry / retry-all endpoints) while the loop may
# still be running other items. A shared in-memory counter would race; the DB
# is the single source of truth so every writer converges to the same numbers.

def _count_items(task_id: int) -> dict:
    items = db.get_task_items(task_id)
    done = sum(1 for it in items if it.get("status") == ItemStatus.COMPLETED.value)
    err = sum(1 for it in items if it.get("status") == ItemStatus.ERROR.value)
    gen = sum(1 for it in items if it.get("status") == ItemStatus.GENERATING.value)
    pend = sum(1 for it in items if it.get("status") == ItemStatus.PENDING.value)
    return {"done": done, "error": err, "generating": gen, "pending": pend, "total": len(items)}


async def _broadcast_progress(task_id: int) -> dict:
    """Recompute counts from DB, persist to the task row, broadcast progress."""
    c = _count_items(task_id)
    db.update_task(task_id, done_count=c["done"], error_count=c["error"])
    await hub.broadcast("task_progress", {
        "task_id": task_id, "done": c["done"], "error": c["error"], "total": c["total"],
    })
    return c


async def _maybe_finalize(task_id: int) -> None:
    """Mark the task COMPLETED + broadcast — but ONLY when no item is still
    pending/generating. Safe to call from a retry path while the main batch
    loop is still working: the guard means whoever finishes the last item
    flips the task, and the COMPLETED-status check prevents a double event.
    """
    c = _count_items(task_id)
    if c["generating"] == 0 and c["pending"] == 0:
        task = db.get_task(task_id)
        if task and task.get("status") != TaskStatus.COMPLETED.value:
            db.update_task(task_id, status=TaskStatus.COMPLETED.value,
                           finished_at=str(time.time()))
            await hub.broadcast("task_completed", {
                "task_id": task_id, "done": c["done"], "error": c["error"],
            })


async def generate_content_item(client, task: dict, item: dict) -> bool:
    """Generate + wait + download ONE video item.

    Updates the DB row, broadcasts item_status / item_completed / item_error
    and task_progress. Returns True on success, False on a handled failure.
    Re-raises SessionDeadError so the caller can disable the dead account.

    This is the single reusable unit of work shared by the batch loop
    (`_process_task`) AND the per-item / retry-failed endpoints — that's what
    lets a retry run even while the parent task is still generating others.
    Error messages are passed through `friendly_error()` so the gallery shows
    something actionable; the raw string rides along as `error_detail`.
    """
    import random as _rng
    from ..services import flow_client as fc_mod
    from ..services import hub_client
    from ..config import cost_for_model_key
    from ..services.error_messages import friendly_error

    task_id = task["id"]
    item_id = item["id"]
    quality = task["quality"]
    aspect = task["aspect_ratio"]

    # Mark GENERATING in the DB *before* the jitter sleep. A regen-from-done
    # flips this item COMPLETED→GENERATING; writing the status first (rather
    # than after sleeping) closes the window where a sibling item finishing
    # could trip _maybe_finalize into broadcasting task_completed prematurely.
    db.update_item(item_id, status=ItemStatus.GENERATING.value, error_message=None)
    await hub.broadcast("item_status", {
        "task_id": task_id, "item_id": item_id,
        "status": ItemStatus.GENERATING.value,
    })
    # Small jitter so concurrent items don't hit Google at the exact same ms.
    await asyncio.sleep(_rng.uniform(0.0, 1.5))
    _rid = None
    _cost = 0
    _reserved = False
    try:
        ref_image_path = None
        loop = False
        if item.get("extra_json"):
            extra = json.loads(item["extra_json"])
            ref_image_path = extra.get("reference_image")
            loop = bool(extra.get("loop"))
        ref_media = None
        if ref_image_path and Path(ref_image_path).exists():
            ref_media = await client.upload_image(ref_image_path)

        # "Loop video" only applies with a reference image — it reuses that
        # image as BOTH first and last frame (interpolation) so the clip ends
        # where it started. No ref image → nothing to loop.
        loop = loop and bool(ref_media)
        mode = "i2v" if ref_media else "t2v"
        eff_quality = quality
        # Omni Flash now HAS a native I2V model (abra_i2v_<N>s, confirmed via
        # labs.google HAR 2026-07-20), so the old fall-back to Veo lite_lp is
        # gone. It still has no interpolation model though — `loop` below
        # routes through interpolation_model_for(), which falls back on its own.
        duration_s = int(task.get("duration") or 8)
        if loop:
            # Interpolation model (first+last frame). Omni Flash has none → lite_lp.
            model_key = interpolation_model_for(eff_quality)
        else:
            model_key = video_model_for(eff_quality, mode, duration_s)

        # Hub internal-credit reserve (cost by model; no-op when Hub off/unreachable).
        # Only a genuine out-of-credit (Hub reachable) blocks the gen.
        _cost = cost_for_model_key(model_key)
        _res = await hub_client.reserve(kind="video", model=model_key, credit_cost=_cost, prompt=item.get("prompt", ""))
        _reserved = True
        if not _res.get("ok", True):
            _msg = _res.get("message") or "Hết credit nội bộ — liên hệ lead để được cấp thêm."
            db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=_msg)
            await hub.broadcast("item_error", {"task_id": task_id, "item_id": item_id, "error": _msg})
            return False
        _rid = _res.get("reservation_id")

        done_state = None
        last_err = None
        for attempt in range(MAX_ITEM_RETRIES):
            workflow = await client.generate_video(
                prompt=item["prompt"],
                model_key=model_key,
                aspect_ratio=aspect,
                reference_image=ref_media,
                end_image=(ref_media if loop else None),   # same image → loop
                duration=duration_s,
            )
            done_state = await client.wait_for_completion(workflow)
            if done_state and done_state.get("state") != "FAILED":
                break
            last_err = (done_state or {}).get("error", "") or "Generation failed"
            if attempt < MAX_ITEM_RETRIES - 1 and any(
                p in last_err.lower() for p in TRANSIENT_PATTERNS
            ):
                log.warning(
                    f"Item {item_id} attempt {attempt + 1}/{MAX_ITEM_RETRIES} "
                    f"transient error: {last_err} — retrying"
                )
                await asyncio.sleep(_rng.uniform(3, 6))
                continue
            raise RuntimeError(last_err)

        if not done_state or done_state.get("state") == "FAILED":
            raise RuntimeError(last_err or "Generation failed")

        media_id = (
            done_state.get("name")
            or done_state.get("media_id")
            or done_state.get("video_id")
        )
        if not media_id:
            raise RuntimeError("Video sinh xong nhưng response không có media_id")

        ext_dir = get_save_dir("video", task_id, task.get("name"))
        out_path = ext_dir / f"{project_item_stem(task, item_id)}.mp4"
        ok = await client.download_to(media_id, str(out_path))
        if not ok:
            raise RuntimeError("Download failed")

        db.update_item(item_id, status=ItemStatus.COMPLETED.value,
                       output_path=str(out_path), error_message=None)
        await hub.broadcast("item_completed", {
            "task_id": task_id, "item_id": item_id,
            "output_path": str(out_path),
        })
        # ── Firebase tracking (best-effort) ──
        try:
            from ..services import tracking as _tracking
            _email = task.get("user_email") or ""
            if not _email:
                from ..services.oauth_auth import load_session as _load_sess
                _sess = _load_sess()
                if _sess:
                    _email = (_sess.get("email") or "").strip()
            if _email:
                await _tracking.track_event(_email, "video_created")
        except Exception:
            pass
        if _reserved:
            await hub_client.commit_result(_rid, "done", kind="video", model=model_key,
                                           credit_cost=_cost, prompt=item.get("prompt", ""),
                                           path=str(out_path), is_video=True)
        return True
    except fc_mod.SessionDeadError:
        msg = ("Phiên đăng nhập của tài khoản đã hết hạn — đăng nhập lại ở "
               "tab Tài Khoản rồi gen lại.")
        db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=msg)
        await hub.broadcast("item_error", {
            "task_id": task_id, "item_id": item_id, "error": msg,
        })
        if _reserved:
            await hub_client.commit_result(_rid, "error", kind="video", model=model_key,
                                           credit_cost=_cost, prompt=item.get("prompt", ""))
        raise   # propagate so caller can disable the account
    except Exception as ex:
        raw = str(ex)
        friendly = friendly_error(raw)
        db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=friendly)
        await hub.broadcast("item_error", {
            "task_id": task_id, "item_id": item_id,
            "error": friendly, "error_detail": raw,
        })
        if _reserved:
            await hub_client.commit_result(_rid, "error", kind="video", model=model_key,
                                           credit_cost=_cost, prompt=item.get("prompt", ""))
        return False
    finally:
        await _broadcast_progress(task_id)


async def _process_task(task_id: int):
    task = db.get_task(task_id)
    items = db.get_task_items(task_id)
    if not task or not items:
        return
    # Normal generation = bridge priority class 2 (below regen=0, upscale=1).
    from ..services.browser_bridge import set_gen_priority, next_gen_seq
    set_gen_priority(2, next_gen_seq())
    db.update_task(task_id, status=TaskStatus.RUNNING.value, started_at=str(time.time()))
    await hub.broadcast("task_started", {"task_id": task_id})

    acc = _pick_account()
    if not acc:
        db.update_task(task_id, status=TaskStatus.ERROR.value)
        await hub.broadcast("task_error", {"task_id": task_id, "error": "Không có account khả dụng"})
        return

    client = None
    try:
        from ..services import flow_client as fc_mod
        from ..services.flow_factory import make_flow_client, get_page_for_account
        page = await get_page_for_account(acc)  # None in extension mode
        client = make_flow_client(
            page=page,
            cookie_path=acc.get("cookie_path") or "",
            account_email=acc["email"],
        )
        try:
            await client.ensure_token()
        except fc_mod.SessionDeadError as sde:
            await _handle_session_dead(task_id, acc, sde)
            return

        parallelism = max(1, min(task.get("concurrent") or 1, len(items)))
        # Random pause between batches — applied AFTER each batch of
        # `parallelism` items finishes, before launching the next.
        # Randomization (not a fixed value) is intentional: a constant
        # N-second gap is itself a signature Google's rate-detector can
        # pick up. Drawing from a uniform [min, max] interval mimics
        # natural user pacing.
        cd_min, cd_max = _read_cooldown_range()
        log.info(
            f"Task {task_id}: processing {len(items)} items "
            f"(batch_size={parallelism}, cooldown={cd_min}-{cd_max}s)"
        )

        # Process only items that aren't already COMPLETED. Each prompt is now
        # attempted INDEPENDENTLY — there is no longer a 403 "circuit breaker"
        # that cancels the rest of the batch after N consecutive failures.
        # Failures are recorded per-item; the user retries them via the
        # per-item "Gen lại" button or "Gen lại tất cả lỗi".
        pending = [
            it for it in items
            if it.get("status") != ItemStatus.COMPLETED.value
        ]
        batches = [
            pending[i:i + parallelism]
            for i in range(0, len(pending), parallelism)
        ]

        session_dead_err: Optional[fc_mod.SessionDeadError] = None
        for batch_idx, batch in enumerate(batches):
            if queue.is_paused(task_id):
                await queue.mark_paused(task_id)
                return
            async def _staggered_item(idx: int, it: dict):
                if idx > 0:
                    await asyncio.sleep(idx * 1.5)
                return await run_item_bounded(it, generate_content_item(client, task, it))

            batch_results = await asyncio.gather(
                *(_staggered_item(idx, it) for idx, it in enumerate(batch)),
                return_exceptions=True,
            )

            # Surface SessionDead at batch boundary — no point sleeping +
            # running more batches if the account is dead.
            for r in batch_results:
                if isinstance(r, fc_mod.SessionDeadError):
                    session_dead_err = r
                    break
            if session_dead_err:
                break

            # Cooldown between batches — random in [cd_min, cd_max]. Skip
            # if this was the last batch or if range is 0..0.
            is_last_batch = (batch_idx == len(batches) - 1)
            if cd_max > 0 and not is_last_batch:
                import random as _rnd
                wait_s = round(_rnd.uniform(cd_min, cd_max), 1)
                await hub.broadcast("task_batch_cooldown", {
                    "task_id": task_id,
                    "seconds": wait_s,
                    "min": cd_min,
                    "max": cd_max,
                    "batch_done": batch_idx + 1,
                    "batch_total": len(batches),
                })
                log.info(
                    f"Task {task_id}: batch {batch_idx + 1}/{len(batches)} done, "
                    f"sleeping {wait_s}s (random in [{cd_min}, {cd_max}]) "
                    f"before next batch"
                )
                await asyncio.sleep(wait_s)

        if session_dead_err is not None:
            await _handle_session_dead(task_id, acc, session_dead_err)
            return

        await _maybe_finalize(task_id)
    except fc_mod.SessionDeadError as sde:
        await _handle_session_dead(task_id, acc, sde)
    except Exception as e:
        log.exception("Task crashed")
        db.update_task(task_id, status=TaskStatus.ERROR.value)
        await hub.broadcast("task_error", {"task_id": task_id, "error": str(e)})
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        _active_tasks.pop(task_id, None)


@router.post("/start")
async def start_content_task(body: StartTaskRequest):
    if not body.prompts:
        raise HTTPException(400, "Prompts required")
    if not 1 <= body.videos_per_prompt <= 4:
        raise HTTPException(400, "videos_per_prompt phải từ 1 đến 4")
    task_name = (body.task_name or "").strip() or f"video_{int(time.time())}"
    # Clamp duration to what the chosen model actually supports
    safe_duration = clamp_duration(body.quality, body.duration)
    from ..services import hub_client
    task_id = db.create_task(
        name=task_name,
        mode=body.mode,
        quality=body.quality,
        aspect_ratio=body.aspect_ratio,
        resolution=body.resolution,
        duration=safe_duration,
        concurrent=body.concurrent,
        total_count=len(body.prompts) * body.videos_per_prompt,
        status=TaskStatus.PENDING.value,
        user_email=hub_client.current_user_email(),
    )
    for i, p in enumerate(body.prompts):
        extra = {}
        # reference_images may contain None for prompts without an image
        # (mixed I2V/T2V batch) — only store the field when actually set.
        if body.reference_images and i < len(body.reference_images):
            ref = body.reference_images[i]
            if ref:
                extra["reference_image"] = ref
                if body.loop:
                    extra["loop"] = True   # reuse this image as the last frame too
        # N video mỗi prompt — mỗi bản mang cùng `extra` (i2v: cùng ảnh frame-đầu → N video khác seed).
        for _ in range(body.videos_per_prompt):
            db.add_task_item(task_id, p, extra=extra or None)

    position = await queue.enqueue("content", task_id, _process_task)
    return {
        "task_id": task_id,
        "items": len(body.prompts) * body.videos_per_prompt,
        "queue_position": position,
        "queued": position > 0,
    }


@router.post("/cancel/{task_id}")
async def cancel_task(task_id: int):
    ok = await queue.cancel(task_id)
    if not ok:
        db.update_task(task_id, status=TaskStatus.CANCELLED.value)
        await hub.broadcast("task_cancelled", {"task_id": task_id})
    return {"ok": True, "queue_cancel": ok}


@router.get("/tasks")
async def list_tasks(limit: int = 50):
    return {"tasks": db.list_tasks(limit)}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return {"task": t, "items": db.get_task_items(task_id)}


# Max edge length for reference images sent to Google Flow. Google's
# generation pipeline downsamples anything larger internally — keeping
# bigger files only slows our upload (base64 inflates by ~33% on top of
# the wire transfer). 2048px preserves enough detail for character /
# style references; smaller is also fine.
REF_IMAGE_MAX_EDGE = 1920


def _prep_uploaded_image(file_path: Path) -> Path:
    """Process uploaded reference image into JPEG RGB (≤1920px, quality 90)
    matching storyboard behavior. Converts large PNG/WEBP/BMP into compact
    JPEG so bridge upload to Google Flow completes in 1-2 seconds.
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img)
            # Handle alpha by compositing on white background before RGB conversion
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[3])
                img = bg
            else:
                img = img.convert("RGB")

            w, h = img.size
            scale = min(1.0, REF_IMAGE_MAX_EDGE / max(w, h))
            if scale < 1.0:
                new_w = int(round(w * scale))
                new_h = int(round(h * scale))
                img = img.resize((new_w, new_h), Image.LANCZOS)

            out_jpg = file_path.with_suffix(".jpg")
            img.save(out_jpg, format="JPEG", quality=90, optimize=True)
            if out_jpg != file_path and file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
            log.info(
                f"upload-image: processed {file_path.name} → {out_jpg.name} "
                f"({out_jpg.stat().st_size // 1024}KB)"
            )
            return out_jpg
    except Exception as e:
        log.warning(f"upload-image: prep failed for {file_path.name} ({e}) — keeping raw")
        return file_path


@router.post("/upload-image")
async def upload_image(file: UploadFile = File(...)):
    """Upload reference/character image to temp folder.

    Processes uploaded reference image into JPEG RGB (≤1920px, quality 90)
    matching storyboard behavior. Large PNGs (e.g. 5MB) become compact ~300KB JPEGs.
    Returns the SAVED path (post-resize) so downstream code stays unchanged.
    """
    img_dir = OUTPUT_DIR / "uploaded_images"
    img_dir.mkdir(parents=True, exist_ok=True)
    ext = (Path(file.filename or "img.png").suffix or ".png").lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        ext = ".png"
    out = img_dir / f"{uuid.uuid4().hex}{ext}"
    with out.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Process in a thread so PIL resize doesn't block the event loop
    out = await asyncio.to_thread(_prep_uploaded_image, out)

    return {"path": str(out), "name": file.filename}


# ── Frame Extraction (G-Labs #3) ─────────────────────────────────────

class ExtractFrameRequest(BaseModel):
    video_path: str
    time_seconds: float | None = None  # None = last frame


@router.post("/extract-frame")
async def extract_frame(body: ExtractFrameRequest):
    """Extract a frame from a video file for use as reference image.

    Used for video chaining: extract last frame of one video → use as
    start image for the next generation. Returns the path to the
    extracted PNG.

    Args:
        video_path: Path to the video file (must be within OUTPUT_DIR)
        time_seconds: Optional timestamp. None = extract last frame.
    """
    vp = Path(body.video_path).resolve()

    # Security: ensure path is within OUTPUT_DIR
    try:
        vp.relative_to(Path(OUTPUT_DIR).resolve())
    except ValueError:
        raise HTTPException(400, "Video path must be within the output directory")

    if not vp.exists():
        raise HTTPException(404, f"Video not found: {body.video_path}")

    from ..services.frame_extractor import extract_last_frame, extract_frame_at

    if body.time_seconds is not None:
        frame_path = await extract_frame_at(str(vp), body.time_seconds)
    else:
        frame_path = await extract_last_frame(str(vp))

    if not frame_path:
        raise HTTPException(500, "Frame extraction failed — ffmpeg may not be available")

    return {"frame_path": frame_path, "video_path": str(vp)}


# ── Video Upscale (Real-ESRGAN) ──────────────────────────────────────

class UpscaleVideoRequest(BaseModel):
    video_paths: list[str]
    resolution: str = "FHD"  # "FHD", "2K", or "4K"
    denoise: float = -1  # -1 = model default, 0~1 = denoise strength
    model: str = "realesrgan-x4plus"
    user_email: Optional[str] = None


@router.get("/upscale-status")
async def upscale_status():
    """Check if the upscaler binary is available."""
    from ..services.upscaler import is_upscaler_available
    try:
        from ..services.topaz_upscaler import is_topaz_available
        has_topaz = is_topaz_available()
    except Exception:
        has_topaz = False
    return {
        "available": is_upscaler_available(),
        "has_topaz": has_topaz,
    }


# In-memory store for active upscale batches to support HTTP polling fallback
_upscale_progress: dict[str, dict] = {}
_upscale_sem: Optional[asyncio.Semaphore] = None
# Set of batch_ids that have been cancelled — checked by _run() between videos
_cancelled_batches: set[str] = set()
# Active subprocess per batch — killed on cancel
_active_upscale_procs: dict[str, asyncio.subprocess.Process] = {}

@router.get("/upscale-status/{batch_id}")
async def get_upscale_status(batch_id: str):
    if batch_id not in _upscale_progress:
        raise HTTPException(404, "Không tìm thấy thông tin lượt upscale")
    return _upscale_progress[batch_id]

@router.post("/upscale-cancel/{batch_id}")
async def cancel_upscale(batch_id: str):
    """Cancel a running video upscale batch."""
    if batch_id not in _upscale_progress:
        raise HTTPException(404, "Không tìm thấy batch")
    prog = _upscale_progress[batch_id]
    if prog.get("completed"):
        return {"ok": True, "message": "Batch đã hoàn tất"}
    _cancelled_batches.add(batch_id)
    # Kill active subprocess if any
    proc = _active_upscale_procs.get(batch_id)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        from ..services.topaz_upscaler import cancel_topaz_process
        cancel_topaz_process(batch_id)
    except Exception:
        pass
    prog.update({
        "stage": "cancelled",
        "message": "Đã hủy bởi người dùng",
        "completed": True,
    })
    await hub.broadcast("video_upscale_batch_done", {
        "batch_id": batch_id,
        "total": len(prog.get("results", [])),
        "results": prog.get("results", []),
        "cancelled": True,
    })
    log.info("Upscale batch %s cancelled by user", batch_id)
    return {"ok": True, "message": "Đã hủy"}

@router.post("/upscale-video")
async def upscale_video_endpoint(body: UpscaleVideoRequest):
    """Upscale one or more videos using Real-ESRGAN NCNN-Vulkan.

    Runs asynchronously in a background thread. Progress is broadcast
    via WebSocket events: video_upscale_progress, video_upscale_completed,
    video_upscale_error.
    """
    from ..services.upscaler import upscale_video, is_upscaler_available
    global _upscale_sem
    if _upscale_sem is None:
        _upscale_sem = asyncio.Semaphore(1)

    if not is_upscaler_available():
        raise HTTPException(
            400,
            "Video Upscale chưa sẵn sàng. Cài tính năng 'Video Upscale' "
            "từ Kho tính năng trước.",
        )

    # Validate all paths
    for vp_str in body.video_paths:
        vp = Path(vp_str).resolve()
        vp_lower = str(vp).lower().replace("\\", "/")
        out_lower = str(Path(OUTPUT_DIR).resolve()).lower().replace("\\", "/")
        if not vp_lower.startswith(out_lower):
            raise HTTPException(400, f"Path phải nằm trong thư mục output: {vp_str}")
        if not vp.exists():
            raise HTTPException(404, f"Video không tồn tại: {vp_str}")

    resolution = body.resolution if body.resolution in ("FHD", "2K", "4K") else "FHD"
    valid_models = (
        "topaz-proteus",
        "realesr-animevideov3",
        "realesrgan-x4plus",
        "realesrgan-x4plus-anime",
        "realesr-general-x4v3",
    )
    model = body.model if body.model in valid_models else "topaz-proteus"
    loop = asyncio.get_running_loop()
    batch_id = str(uuid.uuid4())[:8]

    # Resolve user email & display name for tracking
    # Always prioritize the logged-in OAuth session (@redone.vn)
    _email = ""
    _name = ""
    try:
        from ..services.oauth_auth import load_session as _load_sess
        _sess = _load_sess()
        if _sess and _sess.get("email"):
            _email = (_sess.get("email") or "").strip()
            _name = (_sess.get("name") or "").strip()
    except Exception:
        pass

    if not _email:
        _email = (body.user_email or "").strip()

    if not _email:
        try:
            from ..database import db
            _accts = db.get_accounts()
            if _accts:
                _email = (_accts[0].get("email") or "").strip()
        except Exception:
            pass

    # Initialize progress store entry
    _upscale_progress[batch_id] = {
        "batch_id": batch_id,
        "percent": 0,
        "stage": "starting",
        "message": "Đang xếp hàng...",
        "completed": False,
        "error": None,
        "results": [],
        "resolution": resolution,
        "denoise": body.denoise,
        "model": model,
    }

    async def _run():
        async with _upscale_sem:
            total = len(body.video_paths)
            results = []
        for idx, vp_str in enumerate(body.video_paths, 1):
            # ── Check cancellation before each video ──
            if batch_id in _cancelled_batches:
                _cancelled_batches.discard(batch_id)
                _active_upscale_procs.pop(batch_id, None)
                return
            try:
                # Update progress state
                _upscale_progress[batch_id].update({
                    "percent": 0,
                    "stage": "starting",
                    "message": f"Bắt đầu upscale {idx}/{total}...",
                    "video_path": vp_str,
                })

                await hub.broadcast("video_upscale_progress", {
                    "batch_id": batch_id,
                    "index": idx,
                    "total": total,
                    "percent": 0,
                    "stage": "starting",
                    "message": f"Bắt đầu upscale {idx}/{total}...",
                    "video_path": vp_str,
                })

                def emit(pct: float, stage: str, msg: str):
                    p_round = round(pct, 1)
                    _upscale_progress[batch_id].update({
                        "percent": p_round,
                        "stage": stage,
                        "message": msg,
                    })
                    coro = hub.broadcast("video_upscale_progress", {
                        "batch_id": batch_id,
                        "index": idx,
                        "total": total,
                        "percent": p_round,
                        "stage": stage,
                        "message": msg,
                        "video_path": vp_str,
                    })
                    try:
                        asyncio.get_running_loop()
                        asyncio.create_task(coro)
                    except RuntimeError:
                        asyncio.run_coroutine_threadsafe(coro, loop)

                output = await upscale_video(
                    input_path=vp_str,
                    resolution=resolution,
                    model=model,
                    denoise=body.denoise,
                    progress=emit,
                    task_id=batch_id,
                )
                
                res_item = {"input": vp_str, "output": output}
                results.append(res_item)
                _upscale_progress[batch_id]["results"].append(res_item)

                # ── Firebase tracking (best-effort) ──
                if _email:
                    try:
                        from ..services import tracking as _tracking
                        from pathlib import Path as _P
                        await _tracking.track_event(
                            _email,
                            "video_upscaled",
                            count=1,
                            display_name=_name,
                            extra={
                                "model": model,
                                "resolution": resolution,
                                "video_name": _P(vp_str).name,
                            },
                        )
                    except Exception as _tr_err:
                        log.debug("tracking video_upscaled failed: %s", _tr_err)

                await hub.broadcast("video_upscale_completed", {
                    "batch_id": batch_id,
                    "index": idx,
                    "total": total,
                    "video_path": vp_str,
                    "output_path": output,
                })

            except Exception as e:
                err_str = str(e)
                log.warning("Upscale failed for %s: %s", vp_str, e)
                _upscale_progress[batch_id].update({
                    "percent": 100,
                    "stage": "error",
                    "message": f"Lỗi: {err_str}",
                    "error": err_str,
                    "completed": True,
                })
                await hub.broadcast("video_upscale_error", {
                    "batch_id": batch_id,
                    "index": idx,
                    "total": total,
                    "video_path": vp_str,
                    "error": err_str,
                })
                await hub.broadcast("video_upscale_batch_done", {
                    "batch_id": batch_id,
                    "total": total,
                    "results": results,
                    "error": err_str,
                })
                return  # stop the batch execution on error

        _upscale_progress[batch_id].update({
            "percent": 100,
            "stage": "done",
            "message": "Hoàn tất!",
            "completed": True,
        })
        await hub.broadcast("video_upscale_batch_done", {
            "batch_id": batch_id,
            "total": total,
            "results": results,
        })

    asyncio.create_task(_run())
    return {"ok": True, "batch_id": batch_id, "total": len(body.video_paths)}
