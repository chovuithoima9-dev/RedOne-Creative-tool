"""Image generation endpoints (Imagen / Nano Banana via Google Labs)."""
from __future__ import annotations
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import db
from ..config import OUTPUT_DIR, TaskStatus, ItemStatus, get_save_dir, project_item_stem
from ..ws_hub import hub
from ..queue_manager import queue

log = logging.getLogger("redone.image")
router = APIRouter(prefix="/api/image", tags=["image"])


class StartImageRequest(BaseModel):
    prompts: list[str]
    model: str = "nano_banana_pro"     # nano_banana_pro | nano_banana_2 | imagen_4
    aspect_ratio: str = "1:1"
    count_per_prompt: int = 1
    concurrent: int = 1                 # số luồng song song
    reference_image_paths: Optional[list[str]] = None  # already uploaded
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


async def _handle_session_dead_img(task_id, acc, sde):
    log.warning(f"[image] Session dead {acc['email']}: {sde.reason}")
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


# ── DB-authoritative progress helpers (image variant) ───────────────
# Mirror content.py's helpers but broadcast with kind="image" so the image
# gallery's WS reducer can tell events apart. Counts come from the DB so the
# main batch loop and the per-item/retry-all endpoints can run concurrently
# without racing on a shared in-memory counter.

def _count_items_img(task_id: int) -> dict:
    items = db.get_task_items(task_id)
    done = sum(1 for it in items if it.get("status") == ItemStatus.COMPLETED.value)
    err = sum(1 for it in items if it.get("status") == ItemStatus.ERROR.value)
    gen = sum(1 for it in items if it.get("status") == ItemStatus.GENERATING.value)
    pend = sum(1 for it in items if it.get("status") == ItemStatus.PENDING.value)
    return {"done": done, "error": err, "generating": gen, "pending": pend, "total": len(items)}


async def _broadcast_progress_img(task_id: int) -> dict:
    c = _count_items_img(task_id)
    db.update_task(task_id, done_count=c["done"], error_count=c["error"])
    await hub.broadcast("task_progress", {
        "task_id": task_id, "done": c["done"], "error": c["error"],
        "total": c["total"], "kind": "image",
    })
    return c


async def _maybe_finalize(task_id: int) -> None:
    """Mark COMPLETED + broadcast only when nothing is pending/generating.
    See content.py._maybe_finalize for the race-safety rationale."""
    c = _count_items_img(task_id)
    if c["generating"] == 0 and c["pending"] == 0:
        task = db.get_task(task_id)
        if task and task.get("status") != TaskStatus.COMPLETED.value:
            db.update_task(task_id, status=TaskStatus.COMPLETED.value,
                           finished_at=str(time.time()))
            await hub.broadcast("task_completed", {
                "task_id": task_id, "done": c["done"], "error": c["error"],
                "kind": "image",
            })


