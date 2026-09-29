"""Sequential task queue — supports parallel multi-account generation tasks.

Workflow:
  - Routers (content/image/long_video) call `queue.enqueue(kind, task_id, runner, flow_account_email=...)`.
  - Up to `max_concurrency` tasks can run in parallel (default 2 for Google Flow queue).
  - Per-account lock: tasks sharing the same `flow_account_email` are serialized
    (one at a time per Google Flow account to prevent rate-limit / session collision).
  - Tasks on different Google Flow accounts run simultaneously in parallel.
  - Cancellation & Pause: can target waiting or in-flight tasks.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Awaitable, Callable, Optional

from .database import db
from .ws_hub import hub
from .config import TaskStatus

log = logging.getLogger("redone.queue")

Runner = Callable[[int], Awaitable[None]]


@dataclass
class QueuedItem:
    task_id: int
    kind: str           # "content" | "image" | "long_video"
    enqueued_at: float = field(default_factory=time.time)
    flow_account_email: Optional[str] = None


class TaskQueue:
    def __init__(self, max_concurrency: int = 1):
        self.max_concurrency = max_concurrency
        self._items: list[QueuedItem] = []
        self._runners: dict[int, Runner] = {}      # task_id → runner coro
        self._running: dict[int, QueuedItem] = {}  # task_id → QueuedItem
        self._running_tasks: dict[int, asyncio.Task] = {} # task_id → asyncio.Task
        self._cancel_set: set[int] = set()         # tasks marked to skip / cancel
        self._pause_set: set[int] = set()          # tasks paused (resumable, not cancelled)
        self._signal = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None

    # ── backwards compatibility properties ────────────────
    @property
    def _current(self) -> Optional[QueuedItem]:
        return next(iter(self._running.values()), None) if self._running else None

    @property
    def _current_async_task(self) -> Optional[asyncio.Task]:
        return next(iter(self._running_tasks.values()), None) if self._running_tasks else None

    # ── lifecycle ─────────────────────────────────────────
    def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())
            log.info(f"Queue worker started (max_concurrency={self.max_concurrency})")

    def stop(self):
        if self._worker and not self._worker.done():
            self._worker.cancel()
            log.info("Queue worker stopped")

    # ── public api ────────────────────────────────────────
    async def enqueue(
        self,
        kind: str,
        task_id: int,
        runner: Runner,
        front: bool = False,
        flow_account_email: Optional[str] = None,
    ) -> int:
        """Add a task to the queue. Returns its queue index (0 = next to run).

        `front=True` inserts at the HEAD of the waiting list so the task runs
        right after the currently-running one finishes — used by Resume so a
        paused task continues NEXT instead of being sent to the back."""
        clean_email = flow_account_email.strip().lower() if flow_account_email else None
        item = QueuedItem(task_id=task_id, kind=kind, flow_account_email=clean_email)
        if front:
            self._items.insert(0, item)
        else:
            self._items.append(item)
        self._runners[task_id] = runner
        self._signal.set()
        log.info(
            f"Enqueued task={task_id} kind={kind} flow_acc={clean_email} "
            f"front={front} (queue len={len(self._items)})"
        )
        await hub.broadcast("queue_updated", self.snapshot())
        return 0 if front else len(self._items) - 1

    async def cancel(self, task_id: int) -> bool:
        """Cancel a queued or running task.

        Returns True if the task was found and cancellation was triggered.
        """
        # Case 1: in queue, not yet started → just drop it
        before = len(self._items)
        self._items = [x for x in self._items if x.task_id != task_id]
        self._runners.pop(task_id, None)
        if len(self._items) < before:
            self._cancel_set.add(task_id)
            try:
                db.update_task(task_id, status=TaskStatus.CANCELLED.value)
            except Exception:
                pass
            log.info(f"Cancelled queued task={task_id} (not started yet)")
            await hub.broadcast("task_cancelled", {"task_id": task_id})
            await hub.broadcast("queue_updated", self.snapshot())
            return True

        # Case 2: currently running → cancel the asyncio.Task
        if task_id in self._running:
            self._cancel_set.add(task_id)
            async_task = self._running_tasks.get(task_id)
            if async_task and not async_task.done():
                async_task.cancel()
                log.info(f"Cancelling running task={task_id}")
            return True

        return False

    def position_of(self, task_id: int) -> int:
        """Return 0 if running, 1+ if queued, -1 if not found."""
        if task_id in self._running:
            return 0
        for idx, item in enumerate(self._items):
            if item.task_id == task_id:
                return idx + 1
        return -1

    def is_cancelled(self, task_id: int) -> bool:
        return task_id in self._cancel_set

    async def pause(self, task_id: int) -> str:
        """Pause a queued or running task so it can be resumed later (unlike
        cancel, which discards it). Returns:
          - "queued"  : was waiting in queue -> removed + marked PAUSED
          - "running" : currently running -> cancelled IMMEDIATELY + marked
                        PAUSED; in-flight items reset to PENDING for resume
          - "absent"  : not on this queue (caller decides)
        """
        before = len(self._items)
        self._items = [x for x in self._items if x.task_id != task_id]
        self._runners.pop(task_id, None)
        if len(self._items) < before:
            try:
                db.update_task(task_id, status=TaskStatus.PAUSED.value)
            except Exception:
                pass
            await hub.broadcast("task_paused", {"task_id": task_id})
            await hub.broadcast("queue_updated", self.snapshot())
            return "queued"

        if task_id in self._running:
            self._pause_set.add(task_id)
            async_task = self._running_tasks.get(task_id)
            if async_task and not async_task.done():
                async_task.cancel()
                log.info(f"Pausing running task={task_id} (cancel in-flight)")
            return "running"

        return "absent"

    def is_paused(self, task_id: int) -> bool:
        return task_id in self._pause_set

    async def mark_paused(self, task_id: int):
        """Called by a runner when it notices is_paused() and stops gracefully:
        record PAUSED in DB + clear the flag + notify the UI."""
        self._pause_set.discard(task_id)
        try:
            db.update_task(task_id, status=TaskStatus.PAUSED.value, finished_at=None)
        except Exception:
            pass
        await hub.broadcast("task_paused", {"task_id": task_id})
        await hub.broadcast("queue_updated", self.snapshot())

    def snapshot(self) -> dict:
        running_items = [asdict(x) for x in self._running.values()]
        return {
            "current": running_items[0] if running_items else None,
            "running": running_items,
            "queued": [asdict(x) for x in self._items],
        }

    # ── worker loop ──────────────────────────────────────
    async def _loop(self):
        log.info(f"Queue loop started (max_concurrency={self.max_concurrency})")
        while True:
            try:
                # 1. Purge cancelled items in queue
                while self._items and self._items[0].task_id in self._cancel_set:
                    item = self._items.pop(0)
                    self._runners.pop(item.task_id, None)
                    self._cancel_set.discard(item.task_id)
                    await hub.broadcast("queue_updated", self.snapshot())

                if not self._items:
                    self._signal.clear()
                    await self._signal.wait()
                    continue

                if len(self._running) >= self.max_concurrency:
                    self._signal.clear()
                    await self._signal.wait()
                    continue

                # 2. Find eligible task whose account is NOT already running
                active_accounts = {
                    item.flow_account_email.lower()
                    for item in self._running.values()
                    if item.flow_account_email
                }

                eligible_idx = None
                for idx, candidate in enumerate(self._items):
                    if candidate.task_id in self._cancel_set:
                        self._cancel_set.discard(candidate.task_id)
                        continue
                    if candidate.flow_account_email:
                        cand_acc = candidate.flow_account_email.lower()
                        if cand_acc in active_accounts:
                            # This account is busy with an in-flight task; wait for it
                            continue
                    eligible_idx = idx
                    break

                if eligible_idx is None:
                    # All remaining queued tasks are locked by currently running accounts
                    self._signal.clear()
                    await self._signal.wait()
                    continue

                item = self._items.pop(eligible_idx)
                runner = self._runners.pop(item.task_id, None)

                if runner is None:
                    log.warning(f"No runner for task={item.task_id} — skipping")
                    continue

                self._running[item.task_id] = item
                await hub.broadcast("queue_updated", self.snapshot())

                # Run task in background wrapper coroutine
                t = asyncio.create_task(self._run_task_item(item, runner))
                self._running_tasks[item.task_id] = t

            except asyncio.CancelledError:
                log.info("Queue loop cancelled")
                break
            except Exception as e:
                log.exception(f"Queue loop unexpected error: {e}")
                await asyncio.sleep(1)

    async def _run_task_item(self, item: QueuedItem, runner: Runner):
        task_id = item.task_id
        try:
            await runner(task_id)
        except asyncio.CancelledError:
            if task_id in self._pause_set:
                log.info(f"Task {task_id} paused mid-run")
                self._pause_set.discard(task_id)
                try:
                    from .config import ItemStatus
                    _redo = {
                        ItemStatus.GENERATING.value, ItemStatus.UPLOADING.value,
                        ItemStatus.DOWNLOADING.value,
                    }
                    for _it in db.get_task_items(task_id):
                        if _it["status"] in _redo:
                            db.update_item(_it["id"], status=ItemStatus.PENDING.value, error_message=None)
                    db.update_task(task_id, status=TaskStatus.PAUSED.value, finished_at=None)
                except Exception:
                    pass
                await hub.broadcast("task_paused", {"task_id": task_id})
            else:
                log.info(f"Task {task_id} was cancelled mid-run")
                try:
                    db.update_task(task_id, status=TaskStatus.CANCELLED.value)
                except Exception:
                    pass
                await hub.broadcast("task_cancelled", {"task_id": task_id})
        except Exception as e:
            log.exception(f"Queue runner crashed for task {task_id}: {e}")
        finally:
            self._cancel_set.discard(task_id)
            self._pause_set.discard(task_id)
            self._running.pop(task_id, None)
            self._running_tasks.pop(task_id, None)
            await hub.broadcast("queue_updated", self.snapshot())
            # Wake up the queue loop to pick next eligible queued task
            self._signal.set()


queue = TaskQueue(max_concurrency=2)

# Shakker runs on its OWN independent queue + worker so a Shakker batch and
# a Flow (image/video/long-video) task execute CONCURRENTLY.
shakker_queue = TaskQueue(max_concurrency=1)
