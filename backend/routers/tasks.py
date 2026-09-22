"""Task management endpoints — list all jobs across kinds + cancel/delete."""
from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..database import db
from ..queue_manager import queue, shakker_queue
from ..ws_hub import hub
from ..config import OUTPUT_DIR, slugify_folder, TaskStatus, ItemStatus

log = logging.getLogger("redone.tasks")
router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Task modes that run through the IMAGE pipeline. Storyboard stores
# mode="storyboard" purely so Tasks Manager opens the Storyboard tab — it is
# created with queue.enqueue("image", ..., _process_image_task) and must be
# re-enqueued the same way. Miss it here and /resume + /retry silently hand a
# storyboard to the video runner: every unfinished scene gets a Veo t2v clip
# instead of an image, so the gallery <img> shows nothing while the counter
# keeps climbing.
IMAGE_MODES = ("image", "storyboard")


def _queue_for_mode(mode: str):
    """Which queue a task lives on. Shakker has its own concurrent lane."""
    return shakker_queue if (mode or "").lower() == "shakker" else queue


def _annotate(task: dict) -> dict:
    """Add queue position + progress percent to a DB task row."""
    pos = _queue_for_mode(task.get("mode")).position_of(task["id"])
    total = task.get("total_count") or 0
    done = task.get("done_count") or 0
    err = task.get("error_count") or 0
    progress = round(((done + err) / total) * 100, 1) if total else 0
    return {
        **task,
        "queue_position": pos,                 # 0 = running, ≥1 = queued, -1 = not in queue
        "progress_percent": progress,
    }


def _merged_snapshot() -> dict:
    """Combine the Flow + Shakker queue snapshots. `current`/`queued` stay
    present for backward-compat; `lanes` exposes each lane separately."""
    a = queue.snapshot()
    b = shakker_queue.snapshot()
    return {
        "current": a.get("current") or b.get("current"),
        "queued": (a.get("queued") or []) + (b.get("queued") or []),
        "lanes": {"flow": a, "shakker": b},
    }


@router.get("")
async def list_tasks(limit: int = 200):
    """All tasks (newest first) with queue + progress info."""
    rows = db.list_tasks(limit=limit)
    return {
        "tasks": [_annotate(r) for r in rows],
        "queue": _merged_snapshot(),
    }


@router.get("/{task_id}")
async def get_task(task_id: int):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    items = db.get_task_items(task_id)
    return {
        "task": _annotate(t),
        "items": items,
        "queue": queue.snapshot(),
    }


# ── Detached retry/regen runners registry ───────────────────────────
# /retry-failed, /retry-item, /retry-items run a regen coroutine OUTSIDE the
# sequential queue (so they can run mid-task). Track them per task_id so
# pause/cancel can stop them too — otherwise those buttons are unstoppable.
_retry_runners: dict[int, set] = {}


def _track_retry(task_id: int, coro):
    """Wrap a detached retry coroutine in a tracked asyncio.Task."""
    t = asyncio.create_task(coro)
    _retry_runners.setdefault(task_id, set()).add(t)
    t.add_done_callback(lambda fut, tid=task_id: _retry_runners.get(tid, set()).discard(fut))
    return t


async def _stop_retry_runners(task_id: int) -> bool:
    """Cancel any detached retry runners for this task and wait for them to
    settle. Returns True if any were running."""
    running = [t for t in _retry_runners.get(task_id, set()) if not t.done()]
    for t in running:
        t.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    _retry_runners.pop(task_id, None)
    return bool(running)