async def generate_image_item(client, task: dict, item: dict) -> bool:
    """Generate + download ONE image item. Updates DB + broadcasts events.
    Returns True on success, False on handled failure; re-raises
    SessionDeadError. Reused by the batch loop and the retry endpoints —
    that's what lets a retry run while the parent task is still generating.
    """
    import random as _rng
    from ..services import flow_client as fc_mod
    from ..services import hub_client
    from ..services.error_messages import friendly_error

    task_id = task["id"]
    item_id = item["id"]
    model = task["image_model"]
    aspect = task["aspect_ratio"]

    # Mark GENERATING in the DB *before* the jitter sleep. A regen-from-done
    # flips this item COMPLETED→GENERATING; if we slept first the item would
    # still read COMPLETED during the sleep window, and a sibling item
    # finishing in that window could trip _maybe_finalize into broadcasting
    # task_completed prematurely. Writing the status first closes that race.
    db.update_item(item_id, status=ItemStatus.GENERATING.value, error_message=None)
    await hub.broadcast("item_status", {
        "task_id": task_id, "item_id": item_id,
        "status": ItemStatus.GENERATING.value,
        # Carry the prompt so the frontend pairs prompt↔image by item_id —
        # essential for the Storyboard view where concurrent gen can claim
        # slots out of order.
        "prompt": item.get("prompt", ""),
    })
    # Jitter to avoid hammering Google at the same ms
    await asyncio.sleep(_rng.uniform(0.0, 1.2))

    # Hub internal-credit reserve (no-op when Hub disabled/unreachable). Only a
    # genuine "out of credit" (Hub reachable) blocks the gen — any Hub problem
    # degrades to allow, so gen never breaks because of the Hub.
    # Images are FREE on Google Flow (nano_banana / imagen don't consume Flow
    # credits — per Flow's official pricing table), so deduct 0.
    _cost = 0
    _res = await hub_client.reserve(kind="image", model=model, credit_cost=_cost, prompt=item.get("prompt", ""))
    if not _res.get("ok", True):
        _msg = _res.get("message") or "Hết credit nội bộ — liên hệ lead để được cấp thêm."
        db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=_msg)
        await hub.broadcast("item_error", {"task_id": task_id, "item_id": item_id, "error": _msg})
        await _broadcast_progress_img(task_id)
        return False
    _rid = _res.get("reservation_id")
    try:
        extra = json.loads(item.get("extra_json") or "{}")
        ref_paths = extra.get("reference_images") or []
        ref_media_ids = []
        for p in ref_paths:
            if Path(p).exists():
                try:
                    mid = await client.upload_image(p)
                    if mid:
                        ref_media_ids.append(mid)
                except Exception as e:
                    log.warning(f"Upload ref image failed: {e}")

        result = await client.generate_image(
            prompt=item["prompt"],
            model_key=model,
            aspect_ratio=aspect,
            reference_images=ref_media_ids or None,
        )

        out_dir = get_save_dir("image", task_id, task.get("name"))
        out_path = out_dir / f"{project_item_stem(task, item_id)}.png"
        ok = await client.download_image(result["download_url"], str(out_path))
        if not ok:
            raise RuntimeError("Image download failed")

        # Nano Banana trả ảnh 16:9 ở 1376×768 (lệch tỉ lệ). Crop 2 bên về đúng
        # 1366×768. Chỉ áp dụng cho 16:9; các tỉ lệ khác giữ nguyên. Tái dùng
        # cùng resize_image(mode="cover") mà tab "Resize Hàng Loạt" dùng.
        out_w, out_h = result.get("width"), result.get("height")
        if aspect == "16:9":
            try:
                from ..services.image_utils import crop_cover_inplace
                if crop_cover_inplace(out_path, width=1366, height=768):
                    out_w, out_h = 1366, 768
            except Exception as e:
                log.warning(f"Crop 16:9 failed (giữ ảnh gốc): {e}")

        # Google Flow now stamps a Gemini ✦ watermark (bottom-right) on every
        # nano_banana_* image. Strip it in place so outputs ship clean. Runs
        # AFTER the 16:9 crop so the corner box maps to the final dimensions.
        # Best-effort: never fails the gen (keeps the original on any error).
        _wm_on = db.get_setting("auto_remove_image_watermark", True)
        if (model in ("nano_banana_pro", "nano_banana_2", "nano_banana_lite")
                and _wm_on):
            from ..services.watermark_video import remove_gemini_watermark_inplace
            await remove_gemini_watermark_inplace(out_path)
        else:
            # Say WHY we skipped — a silent skip is indistinguishable from a
            # successful strip when someone asks "why does this one still have
            # the watermark?".
            log.info("Bỏ qua xóa WM ảnh (%s): model=%s setting=%s",
                     out_path.name, model, _wm_on)

        # Persist media_id so the upscale endpoint can find it later.
        extra["media_id"] = result.get("media_id")
        db.update_item(
            item_id,
            status=ItemStatus.COMPLETED.value,
            output_path=str(out_path),
            extra_json=json.dumps(extra),
            error_message=None,
        )
        await hub.broadcast("item_completed", {
            "task_id": task_id, "item_id": item_id,
            "output_path": str(out_path),
            "kind": "image",
            "width": out_w,
            "height": out_h,
            "media_id": result.get("media_id"),
            "prompt": item.get("prompt", ""),
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
                await _tracking.track_event(_email, "image_created")
        except Exception:
            pass
        await hub_client.commit_result(_rid, "done", kind="image", model=model,
                                       credit_cost=_cost, prompt=item.get("prompt", ""),
                                       path=str(out_path), is_video=False)
        return True
    except fc_mod.SessionDeadError:
        msg = ("Phiên đăng nhập của tài khoản đã hết hạn — đăng nhập lại ở "
               "tab Tài Khoản rồi gen lại.")
        db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=msg)
        await hub.broadcast("item_error", {
            "task_id": task_id, "item_id": item_id, "error": msg,
        })
        await hub_client.commit_result(_rid, "error", kind="image", model=model,
                                       credit_cost=_cost, prompt=item.get("prompt", ""))
        raise
    except Exception as ex:
        raw = str(ex)
        friendly = friendly_error(raw)
        db.update_item(item_id, status=ItemStatus.ERROR.value, error_message=friendly)
        await hub.broadcast("item_error", {
            "task_id": task_id, "item_id": item_id,
            "error": friendly, "error_detail": raw,
        })
        await hub_client.commit_result(_rid, "error", kind="image", model=model,
                                       credit_cost=_cost, prompt=item.get("prompt", ""))
        return False
    finally:
        await _broadcast_progress_img(task_id)


async def _process_image_task(task_id: int):
    task = db.get_task(task_id)
    items = db.get_task_items(task_id)
    if not task or not items:
        return
    # Normal generation = bridge priority class 2 (below regen=0 and
    # upscale=1). Set once at the runner top; every proxy_fetch issued by the
    # gathered per-item coroutines inherits this via the copied context.
    from ..services.browser_bridge import set_gen_priority, next_gen_seq
    set_gen_priority(2, next_gen_seq())
    db.update_task(task_id, status=TaskStatus.RUNNING.value, started_at=str(time.time()))
    await hub.broadcast("task_started", {"task_id": task_id, "kind": "image"})

    acc = _pick_account()
    if not acc:
        db.update_task(task_id, status=TaskStatus.ERROR.value)
        await hub.broadcast("task_error", {"task_id": task_id, "error": "Không có account khả dụng"})
        return

    client = None
    try:
        from ..services import flow_client as fc_mod
        from ..services.flow_factory import make_flow_client, get_page_for_account
        page = await get_page_for_account(acc)
        client = make_flow_client(
            page=page,
            cookie_path=acc.get("cookie_path") or "",
            account_email=acc["email"],
        )
        try:
            await client.ensure_token()
        except fc_mod.SessionDeadError as sde:
            await _handle_session_dead_img(task_id, acc, sde)
            return

        parallelism = max(1, min(task.get("concurrent") or 1, len(items)))
        # Random pause between batches — see content.py for rationale.
        # Shares the same settings keys so user only tunes once.
        from .content import _read_cooldown_range
        cd_min, cd_max = _read_cooldown_range()
        log.info(
            f"Image task {task_id}: {len(items)} items "
            f"(batch_size={parallelism}, cooldown={cd_min}-{cd_max}s)"
        )

        # Each prompt is attempted INDEPENDENTLY — no 403 circuit breaker that
        # cancels the rest after N consecutive failures. Failed items are
        # retried via the per-item "Gen lại" button / "Gen lại tất cả lỗi".
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
            from .content import run_item_bounded

            async def _staggered_item(idx: int, it: dict):
                if idx > 0:
                    await asyncio.sleep(idx * 1.5)
                return await run_item_bounded(it, generate_image_item(client, task, it))

            batch_results = await asyncio.gather(
                *(_staggered_item(idx, it) for idx, it in enumerate(batch)),
                return_exceptions=True,
            )

            for r in batch_results:
                if isinstance(r, fc_mod.SessionDeadError):
                    session_dead_err = r
                    break
            if session_dead_err:
                break

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
                    "kind": "image",
                })
                log.info(
                    f"Image task {task_id}: batch {batch_idx + 1}/{len(batches)} "
                    f"done, sleeping {wait_s}s (random in [{cd_min}, {cd_max}])"
                )
                await asyncio.sleep(wait_s)

        if session_dead_err is not None:
            await _handle_session_dead_img(task_id, acc, session_dead_err)
            return

        await _maybe_finalize(task_id)
    except fc_mod.SessionDeadError as sde:
        await _handle_session_dead_img(task_id, acc, sde)
    except Exception as e:
        log.exception("Image task crashed")
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
async def start_image_task(body: StartImageRequest):
    if not body.prompts:
        raise HTTPException(400, "Cần ít nhất 1 prompt")
    if body.count_per_prompt < 1 or body.count_per_prompt > 8:
        raise HTTPException(400, "count_per_prompt phải từ 1 đến 8")

    # Expand prompts × count_per_prompt
    expanded = []
    for p in body.prompts:
        for _ in range(body.count_per_prompt):
            expanded.append(p)

    task_name = (body.task_name or "").strip() or f"image_{int(time.time())}"
    from ..services import hub_client
    task_id = db.create_task(
        name=task_name,
        mode="image",
        image_model=body.model,
        aspect_ratio=body.aspect_ratio,
        concurrent=max(1, min(body.concurrent or 1, 8)),
        total_count=len(expanded),
        status=TaskStatus.PENDING.value,
        user_email=hub_client.current_user_email(),
    )
    extra_global = {"reference_images": body.reference_image_paths} if body.reference_image_paths else None
    for p in expanded:
        db.add_task_item(task_id, p, extra=extra_global)

    position = await queue.enqueue("image", task_id, _process_image_task)
    return {
        "task_id": task_id,
        "items": len(expanded),
        "queue_position": position,
        "queued": position > 0,
    }


