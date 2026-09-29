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
    target_account_email: Optional[str] = field(default=None, compare=False)
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
        # Pending = list of tasks ordered by sort_key. Protected by _pending_lock.
        # Allows matching tasks by target_account_email when multiple extension profiles poll.
        self._pending: list[_BridgeTask] = []
        self._pending_lock = asyncio.Lock()
        # In-flight = claimed by extension, awaiting result.
        self._in_flight: dict[str, _BridgeTask] = {}
        # Extension liveness tracking
        self._ext_last_poll: float = 0.0
        self._ext_last_ready_poll: float = 0.0
        self._ext_last_status: str = "unknown"  # "ready" | "no_tab" | "no_login" | "unknown"
        self._ext_last_url: str = ""
        self._ext_last_email: str = ""
        self._ext_last_tier: str = "FREE"
        self._ext_last_credits: Optional[int] = None
        # Multi-account extension tracking: email -> {last_seen, status, url, tier, credits}
        self._connected_accounts: dict[str, dict] = {}
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
        clean_email = email.strip().lower() if email else ""

        if status == "ready":
            self._ext_last_ready_poll = now
            self._ext_last_status = "ready"
            if url:
                self._ext_last_url = url
            if clean_email:
                self._ext_last_email = clean_email
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

        if clean_email:
            self._connected_accounts[clean_email] = {
                "last_seen": now,
                "status": status,
                "url": url,
                "tier": (tier or "FREE").strip().upper(),
                "credits": credits,
            }

        # Prune accounts not seen in > 120s
        expired = [em for em, d in self._connected_accounts.items() if (now - d["last_seen"]) > 120.0]
        for em in expired:
            self._connected_accounts.pop(em, None)

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

    def is_ready_extension_live(self, account_email: Optional[str] = None) -> bool:
        """True if an instance WITH a signed-in labs.google tab polled recently.
        If account_email is specified, checks if the extension for THAT account is live."""
        now = time.time()
        if account_email:
            clean = account_email.strip().lower()
            data = self._connected_accounts.get(clean)
            if data and (now - data["last_seen"]) < EXT_LIVE_THRESHOLD_S and data["status"] == "ready":
                return True
            return False
        return (now - self._ext_last_ready_poll) < EXT_LIVE_THRESHOLD_S

    def get_connected_flow_accounts(self) -> list[dict]:
        """Return all currently active Flow accounts reported by extensions across profiles."""
        now = time.time()
        res = []
        for em, data in self._connected_accounts.items():
            if (now - data["last_seen"]) < EXT_LIVE_THRESHOLD_S and data["status"] == "ready":
                res.append({
                    "email": em,
                    "tier": data.get("tier", "FREE"),
                    "credits": data.get("credits"),
                    "url": data.get("url", ""),
                })
        return res

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
            "connected_accounts": self.get_connected_flow_accounts(),
            "pending_tasks": len(self._pending),
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
                                     tab_status: str = "ready",
                                     tab_email: str = "") -> Optional[_BridgeTask]:
        """Extension polls; we hand it the next pending task matching its account, or None.

        `timeout=0.0` returns immediately if queue is empty (matches the
        extension's short-poll model).

        CAPABILITY GATE: only an instance reporting `tab_status == "ready"`
        (a signed-in labs.google tab) may claim a task.
        ACCOUNT ROUTING: if task.target_account_email is set, ONLY an extension
        reporting the same tab_email can claim this task.
        """
        if tab_status and tab_status != "ready":
            return None

        clean_tab_email = tab_email.strip().lower() if tab_email else ""

        async with self._pending_lock:
            # Purge expired tasks
            self._pending = [t for t in self._pending if not t.is_expired()]

            chosen_idx = None
            for idx, task in enumerate(self._pending):
                target = (task.target_account_email or "").strip().lower()
                if not target:
                    # Generic task: any ready extension can take it
                    chosen_idx = idx
                    break
                if clean_tab_email and target == clean_tab_email:
                    # Account match: this extension belongs to the target Flow account
                    chosen_idx = idx
                    break

            if chosen_idx is None:
                return None

            task = self._pending.pop(chosen_idx)
            self._in_flight[task.id] = task
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

    async def _enqueue_and_wait(
        self,
        kind: str,
        payload: dict,
        target_account_email: Optional[str] = None,
    ) -> dict:
        """Common enqueue+await flow shared by all task kinds.

        Fails fast with BridgeExtensionOfflineError if the extension
        hasn't polled recently — saves the caller waiting TTL_S for a
        task that no one will pick up.
        """
        clean_target = target_account_email.strip().lower() if target_account_email else None

        if not self.is_extension_live():
            raise BridgeExtensionOfflineError(
                "Extension chưa kết nối. Mở Chrome có cài 'RedOne Auth Helper' "
                "+ tab flow.google.com đã đăng nhập."
            )
        if not self.is_ready_extension_live(clean_target) and not self._in_flight:
            target_desc = f" cho tài khoản {clean_target}" if clean_target else ""
            raise BridgeExtensionOfflineError(
                f"Đã thấy extension nhưng CHƯA có tab Flow{target_desc} đã đăng nhập. "
                "Trong Chrome (profile tương ứng có 'RedOne Auth Helper'): mở tab "
                "https://flow.google.com, đăng nhập, ghim tab, rồi gen lại."
            )
        klass, seq = _gen_priority.get()
        enq = next(_enq_counter)
        task = _BridgeTask(
            sort_key=(klass, seq, enq),
            id=uuid.uuid4().hex,
            kind=kind,
            payload=payload,
            target_account_email=clean_target,
        )

        async with self._pending_lock:
            self._pending.append(task)
            self._pending.sort(key=lambda t: t.sort_key)

        queue_deadline = time.time() + QUEUE_TTL_S
        try:
            while not task.claimed.is_set():
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
                        f"({len(self._pending)} task còn xếp hàng)"
                    )
                try:
                    await asyncio.wait_for(task.claimed.wait(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    pass

            # Phase 2 — claimed, so now it gets the full execution budget.
            return await asyncio.wait_for(task.future, timeout=TASK_TTL_S)
        except asyncio.TimeoutError:
            self._in_flight.pop(task.id, None)
            raise BridgeTimeoutError(
                f"Extension đã nhận task {kind} nhưng không trả kết quả "
                f"sau {TASK_TTL_S}s"
            )
        finally:
            if not task.claimed.is_set():
                async with self._pending_lock:
                    self._pending = [t for t in self._pending if t.id != task.id]

    async def harvest_recaptcha(
        self,
        site_key: str = "",
        action: str = "VIDEO_GENERATION",
        account_email: Optional[str] = None,
    ) -> str:
        """Ask the extension to harvest a fresh reCAPTCHA token from a
        labs.google tab. site_key="" → extension auto-discovers it.

        Returns the token string. Raises if extension errors out.
        """
        result = await self._enqueue_and_wait(
            "recaptcha",
            {"site_key": site_key, "action": action},
            target_account_email=account_email,
        )
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
        account_email: Optional[str] = None,
    ) -> dict:
        """Run a fetch from inside the user's labs.google tab. The
        request carries the user's session cookies automatically
        (`credentials: "include"` on the JS side)."""
        payload = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "body": body,
            "response_mode": response_mode,
            "timeout_ms": timeout_ms,
        }
        return await self._enqueue_and_wait(
            "proxy_fetch",
            payload,
            target_account_email=account_email,
        )

    async def get_cookies(
        self,
        domains: list[str],
        account_email: Optional[str] = None,
    ) -> dict:
        """Ask the extension to read the user's Chrome cookies for `domains`."""
        return await self._enqueue_and_wait(
            "get_cookies",
            {"domains": domains},
            target_account_email=account_email,
        )

    async def batch_execute(
        self,
        rpc_id: str,
        inner_payload: Any,
        source_path: str = "/",
        timeout_ms: int = 120000,
        recaptcha_action: str = "",
        account_email: Optional[str] = None,
    ) -> dict:
        """Execute a BOQ/WIZ batchexecute RPC from inside the user's flow.google.com tab."""
        payload = {
            "rpc_id": rpc_id,
            "inner_payload": inner_payload,
            "source_path": source_path,
            "timeout_ms": timeout_ms,
        }
        if recaptcha_action:
            payload["recaptcha_action"] = recaptcha_action
        return await self._enqueue_and_wait(
            "batch_execute",
            payload,
            target_account_email=account_email,
        )

    async def init_flow_project(
        self,
        target_project_id: str = "",
        force_new: bool = False,
        timeout_ms: int = 25000,
        account_email: Optional[str] = None,
    ) -> dict:
        """Ask extension to verify or navigate flow.google.com tab to a project."""
        payload: dict[str, Any] = {"force_new": force_new}
        if target_project_id:
            payload["target_project_id"] = target_project_id
        return await self._enqueue_and_wait(
            "init_flow_project",
            payload,
            target_account_email=account_email,
        )

    async def proxy_fetch_binary(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout_ms: int = 120000,
        account_email: Optional[str] = None,
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
            account_email=account_email,
        )
        if result.get("error"):
            raise RuntimeError(f"proxy_fetch_binary {url}: {result['error']}")
        b64 = result.get("body_b64", "")
        raw = base64.b64decode(b64) if b64 else b""
        return result.get("status", 0), raw, result.get("headers", {})


# Module-level singleton — imported by routers/sync.py and FlowClient.
bridge = BrowserBridge()