def _reset_inflight_items(task_id: int):
    """Flip items stuck mid-gen (GENERATING/UPLOADING/DOWNLOADING) back to
    PENDING so a paused/cancelled task has no stuck spinners + can resume."""
    redo = {ItemStatus.GENERATING.value, ItemStatus.UPLOADING.value, ItemStatus.DOWNLOADING.value}
    for it in db.get_task_items(task_id):
        if it.get("status") in redo:
            db.update_item(it["id"], status=ItemStatus.PENDING.value, error_message=None)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int):
    """Cancel a queued or running task (kind-agnostic). Also stops any detached
    retry/regen runner for this task (they bypass the queue)."""
    task = db.get_task(task_id)
    q = _queue_for_mode(task.get("mode") if task else "")
    ok = await q.cancel(task_id)
    if not ok:
        other = queue if q is shakker_queue else shakker_queue
        ok = await other.cancel(task_id)
    stopped_retry = await _stop_retry_runners(task_id)
    if stopped_retry:
        _reset_inflight_items(task_id)
    if not ok:
        # Not on a queue (finished, or was a detached retry) → mark DB cancelled.
        db.update_task(task_id, status=TaskStatus.CANCELLED.value)
        await hub.broadcast("task_cancelled", {"task_id": task_id})
    return {"ok": True, "queue_cancel": ok, "stopped_retry": stopped_retry}


@router.post("/{task_id}/retry")
async def retry_task(task_id: int):
    """Re-enqueue a failed / cancelled task.

    - Items with status=COMPLETED stay completed (their output files exist)
    - Items with status=ERROR / PENDING get reset to PENDING for re-processing
    - Picks the right runner based on task.mode
    """
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] in ("PENDING", "RUNNING"):
        raise HTTPException(400, "Task đang chạy / chờ — không thể retry")

    # Reset error items back to PENDING; recompute counters
    from ..config import ItemStatus
    items = db.get_task_items(task_id)
    done = 0
    for it in items:
        if it["status"] in (ItemStatus.ERROR.value, ItemStatus.PENDING.value):
            db.update_item(it["id"], status=ItemStatus.PENDING.value, error_message=None)
        elif it["status"] == ItemStatus.COMPLETED.value:
            done += 1
    db.update_task(task_id,
                   status="PENDING",
                   done_count=done,
                   error_count=0,
                   finished_at=None)

    info = await _reenqueue_task(task_id)
    await hub.broadcast("task_started", {"task_id": task_id, "retried": True})
    return {"ok": True, **info}


async def _reenqueue_task(task_id: int, front: bool = False) -> dict:
    """Pick the runner by task.mode and enqueue. Returns {queue_position, kind}.
    Shared by /retry (append) and /resume (front=True → run next)."""
    task = db.get_task(task_id) or {}
    mode = (task.get("mode") or "").lower()
    enqueue_q = queue
    if mode in IMAGE_MODES:
        from . import image as image_mod
        runner = image_mod._process_image_task
        kind = "image"
    elif mode == "shakker":
        from . import shakker as shakker_mod
        runner = shakker_mod._process_shakker_task
        kind = "shakker"
        enqueue_q = shakker_queue   # independent concurrent lane
    elif mode == "long_video":
        from . import long_video as lv_mod
        runner = lv_mod._run_long_video
        kind = "long_video"
    elif mode == "flow_upscale":
        from . import image as image_mod
        runner = image_mod._process_upscale_task
        kind = "image"
    elif mode == "video_render":
        from . import video_editor as ve_mod
        runner = ve_mod._process_video_render_task
        kind = "video_render"
    else:  # t2v / i2v / fallback
        from . import content as content_mod
        runner = content_mod._process_task
        kind = "content"
    pos = await enqueue_q.enqueue(kind, task_id, runner, front=front)
    return {"queue_position": pos, "kind": kind}