@router.post("/cancel/{task_id}")
async def cancel_image(task_id: int):
    ok = await queue.cancel(task_id)
    if not ok:
        db.update_task(task_id, status=TaskStatus.CANCELLED.value)
        await hub.broadcast("task_cancelled", {"task_id": task_id})
    return {"ok": True, "queue_cancel": ok}


def _find_item(item_id: int) -> tuple[Optional[dict], Optional[dict]]:
    """Look up (task, item) by item_id across all tasks. Returns (None, None)
    if not found. Only searches the last 200 tasks — enough for practical use,
    since upscale is invoked from the currently visible gallery."""
    for t in db.list_tasks(limit=200):
        for it in db.get_task_items(t["id"]):
            if it["id"] == item_id:
                return t, it
    return None, None


def _save_upscaled(item_id: int, resolution: str, raw_bytes: bytes, task: dict) -> Path:
    """Save upscaled JPEG bytes to outputs/image/<date>/<task>/upscaled/."""
    # Mirror the original task's folder so the upscaled file lives next to
    # the source — keeps everything tidy when the user opens the folder.
    base_dir = get_save_dir("image", task["id"], task.get("name"))
    upscale_dir = base_dir / "upscaled"
    upscale_dir.mkdir(parents=True, exist_ok=True)
    out_path = upscale_dir / f"item_{item_id}_{resolution}.jpg"
    out_path.write_bytes(raw_bytes)
    return out_path


