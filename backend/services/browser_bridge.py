"""BrowserBridge — backend ↔ Chrome extension task broker.

Replaces the Playwright-based call path that fired requests against
Google's API directly. Instead, every Google-bound request becomes a
*task* enqueued here, picked up by the Chrome extension, executed inside
the user's real Chrome (so it carries the user's real session cookies
and the user's real browser fingerprint), and the result is delivered
back to the awaiting coroutine.

Public API (sync to use from routers / FlowClient):

    await bridge.harvest_recaptcha(site_key, action) -> str (token)
    await bridge.proxy_fetch(url, method, headers, body) -> dict
    await bridge.proxy_fetch_binary(url, ...) -> bytes

Internals:
    - Each task gets a UUID + asyncio.Future. Extension polls via
      `bridge.pop_task_for_extension()`, runs it, then submits the result
      via `bridge.deliver_result(task_id, result)` which resolves the
      Future.
    - Two separate budgets so a queue backlog can't kill a task that
      never ran: QUEUE_TTL_S while waiting to be claimed, then a fresh
      TASK_TTL_S to actually run. Either way the awaiter gets a
      `BridgeTimeoutError` (with a message saying which one blew).
    - Bridge tracks extension liveness (last seen tab + status) so
      diagnostics endpoints can show "Extension offline" hints.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("redone.bridge")

# Execution budget: how long the extension may take to RUN a task once it has
# CLAIMED it. The clock starts at claim, NOT at enqueue. The extension runs
# tasks strictly one at a time (it can't poll while busy), so with N parallel
# gens the last download legitimately queues for minutes. Charging that queue
# wait against this budget killed tasks that had never even started — the
# "Extension không phản hồi task proxy_fetch sau 120.0s" seen on 5-stream runs.
TASK_TTL_S = 120.0

# Queue budget: how long a task may sit UNCLAIMED before we give up. Generous,
# because serialized downloads are normal backpressure, not a fault. The wait
# aborts early if the extension stops heartbeating, so a closed Chrome doesn't
# park the caller here for the full window.
QUEUE_TTL_S = 300.0

# Heartbeat freshness threshold. If the extension hasn't pinged within
# this window we consider it disconnected (used by snapshot_state).
#
# 60s is intentional: while the ext is busy running a SINGLE proxy_fetch
# task (large file upload can take 5-15s) or grecaptcha.execute (2-3s),
# it can't simultaneously poll /sync/next-task. With a 10s threshold we'd
# mark the ext "offline" mid-task, which then caused
# BridgeExtensionOfflineError on the NEXT call even though the ext is
# alive and well — just busy. 60s gives plenty of slack.
EXT_LIVE_THRESHOLD_S = 60.0


# ── Bridge dispatch priority ────────────────────────────────────────
# Every queued bridge task carries a sort_key = (klass, seq, enq); the
# PriorityQueue pops the SMALLEST first. Lower klass = runs earlier.
#
#   klass 0 = regen  (user clicked "Gen lại" — jump the line)
#   klass 1 = upscale 2K/4K
#   klass 2 = gen thường (normal image/video generation)
#   klass 3 = misc / standalone (default — e.g. credit checks)
#
# Priority is threaded into the gen call stack via a ContextVar (no
# function-signature changes). A runner/endpoint calls set_gen_priority()
# at its top; every proxy_fetch it (and its gathered coroutines) issue
# inherits that class. `seq` orders calls within the same class by which
# runner started first; `enq` is a global put-counter giving FIFO tiebreak
# AND guaranteeing the 3-tuple is unique so PriorityQueue never compares
# down to the _BridgeTask body.
#
# NOTE: priority only reorders the WAITING queue. In-flight fetches are
# never preempted — a regen waits for one of the running slots to free,
# then jumps ahead of everything still queued.
_seq_counter = itertools.count()
_enq_counter = itertools.count()
_gen_priority: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "redone_gen_priority", default=(3, 0)
)


def next_gen_seq() -> int:
    """Allocate the next monotonic sequence number for a gen runner."""
    return next(_seq_counter)


def set_gen_priority(klass: int, seq: int) -> None:
    """Set the bridge dispatch priority for the current context.

    Call at the top of a runner/endpoint body. All proxy_fetch calls made
    within this context (including coroutines scheduled with the copied
    context, e.g. via asyncio.gather) inherit (klass, seq)."""
    _gen_priority.set((klass, seq))


class BridgeTimeoutError(Exception):
    """Raised when a task times out waiting for the extension."""


class BridgeExtensionOfflineError(Exception):
    """Raised when we try to issue a task but the extension hasn't
    polled recently — fail fast rather than wait the full TTL."""


@dataclass(order=True)
class _BridgeTask:
    """One outstanding task waiting on the extension.

    The `future` resolves with the result dict once the extension
    posts back via /sync/task-result. `claimed` is set the moment an
    extension pops the task off the queue — that's what starts the
    TASK_TTL_S execution clock in `_enqueue_and_wait`. Until then the task
    is only spending its QUEUE_TTL_S budget.

    `sort_key` (klass, seq, enq) is the ONLY comparison field — it drives
    the PriorityQueue ordering. Every other field has compare=False so the
    queue never tries to order two tasks by their dict/future bodies (which
    would raise TypeError). The 3-tuple is always unique (enq is a global
    counter) so ties never fall through to a body comparison.
    """
    sort_key: tuple = field(compare=True)
    id: str = field(compare=False)
    kind: str = field(compare=False)  # "recaptcha" | "proxy_fetch"
    payload: dict = field(compare=False)
    created_at: float = field(default_factory=time.time, compare=False)
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_event_loop().create_future(),
        compare=False,
    )
    claimed: asyncio.Event = field(default_factory=asyncio.Event, compare=False)

    def is_expired(self) -> bool:
        """Only ever consulted for tasks still sitting in `_pending` (i.e.
        never claimed), so this is the QUEUE budget, not the execution one."""
        return (time.time() - self.created_at) > QUEUE_TTL_S

    def to_dict(self) -> dict:
        """Wire format sent to the extension."""
        return {"id": self.id, "kind": self.kind, "payload": self.payload}


class BrowserBridge:
    """Singleton-style broker. Routers / FlowClient call its async
    methods; the extension polls / posts via routers/sync.py.
    """

    def __init__(self):
        # Pending = waiting for extension to claim. PriorityQueue pops the
        # task with the smallest sort_key first (regen < upscale < gen <
        # misc); ties break FIFO via the per-put `enq` counter.
        self._pending: asyncio.PriorityQueue[_BridgeTask] = asyncio.PriorityQueue()
        # In-flight = claimed by extension, awaiting result.
        self._in_flight: dict[str, _BridgeTask] = {}
        # Extension liveness tracking
        self._ext_last_poll: float = 0.0
        # Last time ANY instance reported a signed-in labs.google tab. With the
        # extension installed in several Chrome profiles (all polling the same
        # localhost backend), only the profile holding the tab reports "ready".
        self._ext_last_ready_poll: float = 0.0
        self._ext_last_status: str = "unknown"  # "ready" | "no_tab" | "no_login" | "unknown"
        self._ext_last_url: str = ""
        self._ext_last_email: str = ""
        self._ext_last_tier: str = "FREE"
        self._ext_last_credits: Optional[int] = None
        # Server-driven session commands — queued by backend, consumed by
        # extension on next poll. Commands: clear_cookies, reload_tab,
        # navigate_toggle, delay.
        self._session_commands: list[dict] = []

    # ── State / diagnostics ─────────────────────────────────────────

    def update_tab_state(
        self,
        status: str,
        url: str,
        email: str = "",
        tier: str = "",
        credits: Optional[int] = None,
    ) -> None:
        """Called by /sync/next-task. Updates our view of what the
        extension currently can/can't do. Prevents flapping when multiple
        Chrome profiles poll concurrently."""
        now = time.time()
        self._ext_last_poll = now
        if status == "ready":
            self._ext_last_ready_poll = now
            self._ext_last_status = "ready"
            if url:
                self._ext_last_url = url
            if email:
                self._ext_last_email = email.strip().lower()
            if tier:
                self._ext_last_tier = tier.strip().upper()
            if credits is not None:
                self._ext_last_credits = credits
        else:
            # Only downgrade status if we haven't seen a ready tab in 10s
            last_ready_age = now - getattr(self, "_ext_last_ready_poll", 0.0)
            if last_ready_age > 10.0:
                self._ext_last_status = status or "unknown"
                if url:
                    self._ext_last_url = url
                self._ext_last_email = None
                self._ext_last_tier = None
                self._ext_last_credits = None

    def get_active_account_email(self) -> Optional[str]:
        """Return the Google account email detected from the active Flow tab, if any."""
        if self._ext_last_status != "ready" and (time.time() - getattr(self, "_ext_last_ready_poll", 0.0) > 10.0):
            return None
        return self._ext_last_email if self._ext_last_email else None

    def get_active_account_tier(self) -> str:
        """Return the subscription tier detected from the active Flow tab ('ULTRA', 'PRO', 'FREE')."""
        if self._ext_last_status != "ready" and (time.time() - getattr(self, "_ext_last_ready_poll", 0.0) > 10.0):
            return "FREE"
        return self._ext_last_tier or "FREE"

    def get_active_account_credits(self) -> Optional[int]:
        """Return the remaining credits detected from the active Flow tab, if any."""
        if self._ext_last_status != "ready" and (time.time() - getattr(self, "_ext_last_ready_poll", 0.0) > 10.0):
            return None
        return self._ext_last_credits

    def get_active_project_id(self) -> Optional[str]:
        """Extract project ID from the last reported tab URL if available."""
        if not self._ext_last_url:
            return None
        import re
        m = re.search(r"/project/([a-zA-Z0-9_-]{36})", self._ext_last_url)
        return m.group(1) if m else None

    def bump_liveness(self) -> None:
        """Refresh the 'extension is alive' timestamp WITHOUT touching
        tab status. Called by /sync/status — that endpoint pings while
        the ext might be busy in a proxy_fetch task and can't poll
        /sync/next-task for several seconds. Keeps is_extension_live()
        True throughout long-running tasks."""
        self._ext_last_poll = time.time()

    def get_user_agent(self) -> str:
        """Return the User-Agent reported by the active Chrome extension."""
        return getattr(self, "_cached_ua", "") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
        )

    def is_extension_live(self) -> bool:
        return (time.time() - self._ext_last_poll) < EXT_LIVE_THRESHOLD_S

    def is_ready_extension_live(self) -> bool:
        """True if an instance WITH a signed-in labs.google tab polled recently.
        Distinguishes 'an extension is connected' from 'an extension that can
        actually run Flow tasks is connected' — needed when the extension is
        loaded in multiple profiles and only one holds the labs.google tab."""
        return (time.time() - self._ext_last_ready_poll) < EXT_LIVE_THRESHOLD_S

    def snapshot_state(self) -> dict:
        return {
            "extension_live": self.is_extension_live(),
            "last_poll_age_s": round(time.time() - self._ext_last_poll, 1)
                if self._ext_last_poll else None,
            "last_tab_status": self._ext_last_status,
            "last_tab_url": self._ext_last_url,
            "last_tab_email": self._ext_last_email,
            "last_tab_tier": self._ext_last_tier,
            "last_tab_credits": self._ext_last_credits,
            "pending_tasks": self._pending.qsize(),
            "in_flight_tasks": len(self._in_flight),
            "pending_session_commands": len(self._session_commands),
        }

    # ── Server-driven session commands ──────────────────────────────

    def push_session_command(self, command: str, params: dict | None = None) -> None:
        """Queue a session command for the extension to execute on next poll.

        Commands (inspired by G-Labs _applyThemeUpdates):
            - "clear_cookies": Clear all labs.google cookies → force re-login
            - "reload_tab": F5 reload the labs.google tab
            - "navigate_toggle": Toggle /tools/flow ↔ /fx (resets page state)
            - "delay": Wait params["ms"] milliseconds before next command
        """
        self._session_commands.append({
            "cmd": command,
            "params": params or {},
            "ts": time.time(),
        })
        log.info(f"Session command queued: {command} (params={params})")

    def pop_session_commands(self) -> list[dict]:
        """Return and clear all pending session commands."""
        if not self._session_commands:
            return []
        cmds = self._session_commands[:]
        self._session_commands.clear()
        return cmds

    # ── Extension-facing (called from routers/sync.py) ──────────────

    async def pop_task_for_extension(self, timeout: float = 0.0,
                                     tab_status: str = "ready") -> Optional[_BridgeTask]:
        """Extension polls; we hand it the next pending task, or None.

        `timeout=0.0` returns immediately if queue is empty (matches the
        extension's short-poll model).

        CAPABILITY GATE: only an instance reporting `tab_status == "ready"`
        (a signed-in labs.google tab) may claim a task. The extension can be
        installed in MULTIPLE Chrome profiles — every profile's service worker
        polls this SAME localhost backend. A profile without a labs.google tab
        reports "no_tab"/"no_login"; if it claimed a task it would fail
        mid-flight with "no labs.google tab". Returning None leaves the task
        queued for the profile that actually holds the tab. This is what lets
        the extension live in several profiles without errors.
        """
        if tab_status and tab_status != "ready":
            return None
        try:
            if timeout > 0:
                task = await asyncio.wait_for(self._pending.get(), timeout=timeout)
            else:
                task = self._pending.get_nowait()
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None
        # Skip expired tasks (shouldn't happen because awaiters drop them
        # via cancellation, but defensive).
        while task.is_expired():
            try:
                task = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                return None
        self._in_flight[task.id] = task
        # Starts the execution clock for the awaiter in _enqueue_and_wait.
        task.claimed.set()
        return task

    def deliver_result(self, task_id: str, result: dict) -> bool:
        """Resolve the Future for the task. Returns True if delivered,
        False if the task no longer exists (expired / cancelled)."""
        task = self._in_flight.pop(task_id, None)
        if task is None:
            return False
        if not task.future.done():
            task.future.set_result(result)
        return True

    # ── Public API (called from FlowClient / routers) ───────────────

    async def _enqueue_and_wait(self, kind: str, payload: dict) -> dict:
        """Common enqueue+await flow shared by all task kinds.

        Fails fast with BridgeExtensionOfflineError if the extension
        hasn't polled recently — saves the caller waiting TTL_S for a
        task that no one will pick up.
        """
        if not self.is_extension_live():
            raise BridgeExtensionOfflineError(
                "Extension chưa kết nối. Mở Chrome có cài 'RedOne Auth Helper' "
                "+ tab flow.google.com đã đăng nhập."
            )
        # An extension is polling, but does any instance actually hold a
        # signed-in flow.google.com tab? If not — and nothing is mid-flight (which
        # would mean a ready instance is just busy) — fail fast with a clear
        # message instead of letting the task sit unclaimed until TASK_TTL_S.
        if not self.is_ready_extension_live() and not self._in_flight:
            raise BridgeExtensionOfflineError(
                "Đã thấy extension nhưng CHƯA có tab Flow đã đăng nhập. "
                "Trong Chrome (profile có 'RedOne Auth Helper'): mở tab "
                "https://flow.google.com, đăng nhập, ghim tab, rồi gen lại."
            )
        klass, seq = _gen_priority.get()
        enq = next(_enq_counter)
        task = _BridgeTask(
            sort_key=(klass, seq, enq),
            id=uuid.uuid4().hex,
            kind=kind,
            payload=payload,
        )
        await self._pending.put(task)

        # Phase 1 — wait to be CLAIMED. The extension runs one task at a time,
        # so queueing behind other downloads is normal and must NOT eat the
        # execution budget. Poll in slices so a Chrome that closes mid-queue
        # fails fast instead of parking the caller for the whole window.
        queue_deadline = time.time() + QUEUE_TTL_S
        while not task.claimed.is_set():
            # A busy extension can't poll (see EXT_LIVE_THRESHOLD_S), so a
            # stale heartbeat only means "dead" when nothing is in flight.
            if not self.is_extension_live() and not self._in_flight:
                raise BridgeExtensionOfflineError(
                    f"Extension ngắt kết nối khi task {kind} còn đang xếp hàng. "
                    "Mở lại Chrome có 'RedOne Auth Helper' + tab flow.google.com "
                    "đã đăng nhập, rồi thử lại."
                )
            remaining = queue_deadline - time.time()
            if remaining <= 0:
                raise BridgeTimeoutError(
                    f"Không extension nào nhận task {kind} sau {QUEUE_TTL_S}s "
                    f"({self._pending.qsize()} task còn xếp hàng)"
                )
            try:
                await asyncio.wait_for(task.claimed.wait(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass

        # Phase 2 — claimed, so now it gets the full execution budget.
        try:
            return await asyncio.wait_for(task.future, timeout=TASK_TTL_S)
        except asyncio.TimeoutError:
            # Drop from in_flight if it got claimed but never returned
            self._in_flight.pop(task.id, None)
            raise BridgeTimeoutError(
                f"Extension đã nhận task {kind} nhưng không trả kết quả "
                f"sau {TASK_TTL_S}s"
            )

    async def harvest_recaptcha(
        self,
        site_key: str = "",
        action: str = "VIDEO_GENERATION",
    ) -> str:
        """Ask the extension to harvest a fresh reCAPTCHA token from a
        labs.google tab. site_key="" → extension auto-discovers it.

        Returns the token string. Raises if extension errors out.
        """
        result = await self._enqueue_and_wait("recaptcha", {
            "site_key": site_key,
            "action": action,
        })
        token = result.get("token")
        if not token:
            err = result.get("error") or "unknown error"
            raise RuntimeError(f"reCAPTCHA harvest failed: {err}")
        return token

    async def proxy_fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        response_mode: str = "json",   # "json" | "text" | "arraybuffer"
        timeout_ms: int = 60000,
    ) -> dict:
        """Run a fetch from inside the user's labs.google tab. The
        request carries the user's session cookies automatically
        (`credentials: "include"` on the JS side).

        Returns a dict:
            {
              "status": int,
              "headers": dict,
              "body": <parsed JSON or text>,
              # If response_mode="arraybuffer": "body_b64" instead of "body"
              "error": str | None,
            }
        """
        payload = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "response_mode": response_mode,
            "timeout_ms": timeout_ms,
        }
        result = await self._enqueue_and_wait("proxy_fetch", payload)
        return result

    async def get_cookies(self, domains: list[str]) -> dict:
        """Ask the extension to read the user's Chrome cookies for `domains`
        (via the chrome.cookies API → already decrypted, no DB-copy problem).
        Used to auto-feed yt-dlp YouTube auth. Returns
        {"cookies": [{domain,name,value,path,secure,hostOnly,expirationDate}], "count": N}.
        """
        return await self._enqueue_and_wait("get_cookies", {"domains": domains})

    async def batch_execute(
        self,
        rpc_id: str,
        inner_payload: Any,
        source_path: str = "/",
        timeout_ms: int = 120000,
        recaptcha_action: str = "",
    ) -> dict:
        """Execute a BOQ/WIZ batchexecute RPC from inside the user's
        flow.google.com tab.

        Google migrated Flow's API from aisandbox-pa REST (Bearer token)
        to batchexecute RPC (cookie auth + CSRF token). This method sends
        the RPC through the extension, which injects it into the tab
        where the browser automatically attaches Google auth cookies.

        When ``recaptcha_action`` is set (e.g. "IMAGE_GENERATION"),
        the extension mints the reCAPTCHA token **inline** inside the
        same ``executeScript`` call that sends the request — keeping
        token + fetch in one execution context, which prevents Google's
        UNUSUAL_ACTIVITY detection.

        Args:
            rpc_id: The WIZ RPC ID (e.g. "ogiZ0b" for image gen)
            inner_payload: The inner payload (Python value, will be JSON-serialized)
            source_path: The source-path URL param (e.g. "/project/<uuid>")
            timeout_ms: Timeout for the fetch call inside the tab
            recaptcha_action: reCAPTCHA action to mint inline (empty = skip)

        Returns:
            dict with keys: status, rpc_result, chunks, error
        """
        payload = {
            "rpc_id": rpc_id,
            "inner_payload": inner_payload,
            "source_path": source_path,
            "timeout_ms": timeout_ms,
        }
        if recaptcha_action:
            payload["recaptcha_action"] = recaptcha_action
        return await self._enqueue_and_wait("batch_execute", payload)

    async def init_flow_project(
        self,
        target_project_id: str = "",
        force_new: bool = False,
        timeout_ms: int = 25000,
    ) -> dict:
        """Ask extension to verify or navigate flow.google.com tab to a project."""
        payload: dict[str, Any] = {"force_new": force_new}
        if target_project_id:
            payload["target_project_id"] = target_project_id
        return await self._enqueue_and_wait("init_flow_project", payload)

    async def proxy_fetch_binary(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout_ms: int = 120000,
    ) -> tuple[int, bytes, dict]:
        """Convenience wrapper for binary downloads (video/image bytes).
        Returns (status, raw_bytes, headers).
        """
        result = await self.proxy_fetch(
            url=url,
            method=method,
            headers=headers,
            body=body,
            response_mode="arraybuffer",
            timeout_ms=timeout_ms,
        )
        if result.get("error"):
            raise RuntimeError(f"proxy_fetch_binary {url}: {result['error']}")
        b64 = result.get("body_b64", "")
        raw = base64.b64decode(b64) if b64 else b""
        return result.get("status", 0), raw, result.get("headers", {})


# Module-level singleton — imported by routers/sync.py and FlowClient.
bridge = BrowserBridge()