@router.post("/{task_id}/pause")
async def pause_task(task_id: int):
    """Pause a queued/running task so it can be resumed later (unlike cancel,
    which discards it). Stops the queue runner AND any detached retry/regen
    runner, then resets in-flight items to PENDING so resume continues them."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    q = _queue_for_mode(task.get("mode"))
    res = await q.pause(task_id)
    if res == "absent":
        other = queue if q is shakker_queue else shakker_queue
        res = await other.pause(task_id)
    # Detached retry/regen runners bypass the queue — stop them too.
    stopped_retry = await _stop_retry_runners(task_id)
    if res in ("running", "queued"):
        # queue.pause handled it: "running" -> worker resets items + marks PAUSED;
        # "queued" -> already marked PAUSED.
        return {"ok": True, "result": res, "stopped_retry": stopped_retry,
                "status": TaskStatus.PAUSED.value}
    # absent: a detached retry was running, or the task is active but idle in
    # the queue → mark PAUSED + reset in-flight items ourselves.
    if stopped_retry or task.get("status") in (TaskStatus.RUNNING.value, TaskStatus.PENDING.value):
        _reset_inflight_items(task_id)
        db.update_task(task_id, status=TaskStatus.PAUSED.value, finished_at=None)
        await hub.broadcast("task_paused", {"task_id": task_id})
    return {"ok": True, "result": res, "stopped_retry": stopped_retry,
            "status": TaskStatus.PAUSED.value}


@router.post("/{task_id}/resume")
async def resume_task(task_id: int):
    """Resume a PAUSED task — regenerates only items that aren't COMPLETED yet."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("status") != TaskStatus.PAUSED.value:
        raise HTTPException(400, "Task khong o trang thai Tam dung")
    items = db.get_task_items(task_id)
    done = 0
    redo = {
        ItemStatus.GENERATING.value, ItemStatus.UPLOADING.value,
        ItemStatus.DOWNLOADING.value, ItemStatus.ERROR.value, ItemStatus.PENDING.value,
    }
    for it in items:
        if it["status"] == ItemStatus.COMPLETED.value:
            done += 1
        elif it["status"] in redo:
            db.update_item(it["id"], status=ItemStatus.PENDING.value, error_message=None)
    db.update_task(task_id, status="PENDING", done_count=done, error_count=0, finished_at=None)
    info = await _reenqueue_task(task_id, front=True)   # Tiếp tục → chạy kế tiếp (đầu hàng đợi)
    await hub.broadcast("task_started", {"task_id": task_id, "resumed": True})
    return {"ok": True, **info}


# ── Per-item / failed-only retry (works WHILE the task is still running) ──
#
# Unlike /retry (which re-enqueues the whole task through the sequential
# queue and is blocked while RUNNING/PENDING), these regenerate individual
# items in a detached background coroutine with their OWN flow client. The
# main queue worker — if it's still processing this task — only touches the
# items it snapshotted at start (the PENDING ones); items that already
# reached ERROR are done as far as the loop is concerned, so retrying them
# out-of-band never double-processes anything. DB-authoritative counters
# (see content/image `_broadcast_progress`) keep the totals consistent
# across both writers.