async def _do_upscale_one(client, item_id: int, media_id: str, resolution: str, task: dict) -> dict:
    """Run one upscale call + save result. Raises on failure."""
    r = await client.upscale_image(media_id, resolution=resolution)
    raw = r.get("encoded_image")
    if not raw:
        # Fallback path: response had fifeUrl instead of encodedImage
        url = r.get("download_url")
        if not url:
            raise RuntimeError("Upscale response thiếu cả encoded_image lẫn download_url")
        out_dir = get_save_dir("image", task["id"], task.get("name")) / "upscaled"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"item_{item_id}_{resolution}.png"
        ok = await client.download_image(url, str(out_path))
        if not ok:
            raise RuntimeError("Tải ảnh upscaled thất bại")
    else:
        out_path = _save_upscaled(item_id, resolution, raw, task)

    # Flow stamps a FRESH Gemini ✦ on every upscale — the source image having been
    # cleaned at gen time does NOT carry over. Erase it BEFORE the crop below: the
    # glyph is ~48px here (the size Google stamps it at, which the detector is
    # calibrated for), while cover-cropping to 2K/4K shrinks it to ~45px/~34px.
    if db.get_setting("auto_remove_image_watermark", True):
        from ..services.watermark_video import remove_gemini_watermark_inplace
        await remove_gemini_watermark_inplace(out_path)

    # Chuẩn hóa ra ĐÚNG kích thước 16:9 tiêu chuẩn: 2K=2560x1440, 4K=3840x2160.
    # Xoay theo hướng ảnh (ngang→2560x1440/3840x2160, dọc→1440x2560/2160x3840).
    # Cover-crop: scale phủ kín rồi cắt giữa phần dư → ĐÚNG kích thước, KHÔNG méo
    # (ảnh Google ~16:9 chỉ bị cắt vài chục px). Ảnh vuông sẽ bị cắt về 16:9.
    up_w, up_h = r.get("width"), r.get("height")
    _long, _short = {"2k": (2560, 1440), "4k": (3840, 2160)}.get(resolution.lower(), (3840, 2160))
    try:
        from ..services.image_utils import crop_cover_inplace
        from PIL import Image
        with Image.open(out_path) as _im:
            sw, sh = _im.size
        tw, th = (_long, _short) if sw >= sh else (_short, _long)
        crop_cover_inplace(out_path, width=tw, height=th)
        up_w, up_h = tw, th
    except Exception as e:
        log.warning(f"Chuẩn hóa kích thước upscale thất bại: {e}")

    rel = out_path.relative_to(OUTPUT_DIR).as_posix()
    return {
        "item_id": item_id,
        "path": str(out_path),
        "url": f"/files/{rel}",
        "width": up_w,
        "height": up_h,
    }