async def _retry_items_runner(task: dict, item_ids: list[int], force: bool = False) -> None:
    """Background: regenerate `item_ids` for `task` using a fresh client.

    With `force=False` items already COMPLETED are skipped (retry-failed
    semantics). With `force=True` (user clicked "Gen lại" on a specific card)
    COMPLETED items ARE regenerated and overwritten in place — only items no
    longer in the DB are dropped.

    Runs at bridge priority class 0 (regen jumps ahead of normal gen=2 and
    upscale=1 in the bridge queue; in-flight fetches are not preempted, so a
    regen waits for the next free slot then runs first).

    Picks the image vs video single-item generator based on task.mode. Runs in
    batches of task.concurrent. On a dead session it disables the account and
    stops. Always finalizes the task (flip to COMPLETED if nothing is left
    pending/generating) and closes the client.
    """
    from . import content as content_mod, image as image_mod
    from ..services import flow_client as fc_mod
    from ..services.flow_factory import make_flow_client, get_page_for_account
    from ..services.browser_bridge import set_gen_priority, next_gen_seq

    # Regen = highest bridge priority (class 0). Earlier-clicked regens get a
    # smaller seq so they run before later-clicked ones.
    set_gen_priority(0, next_gen_seq())

    task_id = task["id"]
    mode = (task.get("mode") or "").lower()
    # Storyboard tasks generate IMAGES (one per scene) → use the image
    # generator. Without this they'd fall through to the video branch and
    # KeyError on task["quality"] / write a .mp4 for an image scene.
    if mode == "flow_upscale":
        # Flow upscale task — retry failed upscales with the upscale item runner
        # (not the image generator, which would try to re-generate the image).
        gen_fn = image_mod.upscale_one_item
        finalize = image_mod._maybe_finalize
        pick = image_mod._pick_account
    elif mode in ("image", "storyboard"):
        gen_fn = image_mod.generate_image_item
        finalize = image_mod._maybe_finalize
        pick = image_mod._pick_account
    else:
        gen_fn = content_mod.generate_content_item
        finalize = content_mod._maybe_finalize
        pick = content_mod._pick_account

    acc = pick()
    if not acc:
        await hub.broadcast("task_error", {
            "task_id": task_id, "error": "Không có account khả dụng để gen lại",
        })
        return

    client = None
    try:
        page = await get_page_for_account(acc)
        client = make_flow_client(
            page=page,
            cookie_path=acc.get("cookie_path") or "",
            account_email=acc["email"],
        )
        try:
            await client.ensure_token()
        except fc_mod.SessionDeadError as sde:
            db.update_account(acc["id"], enabled=0)
            await hub.broadcast("account_session_dead", {
                "account_id": acc["id"], "email": acc["email"], "reason": sde.reason,
            })
            await hub.broadcast("task_error", {
                "task_id": task_id,
                "error": f"Session {acc['email']} hết hạn — login lại ở tab Tài Khoản",
            })
            return

        # Re-fetch each item fresh. With force=False skip ones already done
        # (retry-failed). With force=True regenerate even COMPLETED items
        # (explicit "Gen lại" → overwrite in place).
        targets = []
        for iid in item_ids:
            it = db.get_item(iid)
            if not it:
                continue
            if not force and it.get("status") == ItemStatus.COMPLETED.value:
                continue
            targets.append(it)
        if not targets:
            await finalize(task_id)
            return

        parallelism = max(1, min(task.get("concurrent") or 1, len(targets)))
        batches = [targets[i:i + parallelism] for i in range(0, len(targets), parallelism)]
        for batch in batches:
            results = await asyncio.gather(
                *(gen_fn(client, task, it) for it in batch),
                return_exceptions=True,
            )
            # Stop early if the account died mid-retry.
            if any(isinstance(r, fc_mod.SessionDeadError) for r in results):
                db.update_account(acc["id"], enabled=0)
                await hub.broadcast("account_session_dead", {
                    "account_id": acc["id"], "email": acc["email"],
                    "reason": "session dead during retry",
                })
                break
        await finalize(task_id)
    except Exception:
        log.exception(f"retry runner crashed task={task_id}")
        # Best effort: still try to finalize so the UI doesn't hang in RUNNING.
        try:
            await finalize(task_id)
        except Exception:
            pass
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


class UpdateItemPromptRequest(BaseModel):
    prompt: str


@router.post("/item/{item_id}/prompt")
async def update_item_prompt(item_id: int, body: UpdateItemPromptRequest):
    """Rewrite one item's prompt so the next regen uses it.

    Nothing else is needed to make it take effect: _retry_items_runner re-reads
    every item with db.get_item() immediately before generating, and each
    generator dereferences item["prompt"] from that fresh row. Shared by the
    Shakker gallery too — its items live in the same task_items table.
    """
    if not db.get_item(item_id):
        raise HTTPException(404, "Không tìm thấy item")
    text = (body.prompt or "").strip()
    if not text:
        raise HTTPException(400, "Prompt không được để trống")
    db.update_item(item_id, prompt=text)
    return {"ok": True, "item_id": item_id, "prompt": text}


@router.post("/item/{item_id}/retry")
async def retry_item(item_id: int):
    """Regenerate a SINGLE item — even while the parent task is still
    generating others. Works on ERROR/PENDING items AND already-COMPLETED ones
    (explicit "Gen lại" → overwrite in place). Returns immediately; progress
    streams over WebSocket (item_status → item_completed/item_error)."""
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404, "Không tìm thấy item")
    task = db.get_task(item["task_id"])
    if not task:
        raise HTTPException(404, "Không tìm thấy task của item")

    # Flip a finished task back to RUNNING so the UI shows in-progress again.
    if task.get("status") in (
        TaskStatus.COMPLETED.value, TaskStatus.ERROR.value, TaskStatus.CANCELLED.value,
    ):
        db.update_task(task["id"], status=TaskStatus.RUNNING.value, finished_at=None)
        await hub.broadcast("task_started", {"task_id": task["id"], "retried": True})

    _track_retry(task["id"], _retry_items_runner(task, [item_id], force=True))
    return {"ok": True, "item_id": item_id}