async def upscale_one_item(client, task: dict, item: dict) -> bool:
    """Upscale ONE queued upscale-item. Mirrors generate_image_item's contract
    (updates DB + broadcasts, returns True/False, re-raises SessionDeadError) so
    it is reused by BOTH the batch runner and the failed-item retry runner.

    The item's extra_json points at the SOURCE image being upscaled:
      {src_item_id, media_id, resolution, src_task_id, src_task_name}
    Results attach to the ORIGINAL image card via the same upscale_* WS events
    (item_id = src_item_id), so the gallery display is unchanged."""
    from ..services import flow_client as fc_mod
    from ..services.error_messages import friendly_error
    task_id = task["id"]
    iid = item["id"]
    ex = json.loads(item.get("extra_json") or "{}")
    src_item_id = ex.get("src_item_id") or iid
    media_id = ex.get("media_id")
    resolution = (ex.get("resolution") or "4k").lower()
    src_task = {"id": ex.get("src_task_id") or task_id, "name": ex.get("src_task_name") or task.get("name")}

    c0 = _count_items_img(task_id)
    db.update_item(iid, status=ItemStatus.GENERATING.value, error_message=None)
    await hub.broadcast("upscale_started", {
        "item_id": src_item_id, "resolution": resolution,
        "index": min(c0["total"], c0["done"] + c0["error"] + 1), "total": c0["total"],
    })
    try:
        if not media_id:
            raise RuntimeError("Thiếu media_id — ảnh tạo trước khi tool hỗ trợ upscale")
        r = await _do_upscale_one(client, src_item_id, media_id, resolution, src_task)
        db.update_item(iid, status=ItemStatus.COMPLETED.value, output_path=r["path"])
        c = await _broadcast_progress_img(task_id)
        await hub.broadcast("upscale_completed", {
            "item_id": src_item_id, "resolution": resolution,
            "path": r["path"], "url": r["url"],
            "index": c["done"], "total": c["total"],
        })
        return True
    except fc_mod.SessionDeadError:
        raise
    except Exception as e:
        friendly = friendly_error(str(e))
        log.warning(f"Upscale item={iid} (src={src_item_id}) failed: {e}")
        db.update_item(iid, status=ItemStatus.ERROR.value, error_message=friendly)
        c = await _broadcast_progress_img(task_id)
        await hub.broadcast("upscale_error", {
            "item_id": src_item_id, "resolution": resolution, "error": friendly,
            "index": c["done"] + c["error"], "total": c["total"],
        })
        return False