@router.post("/{task_id}/retry-failed")
async def retry_failed(task_id: int):
    """Regenerate ALL currently-failed items of a task — works while the task
    is still running. Returns immediately; progress streams over WebSocket."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    items = db.get_task_items(task_id)
    failed_ids = [it["id"] for it in items if it.get("status") == ItemStatus.ERROR.value]
    if not failed_ids:
        return {"ok": True, "retried": 0}

    if task.get("status") in (
        TaskStatus.COMPLETED.value, TaskStatus.ERROR.value, TaskStatus.CANCELLED.value,
    ):
        db.update_task(task_id, status=TaskStatus.RUNNING.value, finished_at=None)
    await hub.broadcast("task_started", {"task_id": task_id, "retried": True})

    _track_retry(task_id, _retry_items_runner(task, failed_ids))
    return {"ok": True, "retried": len(failed_ids)}


class RetryItemsRequest(BaseModel):
    item_ids: list[int]


@router.post("/{task_id}/retry-items")
async def retry_items(task_id: int, body: RetryItemsRequest):
    """Regenerate a SPECIFIC LIST of items (the gallery's "Gen lại" toolbar
    button over the ticked cards). Runs ONE detached runner that batches the
    list by task.concurrent — far cheaper than firing N single-item retries
    (which would spin up N flow clients). force=True → overwrite COMPLETED in
    place. Returns immediately; progress streams over WebSocket."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    ids = [iid for iid in (body.item_ids or []) if db.get_item(iid)]
    if not ids:
        return {"ok": True, "retried": 0}

    if task.get("status") in (
        TaskStatus.COMPLETED.value, TaskStatus.ERROR.value, TaskStatus.CANCELLED.value,
    ):
        db.update_task(task_id, status=TaskStatus.RUNNING.value, finished_at=None)
    await hub.broadcast("task_started", {"task_id": task_id, "retried": True})

    _track_retry(task_id, _retry_items_runner(task, ids, force=True))
    return {"ok": True, "retried": len(ids)}


@router.get("/_/queue")
async def queue_state():
    """Get a snapshot of both queues (Flow + Shakker lanes)."""
    return _merged_snapshot()


def _resolve_task_folder(task: dict) -> Path | None:
    """Find the directory where a task's outputs live.

    Strategy: look at the first item that has a non-empty output_path and
    take its parent dir (most reliable). If no items have output yet, try
    composing the path from <kind>/<created_date>/<task_name>.
    """
    items = db.get_task_items(task["id"])
    for it in items:
        op = it.get("output_path")
        if op:
            p = Path(op).parent
            if p.exists():
                return p

    # Fallback: compose by convention
    mode = (task.get("mode") or "").lower()
    kind = "image" if mode in IMAGE_MODES else "video"

    # Use task's created_at date (UTC string "YYYY-MM-DD HH:MM:SS")
    created = task.get("created_at") or ""
    date_part = created[:10] if len(created) >= 10 else datetime.now().strftime("%Y-%m-%d")
    folder_name = slugify_folder(task.get("name") or "", fallback=f"task_{task['id']}")

    # Try permanent first, then pending
    for base in (OUTPUT_DIR, OUTPUT_DIR / "_pending"):
        cand = base / kind / date_part / folder_name
        if cand.exists():
            return cand
    return None


@router.post("/{task_id}/open-folder")
async def open_folder(task_id: int):
    """Open the OS file explorer at the task's output folder."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    folder = _resolve_task_folder(task)
    if not folder or not folder.exists():
        raise HTTPException(
            404,
            "Chưa có thư mục output cho task này (task chưa render xong file nào)",
        )

    folder_str = str(folder.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(folder_str)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder_str])
        else:
            subprocess.Popen(["xdg-open", folder_str])
    except Exception as e:
        log.exception("open-folder failed")
        raise HTTPException(500, f"Không mở được folder: {e}")

    return {"ok": True, "path": folder_str}