async def _process_upscale_task(task_id: int):
    """Queue runner for a Flow upscale TASK (mode='flow_upscale'). Sequential
    (concurrent=1) because Google's reCAPTCHA gets stricter on bursts. Goes
    through the SAME queue/pause/cancel/resume machinery as normal gen — pause
    bails at item boundaries (leaving the item PENDING), cancel propagates the
    CancelledError which the queue worker turns into CANCELLED."""
    task = db.get_task(task_id)
    items = db.get_task_items(task_id)
    if not task or not items:
        return
    # Upscale = bridge priority class 1 (above normal gen=2, below regen=0).
    from ..services.browser_bridge import set_gen_priority, next_gen_seq
    set_gen_priority(1, next_gen_seq())
    db.update_task(task_id, status=TaskStatus.RUNNING.value, started_at=str(time.time()))
    cfg = json.loads(task.get("character_images_json") or "{}")
    resolution = (cfg.get("resolution") or "4k").lower()
    await hub.broadcast("task_started", {"task_id": task_id, "kind": "image"})
    await hub.broadcast("upscale_batch_started", {
        "total": task.get("total_count") or len(items),
        "resolution": resolution, "task_id": task_id,
    })

    acc = _pick_account()
    if not acc:
        db.update_task(task_id, status=TaskStatus.ERROR.value)
        await hub.broadcast("task_error", {"task_id": task_id, "error": "Không có account khả dụng"})
        return

    client = None
    try:
        from ..services import flow_client as fc_mod
        from ..services.flow_factory import make_flow_client, get_page_for_account
        page = await get_page_for_account(acc)
        client = make_flow_client(
            page=page,
            cookie_path=acc.get("cookie_path") or "",
            account_email=acc["email"],
        )
        try:
            await client.ensure_token()
        except fc_mod.SessionDeadError as sde:
            await _handle_session_dead_img(task_id, acc, sde)
            return

        pending = [it for it in items if it.get("status") != ItemStatus.COMPLETED.value]
        for it in pending:
            if queue.is_paused(task_id):
                await queue.mark_paused(task_id)
                return
            try:
                await upscale_one_item(client, task, it)
            except fc_mod.SessionDeadError as sde:
                await _handle_session_dead_img(task_id, acc, sde)
                return

        await hub.broadcast("upscale_batch_done", {"resolution": resolution, "task_id": task_id})
        await _maybe_finalize(task_id)
    except fc_mod.SessionDeadError as sde:
        await _handle_session_dead_img(task_id, acc, sde)
    except Exception as e:
        log.exception("Upscale task crashed")
        db.update_task(task_id, status=TaskStatus.ERROR.value)
        await hub.broadcast("task_error", {"task_id": task_id, "error": str(e)})
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


@router.post("/upscale/{item_id}")
async def upscale_existing(item_id: int, resolution: str = "4k"):
    """Upscale a single already-generated image to 2K/4K via Google Labs."""
    # Upscale = bridge priority class 1: above normal gen (2), below regen (0).
    from ..services.browser_bridge import set_gen_priority, next_gen_seq
    set_gen_priority(1, next_gen_seq())
    if resolution.lower() not in ("2k", "4k"):
        raise HTTPException(400, "resolution phải là '2k' hoặc '4k'")

    task, item = _find_item(item_id)
    if not item or not task:
        raise HTTPException(404, "Item not found")
    extra = json.loads(item.get("extra_json") or "{}")
    media_id = extra.get("media_id")
    if not media_id:
        raise HTTPException(
            400,
            "Item không có media_id — ảnh này được tạo trước khi tool lưu media_id, "
            "cần generate lại để dùng tính năng upscale."
        )

    acc = _pick_account()
    if not acc:
        raise HTTPException(400, "Không có account khả dụng")
    client = None
    try:
        from ..services import flow_client as fc_mod
        from ..services.flow_factory import make_flow_client, get_page_for_account
        page = await get_page_for_account(acc)
        client = make_flow_client(
            page=page,
            cookie_path=acc.get("cookie_path") or "",
            account_email=acc["email"],
        )
        await client.ensure_token()
        result = await _do_upscale_one(client, item_id, media_id, resolution, task)
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Upscale item={item_id} failed")
        raise HTTPException(500, str(e))
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


class UpscaleBatchRequest(BaseModel):
    item_ids: list[int]
    resolution: str = "4k"   # "2k" | "4k"


@router.post("/upscale-batch")
async def upscale_batch(body: UpscaleBatchRequest):
    """Upscale multiple images — runs as a QUEUED TASK (mode='flow_upscale') so
    it can be Paused / Resumed / Cancelled from "Quản lý task", exactly like
    Shakker upscale. Returns a task_id immediately; per-image results stream
    back over the same upscale_* WS events (the gallery updates the ORIGINAL
    cards), and the task shows in the queue with pause/cancel/resume buttons.

    Sequential (concurrent=1) because Google's reCAPTCHA gets stricter on bursts.
    """
    if body.resolution.lower() not in ("2k", "4k"):
        raise HTTPException(400, "resolution phải là '2k' hoặc '4k'")
    if not body.item_ids:
        raise HTTPException(400, "item_ids rỗng")

    # Resolve src items + media_ids up-front; skip ones with no media_id.
    resolved: list[tuple[dict, dict, str]] = []
    skipped: list[dict] = []
    for iid in body.item_ids:
        src_task, item = _find_item(iid)
        if not item or not src_task:
            skipped.append({"item_id": iid, "error": "Không tìm thấy item"})
            continue
        extra = json.loads(item.get("extra_json") or "{}")
        mid = extra.get("media_id")
        if not mid:
            skipped.append({"item_id": iid, "error": "Thiếu media_id (ảnh tạo trước khi tool support upscale)"})
            continue
        resolved.append((src_task, item, mid))

    if not resolved:
        raise HTTPException(
            400,
            "Tất cả items đã chọn đều không có media_id — "
            "tính năng upscale chỉ áp dụng cho ảnh tạo từ phiên bản 1.0.3 trở đi."
        )

    res = body.resolution.lower()
    from ..services import hub_client
    # Name the upscale task after the SOURCE task: "<tên task gốc>_upscale 4K".
    # (Batches are normally from one gen task; fall back to a generic name if the
    # source task has no name.)
    _src_name = (resolved[0][0].get("name") or "").strip()
    task_name = f"{_src_name}_upscale {res.upper()}" if _src_name else f"Upscale {res.upper()} ({len(resolved)} ảnh)"
    task_id = db.create_task(
        name=task_name,
        mode="flow_upscale",
        image_model=f"upscale_{res}",
        aspect_ratio="",
        concurrent=1,                 # sequential — reCAPTCHA
        total_count=len(resolved),
        status=TaskStatus.PENDING.value,
        character_images_json=json.dumps({
            "upscale": True, "resolution": res,
            "parent_task_id": resolved[0][0]["id"],   # task gốc → lồng cha-con trong Quản lý task
        }),
        user_email=hub_client.current_user_email(),
    )
    for (src_task, item, mid) in resolved:
        db.add_task_item(task_id, f"Upscale {res.upper()}", extra={
            "src_item_id": item["id"],
            "media_id": mid,
            "resolution": res,
            "src_task_id": src_task["id"],
            "src_task_name": src_task.get("name"),
        })

    position = await queue.enqueue("image", task_id, _process_upscale_task)
    return {
        "ok": True,
        "task_id": task_id,
        "items": len(resolved),
        "skipped": skipped,
        "queue_position": position,
        "queued": position > 0,
        "resolution": res,
    }
