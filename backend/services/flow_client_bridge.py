"""BridgeFlowClient — FlowClient that routes ALL Google calls through
the Chrome extension bridge instead of Playwright.

Why
===
A request fired by Playwright is "from a freshly-spun, never-used-before
Chromium under automation control" — Google's bot detector picks up on
this and starts handing out 403 on reCAPTCHA tokens. Once flagged, the
same browser context gets 403 for ~10-15 minutes.

A request fired from inside the user's REAL Chrome (via the extension's
`chrome.scripting.executeScript({world: 'MAIN'})` + `fetch(url, {credentials: 'include'})`)
carries:
  - the user's real session cookies
  - the user's real browser fingerprint
  - tab history Google has seen interactively for hours

→ Google treats the call as "user did something" not "bot ran a script".
403s drop to near-zero (proven in production).

Architecture
============
BridgeFlowClient subclasses FlowClient and overrides ONLY the methods
that touch Playwright internals (`self._page.evaluate(...)`,
`self._page.request.post(...)`, etc.). The high-level methods
(`generate_video`, `upload_image`, `wait_for_completion`, etc.) stay
inherited unchanged because they build their payloads + call
`_browser_sandbox_request()` — which we now route through the bridge.

This keeps the public API 100% identical to FlowClient so routers
(content.py, image.py, long_video.py) don't need to know which client
they got.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random as _rand
import re
from pathlib import Path
from typing import Optional

from .flow_client import (
    FlowClient,
    SessionDeadError,
    _shrink_image_for_upload,
)
from ..config import (
    AISANDBOX_BASE,
    X_CLIENT_DATA,
    X_BROWSER_VALIDATION,
    MAX_RETRY_COUNT,
)
from .browser_bridge import bridge, BridgeExtensionOfflineError, BridgeTimeoutError

log = logging.getLogger("redone.flow_bridge")


def _map_flow_tier(paygate: Optional[str], service: Optional[str],
                   sku: Optional[str]) -> str:
    """Map Google Flow /v1/credits tier fields → friendly label.

    `userPaygateTier` is the clearest ladder: TIER_TWO = Google AI Ultra,
    TIER_ONE = Google AI Pro, else (TIER_ZERO/UNSPECIFIED/missing) = Free.
    Confirmed on a real ULTRA account (2026-06): userPaygateTier=
    PAYGATE_TIER_TWO, serviceTier=SERVICE_TIER_ADVANCED, sku=G1_TIER2.
    serviceTier/sku are only used as a fallback when paygate is absent.
    """
    pg = (paygate or "").upper()
    sv = (service or "").upper()
    sk = (sku or "").upper()
    if "TIER_TWO" in pg:
        return "ULTRA"
    if "TIER_ONE" in pg:
        return "PRO"
    if pg:                       # any other explicit paygate value = free
        return "FREE"
    # Fallback when userPaygateTier missing — infer from serviceTier / sku.
    if "ADVANCED" in sv or "TIER2" in sk:
        return "ULTRA"
    if "PRO" in sv or "TIER1" in sk:
        return "PRO"
    return "FREE"


class BridgeFlowClient(FlowClient):
    """FlowClient variant where every Google call goes through the
    Chrome extension bridge. See module docstring for rationale.

    Public API matches FlowClient exactly — drop-in replacement.
    """

    # Persistent verified project_id per account
    _ACTIVE_PROJECT_IDS: dict[str, str] = {}
    _FAILED_PROJECT_IDS: set[str] = set()

    def __init__(self, page=None, cookie_path: str = "", account_email: str = ""):
        # We pass page=None to the parent; everything that touches
        # self._page is overridden in this subclass.
        super().__init__(page=None, cookie_path=cookie_path, account_email=account_email)
        # Cache last-known user agent for use in headers (bridge fills it
        # in from the real Chrome on first proxy_fetch — until then we
        # use a sensible default).
        self._ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        )
        self.TRPC = "https://flow.google.com/fx/api/trpc"
        active_p = bridge.get_active_project_id()
        if active_p and active_p not in BridgeFlowClient._FAILED_PROJECT_IDS:
            BridgeFlowClient._ACTIVE_PROJECT_IDS[account_email] = active_p
            self.project_id = active_p
        else:
            cached_p = BridgeFlowClient._ACTIVE_PROJECT_IDS.get(account_email)
            if cached_p and cached_p not in BridgeFlowClient._FAILED_PROJECT_IDS:
                self.project_id = cached_p
        # NOTE: no per-account proxy here — in bridge mode every Google call
        # (including download_video/download_image) executes inside the user's
        # real Chrome tab, so the egress IP is Chrome's, not this process's.
        # Per-account proxying is impossible by construction: all accounts
        # share one browser.
        # Per-account error tracking (G-Labs #5)
        self._consecutive_errors: int = 0
        self._consecutive_403_count: int = 0
        self._last_request_status: str = "ok"
        self._total_errors: int = 0
        self._total_success: int = 0
        self._token_lock = asyncio.Lock()
        self._flow_session = None

    def _record_success(self) -> None:
        """Mark a successful API call."""
        self._consecutive_errors = 0
        self._consecutive_403_count = 0
        self._last_request_status = "ok"
        self._total_success += 1

    def _record_error(self, error_type: str = "unknown") -> None:
        """Mark a failed API call and trigger session commands if needed."""
        self._consecutive_errors += 1
        self._total_errors += 1
        self._last_request_status = error_type
        if "403" in error_type:
            self._consecutive_403_count += 1

    def get_health_stats(self) -> dict:
        """Return per-account health metrics."""
        return {
            "email": self._account_email,
            "consecutive_errors": self._consecutive_errors,
            "consecutive_403": self._consecutive_403_count,
            "last_status": self._last_request_status,
            "total_errors": self._total_errors,
            "total_success": self._total_success,
            "should_disable": self._consecutive_errors >= 10,
        }

    async def get_flow_session(self):
        """Ensure an authenticated FlowSession exists for this client."""
        if self._flow_session is None:
            from .flow_session import FlowSession
            ua = bridge.get_user_agent() or self._ua
            self._flow_session = FlowSession(
                account_email=self._account_email,
                user_agent=ua,
            )
        if not self._flow_session.cookie_str:
            cookie_res = await bridge.get_cookies([
                "flow.google.com",
                ".google.com",
                "accounts.google.com",
                ".labs.google",
            ])
            cookies = cookie_res.get("cookies", [])
            if cookies:
                self._flow_session.update_cookies_from_list(cookies)
        return self._flow_session

    async def _execute_batchexecute(
        self,
        rpc_id: str,
        inner_payload: Any,
        source_path: str = "/",
        timeout_ms: int = 120000,
        recaptcha_action: str = "",
    ) -> dict:
        """Execute batchexecute RPC.

        Architecture:
        - Generation RPCs (ogiZ0b, eb1hJf, YhhmEf, SPrCad, maseQ) require a fresh reCAPTCHA
          Enterprise token minted in Chrome and MUST be submitted natively from the Chrome
          tab via bridge.batch_execute so Chrome's TLS session, HTTP/2 connection, and
          x-client-data headers are preserved.

          When ``recaptcha_action`` is set, the extension mints the token **inline**
          inside the same executeScript call that sends the request, preventing
          Google Code 7 PUBLIC_ERROR_UNUSUAL_ACTIVITY caused by context mismatch.

        - Administrative RPCs (jHPbke, nzlxg, UpteDb, jwpduf) do not require reCAPTCHA and run
          instantaneously (~200ms) in Python FlowSession without interrupting tab focus.
        """
        # 1. For generation RPCs (ogiZ0b, eb1hJf, YhhmEf, SPrCad, maseQ) requiring reCAPTCHA:
        # Harvest fresh token from tab and inject directly into inner_payload (G-Labs Studio architecture)
        has_recaptcha_placeholder = isinstance(inner_payload, (list, dict, str)) and "__MINT_RECAPTCHA__" in json.dumps(inner_payload)
        if recaptcha_action or has_recaptcha_placeholder:
            action = recaptcha_action or "IMAGE_GENERATION"
            try:
                log.info(f"[{self._account_email}] Harvesting reCAPTCHA token via Chrome tab (action={action})...")
                rc_res = await bridge.harvest_recaptcha(
                    site_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                    action=action,
                )
                rc_token = rc_res.get("token") if isinstance(rc_res, dict) else str(rc_res)
                if rc_token:
                    inner_str = json.dumps(inner_payload).replace('"__MINT_RECAPTCHA__"', json.dumps(rc_token))
                    if "__MINT_RECAPTCHA__" in inner_str:
                        inner_str = inner_str.replace("__MINT_RECAPTCHA__", rc_token)
                    inner_payload = json.loads(inner_str)
                    log.info(f"[{self._account_email}] reCAPTCHA token injected into inner_payload (len={len(rc_token)})")
                else:
                    log.warning(f"[{self._account_email}] Empty reCAPTCHA token harvested")
            except Exception as rc_err:
                log.error(f"[{self._account_email}] Failed to harvest reCAPTCHA token: {rc_err}")
                return {"status": 0, "rpc_result": None, "error": f"reCAPTCHA harvest failed: {rc_err}", "chunks": []}

        # 2. Execute via Python FlowSession (G-Labs Studio direct HTTP POST)
        try:
            session = await self.get_flow_session()

            # Fast-track CSRF bootstrap from active tab WIZ_global_data if not yet bootstrapped
            if not session.at_token:
                try:
                    dom_res = await bridge.batch_execute("DEBUG_DOM", {})
                    r_dom = dom_res.get("rpc_result", {})
                    if r_dom.get("at"):
                        session.at_token = r_dom["at"]
                        session.build_label = r_dom.get("bl") or session.build_label
                        session.session_id = r_dom.get("fSid") or session.session_id
                        session.last_bootstrap_ts = asyncio.get_event_loop().time()
                        log.info(f"[{self._account_email}] Fast-populated FlowSession CSRF token from active tab: {session.at_token[:15]}...")
                except Exception as ex:
                    log.debug(f"Fast-populating CSRF token from tab failed: {ex}")

            res = await session.execute(
                rpc_id=rpc_id,
                inner_payload=inner_payload,
                source_path=source_path,
                timeout_ms=timeout_ms,
            )
            err = res.get("error")
            rpc_res = res.get("rpc_result")

            # Clean success
            if res.get("status") == 200 and not err and rpc_res is not None:
                return res

            # If session error (e.g. cookie expired / 401 / 400), try refreshing cookies once
            if res.get("status") in (400, 401):
                log.info(f"[{self._account_email}] Refreshing cookies from bridge for {rpc_id}...")
                cookie_res = await bridge.get_cookies([
                    "flow.google.com",
                    ".google.com",
                    "accounts.google.com",
                ])
                cookies = cookie_res.get("cookies", [])
                if cookies:
                    session.update_cookies_from_list(cookies)
                    await session.bootstrap(force=True)
                    res = await session.execute(
                        rpc_id=rpc_id,
                        inner_payload=inner_payload,
                        source_path=source_path,
                        timeout_ms=timeout_ms,
                    )
                    return res

            return res
        except Exception as e:
            log.error(f"[{self._account_email}] FlowSession execution failed for {rpc_id}: {e}")
            return {"status": 0, "rpc_result": None, "error": str(e), "chunks": []}

    # ── Auth / token ────────────────────────────────────────────────

    async def _do_get_token(self):
        """Fetch `ya29.*` Bearer token via the extension.

        The extension's background service worker fetches
        labs.google/fx/api/auth/session with manually-built Cookie header
        (from chrome.cookies API), so this works regardless of whether
        the user's tab is on labs.google or flow.google.com.

        A `status == 0` reply means the extension couldn't even run the fetch —
        almost always because the extension service worker had issues.
        That's TRANSIENT, so we retry a few times before declaring the
        session dead.
        """
        log.info(f"[{self._account_email}] (bridge) fetching NextAuth session...")
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                r = await bridge.proxy_fetch(
                    url="https://labs.google/fx/api/auth/session",
                    method="GET",
                    headers={"Accept": "application/json"},
                    response_mode="json",
                    timeout_ms=15000,
                )
            except BridgeExtensionOfflineError as e:
                raise SessionDeadError(self._account_email, str(e))

            status = r.get("status", 0)
            body = r.get("body")
            err = r.get("error")

            log.info(
                f"[{self._account_email}] (bridge) auth/session response "
                f"(attempt {attempt}/{attempts}): status={status} error={err!r} "
                f"body_type={type(body).__name__} body_preview={str(body)[:200]!r}"
            )

            if status == 200 and isinstance(body, dict) and body.get("access_token"):
                self._token = body["access_token"]
                log.info(f"[{self._account_email}] (bridge) got token ya29...{self._token[-8:]}")
                return

            # status=0 → extension couldn't fetch (labs.google tab momentarily
            # missing/discarded/navigating). TRANSIENT → wait + retry.
            if status == 0 and attempt < attempts:
                log.warning(
                    f"[{self._account_email}] (bridge) auth/session transient "
                    f"(status=0, {err or 'no detail'}) — retry {attempt}/{attempts}…"
                )
                await asyncio.sleep(1.0 * attempt)
                continue
            if status == 0:
                raise SessionDeadError(
                    self._account_email,
                    f"Extension không fetch được /fx/api/auth/session ({err or 'no detail'}) "
                    f"sau {attempts} lần thử. Kiểm tra: chrome://extensions có 'RedOne Auth Helper' "
                    "+ popup 3 dòng xanh + tab flow.google.com đang mở, đã đăng nhập.",
                )

            # 200 OK but no access_token = page returned but user signed out.
            # 404 = labs.google has no NextAuth session for current Chrome user (normal on flow.google.com).
            if status == 404 or (status == 200 and not (isinstance(body, dict) and body.get("access_token"))):
                if bridge.is_extension_live():
                    log.info(
                        f"[{self._account_email}] (bridge) NextAuth session not present on labs.google, "
                        "but Chrome extension bridge is live on flow.google.com. Proceeding with BOQ cookie auth."
                    )
                    self._token = "BOQ_COOKIE_AUTH"
                    return
                raise SessionDeadError(
                    self._account_email,
                    "Chưa đăng nhập Google trong Chrome thật. "
                    "Mở tab https://flow.google.com trong Chrome thật, "
                    "click Sign in, chọn account Google. Sau đó retry task.",
                )

            log.error(f"[{self._account_email}] (bridge) token fetch failed: HTTP {status} {body}")
            if bridge.is_extension_live():
                log.info(f"[{self._account_email}] (bridge) Fallback to BOQ cookie auth.")
                self._token = "BOQ_COOKIE_AUTH"
                return
            raise SessionDeadError(
                self._account_email,
                f"Không lấy được session token (HTTP {status}, error: {err}). Login lại trong Chrome.",
            )

    async def renew_token(self, bad_token: Optional[str] = None):
        """Force a fresh /fx/api/auth/session."""
        async with self._get_token_lock():
            # If a concurrent task already successfully renewed the token, reuse it!
            if bad_token and self._token != bad_token:
                log.info(f"[{self._account_email}] (bridge) token already renewed by another task.")
                return

            log.info(f"[{self._account_email}] (bridge) renewing token...")
            self._token = None


            # _do_get_token, NOT ensure_token: we already hold _token_lock and
            # ensure_token() takes the SAME lock. asyncio.Lock isn't reentrant,
            # so calling it here waits on ourselves forever — no timeout, no
            # error, the batch's gather() simply never returns. Fires on the
            # first 403 of a run and freezes the whole task.
            await self._do_get_token()

    # ── Sandbox HTTP calls (route via bridge) ───────────────────────

    async def _browser_sandbox_request(
        self, endpoint: str, payload: dict, is_text_plain: bool = False,
    ) -> dict:
        """Replacement for the Playwright-based version. Calls
        aisandbox-pa endpoint via bridge.proxy_fetch — runs inside the
        user's flow.google.com tab so cookies + fingerprint are real.
        """
        await self.ensure_token()
        token = self._token or ""
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        body_json = json.dumps(payload, ensure_ascii=False)

        ct = "text/plain;charset=UTF-8" if is_text_plain else "application/json"
        for attempt in range(1, MAX_RETRY_COUNT + 1):
            try:
                # Match the original Playwright _browser_sandbox_request JS
                # exactly: ONLY Authorization + Content-Type. Adding x-client-data
                # or x-browser-validation triggers an additional CORS preflight
                # header check that aisandbox-pa rejects → "Failed to fetch".
                # The browser context (labs.google tab) supplies its own
                # x-client-data when needed via Chrome's network stack.
                r = await bridge.proxy_fetch(
                    url=url,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": ct,
                    },
                    body=body_json,
                    response_mode="json",
                    timeout_ms=120000,
                )
            except BridgeExtensionOfflineError as e:
                return {"error": str(e)}
            except BridgeTimeoutError as e:
                log.warning(f"(bridge) {endpoint} attempt {attempt} timeout: {e}")
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2 * attempt)
                    continue
                return {"error": str(e)}

            if r.get("error"):
                log.warning(f"(bridge) {endpoint} attempt {attempt}: {r['error']}")
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2 * attempt)
                    continue
                return {"error": r["error"]}

            status = r.get("status", 0)

            # 401/403 → renew token and retry. After max retries on 401,
            # session is dead.
            if status in (401, 403) and attempt < MAX_RETRY_COUNT:
                body = r.get("body")
                err_text = ""
                if isinstance(body, dict):
                    err_text = json.dumps(body, ensure_ascii=False)[:200]
                elif isinstance(body, str):
                    err_text = body[:200]
                else:
                    err_text = r.get("body_text", "")[:200]
                log.warning(f"(bridge) {endpoint}: HTTP {status}: {err_text[:100]}")
                # reCAPTCHA-flavored 403 — track + don't bother renewing token,
                # surface the error so circuit breaker can handle.
                low = err_text.lower()
                if "recaptcha" in low or "unusual_activity" in low:
                    self._record_error(f"403_recaptcha")
                    return {"error": f"HTTP {status}", "text": err_text}
                self._record_error(f"{status}")
                try:
                    await self.renew_token(token)
                except SessionDeadError:
                    raise
                token = self._token or ""
                await asyncio.sleep(1)
                continue

            # Persistent 401 → session dead
            if status == 401 and attempt >= MAX_RETRY_COUNT:
                body = r.get("body")
                err_text = json.dumps(body)[:200] if isinstance(body, dict) else str(body)[:200]
                low = err_text.lower()
                if "missing required authentication" in low or "unauthenticated" in low:
                    raise SessionDeadError(
                        self._account_email,
                        "Google trả 401 nhiều lần — session chết, login lại.",
                    )

            if 200 <= status < 300:
                self._record_success()
                body = r.get("body")
                if isinstance(body, dict):
                    return body
                # Body was returned as plain text (not JSON) — caller
                # expecting dict gets back wrapped text for them to handle.
                return {"text": (r.get("body_text") or "")[:2000]}

            # Other HTTP errors
            body = r.get("body")
            err_text = json.dumps(body)[:300] if isinstance(body, dict) else (
                r.get("body_text") or str(body)
            )[:300]
            log.warning(f"(bridge) {endpoint} HTTP {status}: {err_text}")
            if attempt < MAX_RETRY_COUNT and status >= 500:
                await asyncio.sleep(2 * attempt)
                continue
            return {"error": f"HTTP {status}", "text": err_text}

        return {"error": "Max retries exceeded"}

    async def _sandbox_request(
        self, endpoint: str, payload, method: str = "POST",
    ) -> dict:
        """Same as _browser_sandbox_request — historically the Playwright
        FlowClient had two paths (httpx + browser fetch), but with the
        bridge everything goes through the user's tab."""
        if not isinstance(payload, (str, bytes)):
            payload_dict = payload if isinstance(payload, dict) else {}
        else:
            try:
                payload_dict = json.loads(payload)
            except Exception:
                payload_dict = {}
        return await self._browser_sandbox_request(endpoint, payload_dict)

    # ── reCAPTCHA via bridge ────────────────────────────────────────

    FLOW_RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

    async def preflight_session(self) -> None:
        """Execute preflight RPC `nzlxg` matching Google Flow Angular frontend.
        Google Flow sends nzlxg before every generation RPC to verify quota
        and establish session affinity (af.httprm).
        """
        try:
            source_path = f"/project/{self.project_id}" if self.project_id else "/"
            log.info(f"[{self._account_email}] (BOQ) preflight session (nzlxg) for project={self.project_id}...")
            await self._execute_batchexecute(
                rpc_id="nzlxg",
                inner_payload=[],
                source_path=source_path,
                timeout_ms=15000,
            )
        except Exception as e:
            log.warning(f"[{self._account_email}] (BOQ) preflight nzlxg warning: {e}")

    async def get_recaptcha_token(self, action: str = "VIDEO_GENERATION") -> str:
        """Harvest reCAPTCHA token from the user's real Chrome using Google Flow's official sitekey."""
        log.info(f"[{self._account_email}] (bridge) harvesting reCAPTCHA action={action}...")
        try:
            token = await bridge.harvest_recaptcha(site_key=self.FLOW_RECAPTCHA_SITE_KEY, action=action)
        except BridgeExtensionOfflineError as e:
            log.warning(f"(bridge) reCAPTCHA: {e}")
            return ""
        except (BridgeTimeoutError, RuntimeError) as e:
            log.warning(f"(bridge) reCAPTCHA harvest failed: {e}")
            self._recaptcha_fail_count += 1
            return ""
        log.info(f"[{self._account_email}] (bridge) got token: {token[:20]}...")
        self._recaptcha_fail_count = 0
        return token

    # ── tRPC API (via bridge) ───────────────────────────────────────

    async def _api(self, procedure: str, payload, method: str = "POST") -> dict:
        """tRPC API — also via bridge so cookies are valid for
        labs.google domain."""
        await self.ensure_token()
        token = self._token or ""
        # tRPC is on labs.google (same-origin from labs.google tab) so
        # we can keep cookies + minimal headers. No x-client-data etc.
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            if method.upper() == "GET":
                import urllib.parse
                inp = json.dumps({"json": payload})
                url = f"{self.TRPC}/{procedure}?input={urllib.parse.quote(inp)}"
                r = await bridge.proxy_fetch(
                    url=url, method="GET", headers=headers,
                    response_mode="json", timeout_ms=30000,
                )
            else:
                url = f"{self.TRPC}/{procedure}"
                body = json.dumps({"json": payload})
                r = await bridge.proxy_fetch(
                    url=url, method="POST", headers=headers, body=body,
                    response_mode="json", timeout_ms=30000,
                )
            status = r.get("status", 0)
            body = r.get("body")
            if 200 <= status < 300 and isinstance(body, dict):
                return body
            return {"error": status, "text": (r.get("body_text") or "")[:500]}
        except Exception as e:
            return {"error": str(e)}

    # ── Credits check (BOQ batchexecute) ─────────────────────────────

    async def check_credits(self) -> Optional[dict]:
        """Check remaining credits via batchexecute RPC `nzlxg` and bridge tab state.

        Returns {"remainingCredits": N, "tier": "FREE|PRO|ULTRA"} on success.
        """
        try:
            r = await self._execute_batchexecute(
                rpc_id="nzlxg",
                inner_payload=[],
                source_path="/",
                timeout_ms=20000,
            )
            status = r.get("status", 0)
            rpc_result = r.get("rpc_result")
            err = r.get("error")
            if status != 200 or err:
                log.warning(f"(bridge) check_credits: status={status} error={err}")
                # Fallback to bridge tab state if extension detected credits/tier
                ext_credits = bridge.get_active_account_credits()
                ext_tier = bridge.get_active_account_tier()
                if ext_credits is not None:
                    return {"remainingCredits": ext_credits, "tier": ext_tier}
                return {"error": f"HTTP {status}: {err}"}

            # nzlxg response: [totalCredits, paygateTierCode, ?, ?, null, totalCredits]
            # Example: [21214, 2, 3, 3, null, 21214] where 2 = PAYGATE_TIER_TWO (ULTRA)
            if isinstance(rpc_result, str):
                try:
                    rpc_result = json.loads(rpc_result)
                except Exception:
                    pass

            if isinstance(rpc_result, list) and len(rpc_result) >= 1:
                credits = rpc_result[0] if isinstance(rpc_result[0], (int, float)) else 0

                tier = "FREE"
                raw_str = json.dumps(rpc_result) if rpc_result else ""
                if "TIER_TWO" in raw_str or "ADVANCED" in raw_str or "TIER2" in raw_str:
                    tier = "ULTRA"
                elif len(rpc_result) >= 2 and rpc_result[1] == 2:
                    tier = "ULTRA"
                elif len(rpc_result) >= 2 and rpc_result[1] == 1:
                    tier = "PRO"
                elif credits >= 500:
                    # Free tier never exceeds 100 credits. >= 500 credits indicates paid Ultra/Pro subscription.
                    tier = "ULTRA"

                # Check if tab DOM explicitly detected ULTRA badge
                ext_tier = bridge.get_active_account_tier()
                if ext_tier in ("ULTRA", "PRO"):
                    tier = ext_tier

                return {"remainingCredits": int(credits), "tier": tier}
            log.warning(f"(bridge) check_credits: unexpected result: {rpc_result}")
            return {"error": "Unexpected credits response format"}
        except Exception as e:
            log.error(f"(bridge) check_credits error: {e}")
            return {"error": str(e)}

    # ── Project resolution (Auto-discovery & initialization) ─────────

    async def fetch_user_projects(self) -> list[str]:
        """Fetch existing project IDs for this account using BOQ RPC UpteDb."""
        try:
            active_p = bridge.get_active_project_id()
            source_path = f"/project/{active_p}" if (active_p and active_p not in BridgeFlowClient._FAILED_PROJECT_IDS) else "/"
            r = await self._execute_batchexecute(
                rpc_id="UpteDb",
                inner_payload=["projects/*", 21, None, None, None, None, [1]],
                source_path=source_path,
                timeout_ms=15000,
            )
            rpc_result = r.get("rpc_result")
            log.info(
                f"[{self._account_email}] UpteDb response: status={r.get('status')} "
                f"error={r.get('error')} rpc_result_type={type(rpc_result)} "
                f"rpc_result_preview={str(rpc_result)[:300]} "
                f"blacklisted={list(BridgeFlowClient._FAILED_PROJECT_IDS)[:5]}"
            )
            if isinstance(rpc_result, list) and len(rpc_result) > 0 and isinstance(rpc_result[0], list):
                projects = []
                for item in rpc_result[0]:
                    if isinstance(item, list) and len(item) > 0 and isinstance(item[0], str):
                        pid = item[0]
                        if pid not in BridgeFlowClient._FAILED_PROJECT_IDS:
                            projects.append(pid)
                log.info(f"[{self._account_email}] Discovered {len(projects)} existing valid Google Flow project(s)")
                return projects
        except Exception as e:
            log.warning(f"[{self._account_email}] fetch_user_projects failed: {e}")
        return []

    async def create_project(self, display_name: str = "Flow Project") -> Optional[str]:
        """Create a new Google Flow project via jHPbke RPC (G-Labs architecture)."""
        import datetime
        now_str = datetime.datetime.now().strftime("%b %d - %H:%M")
        name = f"{display_name} ({now_str})"
        inner = ["projects/*", [None, [name]], [None, 22]]
        try:
            sess = await self.get_flow_session()
            res = await sess.execute("jHPbke", inner, source_path="/")
            rpc_result = res.get("rpc_result")
            if rpc_result and isinstance(rpc_result, list) and len(rpc_result) > 0:
                new_pid = rpc_result[0]
                log.info(f"[{self._account_email}] Created new Flow project via jHPbke: {new_pid}")
                BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = new_pid
                self.project_id = new_pid
                return new_pid
        except Exception as e:
            log.warning(f"[{self._account_email}] create_project jHPbke error: {e}")
        return None

    async def _handle_project_error(self, status: int, err: Any) -> None:
        """Mark current project_id as rejected, invalidate cache, and re-resolve."""
        bad_proj = self.project_id
        err_str = str(err)
        # UNUSUAL_ACTIVITY / [7] is rate-limit / captcha check.
        # DO NOT blacklist project, DO NOT create new project, DO NOT navigate the tab.
        if "UNUSUAL_ACTIVITY" in err_str or "[7]" in err_str or "[7," in err_str or "PERMISSION_DENIED" in err_str:
            log.warning(f"[{self._account_email}] Error 7 (UNUSUAL_ACTIVITY) detected: {err}. Keeping active project in tab.")
            active_p = bridge.get_active_project_id()
            if active_p:
                self.project_id = active_p
            return

        if "PROJECT_NOT_FOUND" in err_str or "[5]" in err_str or "[5," in err_str:
            if bad_proj:
                log.warning(f"[{self._account_email}] Blacklisting not-found project {bad_proj} (status={status}, err={err})")
                BridgeFlowClient._FAILED_PROJECT_IDS.add(bad_proj)
            BridgeFlowClient._ACTIVE_PROJECT_IDS.pop(self._account_email, None)
            await self.ensure_project_id(force_refresh=True)
            return

        active_p = bridge.get_active_project_id()
        if active_p:
            self.project_id = active_p

    async def ensure_project_id(self, force_refresh: bool = False) -> str:
        """Ensure self.project_id points to a VALID, existing project in Google Flow.
        
        Resolution order (G-Labs Studio architecture):
        1. ACTIVE TAB CHECK: If Chrome tab is open to a project (/project/<id>), use it immediately.
        2. Check memory cache (_ACTIVE_PROJECT_IDS) unless force_refresh.
        3. Auto-discover the user's existing projects via UpteDb RPC.
        4. Create fresh project directly via RPC jHPbke (G-Labs style).
        NEVER force tab navigation or reload!
        """
        active_email = bridge.get_active_account_email()
        if active_email and active_email.lower() != self._account_email.lower():
            log.info(
                f"Aligning client account from {self._account_email} to active Flow tab account: {active_email}"
            )
            self._account_email = active_email
            self.project_id = ""

        # 1. Active tab check (Chrome tab URL) — HIGHEST PRIORITY
        active_tab_proj = bridge.get_active_project_id()
        if active_tab_proj:
            BridgeFlowClient._FAILED_PROJECT_IDS.discard(active_tab_proj)
            BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = active_tab_proj
            self.project_id = active_tab_proj
            log.info(f"[{self._account_email}] Using active project from Chrome tab: {active_tab_proj}")
            return active_tab_proj

        # 2. Check memory cache
        cached = BridgeFlowClient._ACTIVE_PROJECT_IDS.get(self._account_email)
        if not force_refresh and cached and cached not in BridgeFlowClient._FAILED_PROJECT_IDS:
            self.project_id = cached
            return self.project_id

        # 3. RPC UpteDb check (Google Cloud source of truth for user's real projects)
        projects = await self.fetch_user_projects()
        valid_projects = [p for p in projects if p not in BridgeFlowClient._FAILED_PROJECT_IDS]
        if valid_projects:
            latest_proj = valid_projects[0]
            BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = latest_proj
            self.project_id = latest_proj
            log.info(f"[{self._account_email}] Auto-selected latest Google Flow project: {latest_proj}")
            return latest_proj

        # 4. Provision new project directly via RPC jHPbke (G-Labs style)
        log.info(f"[{self._account_email}] No projects found. Creating new project via jHPbke...")
        try:
            new_pid = await self.create_project("Flow Project")
            if new_pid:
                return new_pid
        except Exception as e:
            log.warning(f"[{self._account_email}] create_project failed: {e}")

        # Fallback to verified project
        fallback_pid = "d9ec99ab-368b-49e4-b641-e74719742745"
        self.project_id = self.project_id or fallback_pid
        return self.project_id


    async def upload_image(self, image_path: str | Path) -> str:
        """Upload image to Google Flow via batchexecute maseQ RPC and return media_id."""
        p = Path(image_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        media_id = await self._upload_image_raw(p)
        if not media_id:
            raise ValueError(f"Failed to upload reference image: {p.name}")
        return media_id

    async def _upload_image_raw(self, path: "Path") -> Optional[str]:
        """Upload image to Flow via batchexecute RPC `maseQ`.

        Returns the media_id (UUID string) from the response, or raises ValueError.
        """
        await self.ensure_project_id()
        import uuid as _uuid
        raw, mime = await asyncio.to_thread(_shrink_image_for_upload, path)
        b64 = base64.b64encode(raw).decode("utf-8")

        # reCAPTCHA token minted inline by extension (prevents UNUSUAL_ACTIVITY)
        client_ctx = [
            None, 22, None, None, None,
            self.project_id,
            None, None, None, None,
            ["__MINT_RECAPTCHA__", 1],
        ]

        uuid1 = str(_uuid.uuid4()).upper()
        uuid2 = str(_uuid.uuid4()).upper()

        inner_payload = [
            client_ctx,
            b64,
            mime,
            1,
            None, None, None, None,
            path.name,
            None,
            uuid1,
            uuid2,
        ]

        log.info(f"[{self._account_email}] (BOQ) Uploading ref image {path.name} ({len(raw)} bytes, {mime})...")
        r = await self._execute_batchexecute(
            rpc_id="maseQ",
            inner_payload=inner_payload,
            source_path=f"/project/{self.project_id}",
            timeout_ms=60000,
            recaptcha_action="UPLOAD_IMAGE",
        )

        rpc_result = r.get("rpc_result")
        err = r.get("error")
        status = r.get("status", 0)

        if status == 400 or (err and ("UNUSUAL_ACTIVITY" in str(err) or "[7," in str(err))):
            await self._handle_project_error(status, err)

        if not rpc_result or not isinstance(rpc_result, list) or not rpc_result[0]:
            err = err or "Unknown upload failure"
            log.error(f"[{self._account_email}] (BOQ) Upload failed: {err}")
            raise ValueError(f"Upload ảnh tham chiếu lỗi: {err}")

        active_p = r.get("active_project_id")
        if active_p and active_p != self.project_id and active_p not in BridgeFlowClient._FAILED_PROJECT_IDS:
            BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = active_p
            self.project_id = active_p

        media_id = rpc_result[0][0]
        log.info(f"[{self._account_email}] (BOQ) Upload OK: {path.name} → {media_id}")
        return media_id

    # ── Image gen (BOQ batchexecute `ogiZ0b`) ────────────────────────

    # Model name mapping for batchexecute (may differ from REST API)
    BOQ_IMAGE_MODEL_MAP = {
        "nano_banana_pro": "GEM_PIX_2",
        "nano_banana_2": "NARWHAL",
        "nano_banana_lite": "HARBOR_SEAL",
        "imagen_4": "IMAGEN_3_5",
        "imagen_3_5": "IMAGEN_3_5",
        "imagen_3": "IMAGEN_3",
        "imagen_3_fast": "IMAGEN_3_FAST",
    }

    # Aspect ratio → numeric code used in batchexecute (_.PI & _.HSa in Google bundle)
    # SQUARE: 1, PORTRAIT (9:16): 2, LANDSCAPE (16:9): 3, PORTRAIT_3_4: 4, LANDSCAPE_4_3: 5
    BOQ_ASPECT_RATIO_MAP = {
        "1:1": 1,
        "9:16": 2,
        "16:9": 3,
        "3:4": 4,
        "4:3": 5,
    }

    async def generate_image(
        self,
        prompt: str,
        model_key: str = "nano_banana_pro",
        aspect_ratio: str = "1:1",
        reference_images: list[str] | None = None,
        seed: int | None = None,
    ) -> dict:
        """Generate an image using BOQ batchexecute RPC `ogiZ0b`.

        Supports text-to-image and image-to-image (reference images).
        Returns dict with keys: media_id, download_url, seed, width, height
        """
        import random as _rand
        import uuid as _uuid

        if seed is None:
            seed = _rand.randint(100000000, 2147483647)

        await self.ensure_project_id()

        # Resolve local reference image paths via maseQ RPC directly (Zero DOM)
        resolved_refs = []
        for p in (reference_images or []):
            if p and Path(str(p)).exists():
                try:
                    ref_id = await self.upload_image(p)
                    resolved_refs.append(ref_id)
                except Exception as up_err:
                    log.warning(f"[{self._account_email}] Failed to upload ref image {p}: {up_err}")
            elif p:
                resolved_refs.append(str(p))
        reference_images = resolved_refs

        model_name = self.BOQ_IMAGE_MODEL_MAP.get(model_key, "GEM_PIX_2")
        ar_code = self.BOQ_ASPECT_RATIO_MAP.get(aspect_ratio, 3)

        # Preflight session to match Google Flow Angular frontend (nzlxg)
        await self.preflight_session()

        # reCAPTCHA token will be minted INLINE by the extension inside the
        # same executeScript call that sends batchexecute — prevents Google's
        # UNUSUAL_ACTIVITY detection from context mismatch. Use placeholder.
        RECAPTCHA_PLACEHOLDER = "__MINT_RECAPTCHA__"

        # Build batch UUIDs
        batch_uuid = str(_uuid.uuid4()).upper()
        op_uuid = str(_uuid.uuid4()).upper()

        # Build reference images array for index 2
        # Format from HAR: [["<media_id>", null, null, null, 1], ...]
        ref_arr = None
        if reference_images:
            ref_arr = [[ref_id, None, None, None, 1] for ref_id in reference_images]

        client_ctx = [
            None, 22, None, None, None,
            self.project_id,
            None, None, None, None,
            [RECAPTCHA_PLACEHOLDER, 1],
        ]
        prompt_arr = [[[prompt]]]

        # Build 4 candidates matching Google Flow Angular frontend batch format (docs/flow.google.com.update1.har)
        candidates = []
        for i in range(4):
            c_seed = seed if i == 0 else _rand.randint(100000000, 2147483647)
            c_batch = batch_uuid if i == 0 else str(_uuid.uuid4()).upper()
            c_op = op_uuid if i == 0 else str(_uuid.uuid4()).upper()
            candidates.append([
                None, None, ref_arr, c_seed, ar_code, model_name, None,
                client_ctx,
                prompt_arr,
                None, None, None,
                c_batch, c_op,
            ])

        inner_payload = [
            None,
            candidates,
            1,
            client_ctx,
            [str(_uuid.uuid4()).upper()],
        ]

        source_path = f"/project/{self.project_id}"

        log.info(
            f"[{self._account_email}] (BOQ) Generating image: model={model_name}, "
            f"seed={seed}, refs={len(reference_images or [])}, project={self.project_id}"
        )

        # Retry loop
        result = None
        for attempt in range(5):
            if attempt > 0:
                await asyncio.sleep(_rand.uniform(1.5, 3.0))
                # Re-preflight session; token will be re-minted inline by extension
                await self.preflight_session()
                new_ctx = [
                    None, 22, None, None, None,
                    self.project_id,
                    None, None, None, None,
                    [RECAPTCHA_PLACEHOLDER, 1],
                ]
                inner_payload[3] = new_ctx
                for cand in inner_payload[1]:
                    cand[7] = new_ctx
                    cand[3] = _rand.randint(100000000, 2147483647)
                    cand[12] = str(_uuid.uuid4()).upper()
                    cand[13] = str(_uuid.uuid4()).upper()
                inner_payload[4] = [str(_uuid.uuid4()).upper()]

            try:
                r = await self._execute_batchexecute(
                    rpc_id="ogiZ0b",
                    inner_payload=inner_payload,
                    source_path=source_path,
                    timeout_ms=120000,
                    recaptcha_action="IMAGE_GENERATION",
                )
            except BridgeExtensionOfflineError as e:
                raise ValueError(str(e))
            except BridgeTimeoutError as e:
                log.warning(f"(BOQ) ogiZ0b attempt {attempt + 1} timeout: {e}")
                if attempt < 4:
                    continue
                raise ValueError(str(e))

            status = r.get("status", 0)
            err = r.get("error")
            rpc_result = r.get("rpc_result")

            if status == 400 or (err and ("PROJECT_NOT_FOUND" in str(err) or "[5," in str(err) or "[5]" in str(err) or "UNUSUAL_ACTIVITY" in str(err) or "[7," in str(err) or "[7]" in str(err) or "PERMISSION_DENIED" in str(err))):
                log.warning(f"[{self._account_email}] Project rejected (HTTP {status}, {err}). Auto-recovering...")
                await self._handle_project_error(status, err)
                source_path = f"/project/{self.project_id}"
                if attempt < 4:
                    await asyncio.sleep(2.0)
                    continue
                raise ValueError(f"batchexecute HTTP {status}: {err}")

            if err and status == 0:
                log.warning(f"(BOQ) ogiZ0b attempt {attempt + 1} error: {err}")
                if attempt < 4:
                    continue
                raise ValueError(f"batchexecute error: {err}")

            if status != 200:
                err_text = r.get("body_text", err or "")
                log.warning(f"(BOQ) ogiZ0b HTTP {status}: {err_text[:200]}")
                if attempt < 4:
                    continue
                raise ValueError(f"batchexecute HTTP {status}: {err_text[:200]}")

            if err:
                log.warning(f"(BOQ) ogiZ0b attempt {attempt + 1} RPC error: {err}")
                if "QUOTA_REACHED" in str(err) or "PER_MODEL_DAILY_QUOTA" in str(err):
                    raise ValueError(f"Google RPC error: {err}")
                if attempt < 4:
                    continue
                raise ValueError(f"Google RPC error: {err}")

            if rpc_result is None:
                log.warning(f"(BOQ) ogiZ0b attempt {attempt + 1}: no rpc_result in response")
                if attempt < 4:
                    continue
                raise ValueError("No RPC result in batchexecute response")

            active_p = r.get("active_project_id")
            if active_p and active_p != self.project_id and active_p not in BridgeFlowClient._FAILED_PROJECT_IDS:
                BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = active_p
                self.project_id = active_p

            result = rpc_result
            break
        else:
            raise ValueError("Image gen failed after 5 attempts")

        # Parse BOQ response
        return self._extract_boq_image_result(result, seed)

    def _extract_boq_image_result(self, rpc_result: list, seed: int) -> dict:
        """Extract image data from ogiZ0b RPC response.

        Response structure (from HAR):
        [
          [  # media array
            [IMAGE_ID, null, BATCH_ID, null, null, null,
              [  # image data
                [null, SEED, ..., PROMPT, ..., BATCH_ID, null,
                  DOWNLOAD_URL, AR_CODE, ..., IMAGE_ID
                ],
                null,
                [WIDTH, HEIGHT]
              ]
            ]
          ],
          [  # workflow/batch metadata
            [BATCH_ID, ...]
          ]
        ]
        """
        try:
            media_arr = rpc_result[0]
            if not media_arr or not isinstance(media_arr, list):
                raise ValueError(f"Empty media array in BOQ response: {str(rpc_result)[:300]}")

            first_media = media_arr[0]
            image_id = first_media[0]

            image_data = first_media[6]  # [[...], null, [W, H]]
            gen_entry = image_data[0]    # [null, seed, ..., url, ...]
            dims = image_data[2]         # [width, height]

            actual_seed = gen_entry[1] if gen_entry[1] else seed
            download_url = gen_entry[13]  # flow-content.google URL
            media_id = gen_entry[17] if len(gen_entry) > 17 else image_id

            width = dims[0] if dims and len(dims) > 0 else 0
            height = dims[1] if dims and len(dims) > 1 else 0

            self._record_success()
            log.info(
                f"[{self._account_email}] (BOQ) Image gen OK: "
                f"{image_id}, {width}x{height}, seed={actual_seed}"
            )

            return {
                "media_id": media_id or image_id,
                "download_url": download_url,
                "seed": actual_seed,
                "width": width,
                "height": height,
            }
        except (IndexError, TypeError, KeyError) as e:
            log.error(
                f"[{self._account_email}] (BOQ) Failed to parse ogiZ0b response: "
                f"{e} — raw: {str(rpc_result)[:500]}"
            )

    # ── Upscale Image (GPU-Accelerated 2K/4K Upscale) ─────────────────

    async def upscale_image(
        self,
        media_id: str,
        resolution: str = "4k",
        input_path: str | Path | None = None,
    ) -> dict:
        """Upscale an image to 2K or 4K.

        Uses local GPU-accelerated Real-ESRGAN NCNN Vulkan (~1.8s on RTX 4060 Ti)
        with PIL Lanczos fallback. Completely avoids Google reCAPTCHA Enterprise bot
        detection (PUBLIC_ERROR_UNUSUAL_ACTIVITY) and network failures.

        Args:
            media_id: The media_id / generation_id of the source image
            resolution: "2k" or "4k"
            input_path: Optional direct path to source image file

        Returns:
            dict with:
                - media_id: str
                - download_url: Optional[str]
                - encoded_image: bytes (raw JPEG/PNG bytes)
                - width: int
                - height: int
        """
        scale = 2 if resolution.lower() == "2k" else 4
        log.info(
            f"[{self._account_email}] Upscaling image {media_id} to {resolution} (scale={scale})"
        )

        # 1. Resolve source image path
        src_path = Path(input_path).resolve() if input_path else None
        if not src_path or not src_path.exists():
            from ..database import db
            # Lookup output_path of item matching media_id
            for t in db.list_tasks(limit=50):
                for it in db.get_task_items(t["id"]):
                    ex = json.loads(it.get("extra_json") or "{}")
                    if ex.get("media_id") == media_id and it.get("output_path"):
                        p = Path(it["output_path"])
                        if p.exists():
                            src_path = p
                            break
                if src_path:
                    break

        if not src_path or not src_path.exists():
            raise FileNotFoundError(f"Cannot find source image file for media_id: {media_id}")

        # 2. Run upscale via upscaler service
        from .upscaler import upscale_image_file
        import tempfile
        tmp_out = Path(tempfile.gettempdir()) / f"redone_upscale_{media_id}_{resolution}.png"
        out_p = await upscale_image_file(src_path, tmp_out, scale=scale)

        raw_bytes = out_p.read_bytes()
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass

        from PIL import Image
        with Image.open(src_path) as im:
            orig_w, orig_h = im.size

        is_landscape = orig_w >= orig_h
        if scale == 2:
            up_w, up_h = (2560, 1440) if is_landscape else (1440, 2560)
        else:
            up_w, up_h = (3840, 2160) if is_landscape else (2160, 3840)

        return {
            "media_id": media_id,
            "download_url": None,
            "encoded_image": raw_bytes,
            "width": up_w,
            "height": up_h,
        }

    # ── Video gen (BOQ batchexecute `eb1hJf` + `jwpduf`) ───────────

    BOQ_VIDEO_MODEL_MAP = {
        "veo_3_generate_video_fast": "veo_3_1_t2v_fast",
        "veo_3_generate_video_lite_lp": "veo_3_1_t2v_lite",
        "veo_3_1_t2v_lite_low_priority": "veo_3_1_t2v_lite",
        "veo_3_1_i2v_lite_low_priority": "veo_3_1_i2v_lite",
        "veo_3_1_t2v_fast_ultra": "veo_3_1_t2v_fast",
        "veo_3_1_t2v_lite": "veo_3_1_t2v_lite",
        "veo_3_1_t2v_fast": "veo_3_1_t2v_fast",
        "veo_3_1_t2v_quality": "veo_3_1_t2v_quality",
        "veo_3_1_i2v_lite": "veo_3_1_i2v_lite",
        "veo_3_1_i2v_fast": "veo_3_1_i2v_fast",
        "veo_3_1_i2v_quality": "veo_3_1_i2v_quality",
        "veo_3_1_fast": "veo_3_1_t2v_fast",
        "veo_3_1_quality": "veo_3_1_t2v_quality",
        "omni_flash": "omni_flash",
        "lite": "veo_3_1_t2v_lite",
        "fast": "veo_3_1_t2v_fast",
        "quality": "veo_3_1_t2v_quality",
    }

    async def generate_video(
        self,
        prompt: str,
        reference_image: Optional[str] = None,
        end_image: Optional[str] = None,
        model_key: str = "veo_3_generate_video_fast",
        aspect_ratio: str = "LANDSCAPE",
        duration: int = 8,
        ref_image_path: Optional[str] = None,
        video_mode: str = "start_image",
    ) -> Optional[str]:
        """Submit video generation request via Native Flow UI Worker (primary) or BOQ RPC (fallback).

        - Text-to-Video (T2V)
        - Image-to-Video (I2V)
        Returns the generation/media ID.
        """
        import uuid as _uuid
        await self.ensure_project_id()

        # 1. Resolve reference image and end image to media_id via maseQ RPC (Zero DOM)
        actual_ref = ref_image_path if (ref_image_path and Path(str(ref_image_path)).exists()) else (
            reference_image if (reference_image and Path(str(reference_image)).exists()) else reference_image
        )
        if actual_ref and Path(str(actual_ref)).exists():
            try:
                log.info(f"[{self._account_email}] Uploading start frame image {actual_ref} via maseQ RPC...")
                actual_ref = await self.upload_image(actual_ref)
            except Exception as up_err:
                log.warning(f"[{self._account_email}] Failed to upload start frame {actual_ref}: {up_err}")

        actual_end = end_image
        if actual_end and Path(str(actual_end)).exists():
            try:
                log.info(f"[{self._account_email}] Uploading end frame image {actual_end} via maseQ RPC...")
                actual_end = await self.upload_image(actual_end)
            except Exception as up_err:
                log.warning(f"[{self._account_email}] Failed to upload end frame {actual_end}: {up_err}")

        reference_image = actual_ref

        # Map aspect ratio: 2 = 16:9 (LANDSCAPE), 1 = 9:16 (PORTRAIT)
        ar_code = 1 if ("9:16" in str(aspect_ratio) or "PORTRAIT" in str(aspect_ratio).upper()) else 2

        # Map model name and RPC ID
        if reference_image and actual_end:
            model_name = self.BOQ_VIDEO_MODEL_MAP.get(model_key, "veo_3_1_i2v_lite")
            rpc_id = "anprQif"
        elif reference_image:
            if model_key and "i2v" in model_key:
                model_name = self.BOQ_VIDEO_MODEL_MAP.get(model_key, model_key)
            elif model_key and "fast" in model_key:
                model_name = "veo_3_1_i2v_fast"
            else:
                model_name = "veo_3_1_i2v_lite"
            rpc_id = "eb1hJf"
        else:
            model_name = self.BOQ_VIDEO_MODEL_MAP.get(model_key, model_key or "veo_3_1_t2v_lite")
            rpc_id = "YhhmEf"

        source_path = f"/project/{self.project_id}"
        log.info(
            f"[{self._account_email}] (BOQ) Generating video ({rpc_id}): model={model_name}, "
            f"has_ref={bool(reference_image)}, has_end={bool(actual_end)}, project={self.project_id}"
        )

        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2.0)

            # Preflight session to match Google Flow Angular frontend (nzlxg)
            await self.preflight_session()

            # reCAPTCHA token minted inline by extension
            client_ctx = [
                None, 22, None, None, None,
                self.project_id,
                None, None, None, None,
                ["__MINT_RECAPTCHA__", 1],
            ]

            prompt_item = [None, None, [[[prompt or "Static shot"]]]]
            uuid_a = str(_uuid.uuid4()).upper()
            uuid_b = str(_uuid.uuid4()).upper()

            if reference_image and actual_end:
                ref_config = [None, reference_image, None, None, actual_end, [None, None, 1, 1]]
                candidate = [
                    prompt_item,
                    model_name,
                    ar_code,
                    None,
                    ref_config,
                    [None, None, None, None, uuid_a, uuid_b],
                ]
            elif reference_image:
                ref_config = [None, reference_image, None, None, None, [None, None, 1, 1]]
                candidate = [
                    prompt_item,
                    model_name,
                    ar_code,
                    None,
                    ref_config,
                    [None, None, None, None, uuid_a, uuid_b],
                ]
            else:
                # T2V: 5-item candidate directly ending with UUIDs at index 4 (confirmed via HAR capture)
                candidate = [
                    prompt_item,
                    model_name,
                    ar_code,
                    None,
                    [None, None, None, None, uuid_a, uuid_b],
                ]

            batch_uuid = str(_uuid.uuid4()).upper()
            inner_payload = [
                [candidate],
                client_ctx,
                [batch_uuid, 2],
            ]

            r = await self._execute_batchexecute(
                rpc_id=rpc_id,
                inner_payload=inner_payload,
                source_path=source_path,
                timeout_ms=120000,
                recaptcha_action="VIDEO_GENERATION",
            )

            status = r.get("status", 0)
            err = r.get("error")
            rpc_result = r.get("rpc_result")

            if status == 400 or (err and ("UNUSUAL_ACTIVITY" in str(err) or "[7," in str(err))):
                await self._handle_project_error(status, err)
                if attempt < 2:
                    source_path = f"/project/{self.project_id}"
                    continue

            if err or status != 200 or not rpc_result:
                log.error(f"(BOQ) {rpc_id} failed (attempt {attempt + 1}): status={status}, err={err}")
                if attempt < 2 and (not err or "UNUSUAL_ACTIVITY" in str(err) or "[7," in str(err)):
                    continue
                raise ValueError(f"Tạo video thất bại: {err or f'HTTP {status}'}")

            active_p = r.get("active_project_id")
            if active_p and active_p != self.project_id and active_p not in BridgeFlowClient._FAILED_PROJECT_IDS:
                BridgeFlowClient._ACTIVE_PROJECT_IDS[self._account_email] = active_p
                self.project_id = active_p

            try:
                candidates = rpc_result[3] if len(rpc_result) > 3 and isinstance(rpc_result[3], list) else rpc_result[1]
                generation_id = candidates[0][0]
                log.info(f"[{self._account_email}] (BOQ) Video generation started ({rpc_id}): {generation_id}")
                return generation_id
            except (IndexError, TypeError) as e:
                log.error(f"(BOQ) Could not extract generation_id from {rpc_id}: {e}, res={str(rpc_result)[:300]}")
                if attempt < 2:
                    continue
                raise ValueError(f"Không thể trích xuất ID video: {e}")

    async def poll_status(self, generation_id: str) -> dict:
        """Poll video generation status via BOQ batchexecute RPC `jwpduf`."""
        inner_payload = [
            None,
            None,
            [[generation_id]],
        ]

        try:
            r = await self._execute_batchexecute(
                rpc_id="jwpduf",
                inner_payload=inner_payload,
                source_path=f"/project/{self.project_id}",
                timeout_ms=30000,
            )
            rpc_result = r.get("rpc_result")
            if not rpc_result or not isinstance(rpc_result, list) or len(rpc_result) < 3:
                return {"state": "RUNNING"}

            items = rpc_result[2]
            if not items or not isinstance(items, list):
                return {"state": "COMPLETED"}

            for it in items:
                if isinstance(it, list) and len(it) > 0 and it[0] == generation_id:
                    meta = it[5] if len(it) > 5 and isinstance(it[5], list) else []
                    status_code = None
                    if len(meta) > 8 and isinstance(meta[8], list) and len(meta[8]) > 0:
                        status_code = meta[8][0]

                    if status_code == 3:
                        log.info(f"[{self._account_email}] (BOQ) Video {generation_id} COMPLETED")
                        return {
                            "state": "COMPLETED",
                            "name": generation_id,
                            "media_id": generation_id,
                            "video_id": generation_id,
                            "generation_id": generation_id,
                            "mediaMetadata": {
                                "mediaStatus": {"mediaGenerationStatus": "COMPLETED"},
                                "videoUri": f"https://flow-content.google/video/{generation_id}",
                            },
                            "media": [{"name": generation_id}],
                        }
                    elif status_code == 6:
                        return {"state": "RUNNING"}
                    elif status_code is not None:
                        log.warning(f"[{self._account_email}] (BOQ) Video {generation_id} status code: {status_code}")

            return {"state": "RUNNING"}
        except Exception as e:
            log.warning(f"(BOQ) poll_status error for {generation_id}: {e}")
            return {"state": "RUNNING"}

    # ── Binary downloads ───────────────────────────────────────────

    async def get_download_url(self, media_id: str, for_video: bool = True) -> Optional[str]:
        """Fetch signed download URL for media_id via BOQ RPC `as29s`.

        Queries flow-content.google signed CDN URL directly from Google Flow.
        For videos, prioritizes the `/video/` stream over the `/image/` thumbnail.
        """
        if not media_id:
            return None

        attempts = 3 if for_video else 1
        for attempt in range(1, attempts + 1):
            try:
                r = await self._execute_batchexecute(
                    rpc_id="as29s",
                    inner_payload=[media_id],
                    source_path=f"/project/{self.project_id}",
                    timeout_ms=30000,
                )
                rpc_result = r.get("rpc_result")
                if rpc_result:
                    import re
                    found_urls = re.findall(r'https://flow-content\.google/[^\s",\']+', json.dumps(rpc_result))
                    if for_video:
                        video_urls = [u for u in found_urls if "/video/" in u]
                        if video_urls:
                            url = video_urls[0]
                            log.info(f"[{self._account_email}] (BOQ) Got signed VIDEO download URL (attempt {attempt}): {url[:80]}...")
                            return url
                        log.warning(
                            f"[{self._account_email}] (BOQ) as29s attempt {attempt}/{attempts}: "
                            f"found image URL but /video/ URL not ready yet"
                        )
                    elif found_urls:
                        url = found_urls[0]
                        log.info(f"[{self._account_email}] (BOQ) Got signed download URL: {url[:80]}...")
                        return url
                log.warning(f"[{self._account_email}] (BOQ) as29s returned no URL (attempt {attempt}): {rpc_result}")
            except Exception as e:
                log.warning(f"[{self._account_email}] (BOQ) get_download_url error (attempt {attempt}): {e}")

            if attempt < attempts:
                await asyncio.sleep(2)

        return None

    async def _fetch_mp4_via_browser(self, media_id: str) -> Optional[bytes]:
        """Fetch generated video bytes via signed URL and proxy fetch (Zero DOM).

        1. Query signed CDN video URL via as29s RPC and fetch via bridge proxy_fetch_binary
        2. Fallback to labs.google trpc redirect if as29s doesn't give a video URL
        """
        # Method 1: get signed /video/ URL via as29s + extension proxy
        download_url = await self.get_download_url(media_id, for_video=True)
        if download_url:
            try:
                status, body, _ = await bridge.proxy_fetch_binary(
                    url=download_url,
                    method="GET",
                    timeout_ms=300000,
                )
                if status == 200 and body and len(body) > 100_000:
                    if body.startswith(b"\xff\xd8\xff"):
                        log.warning(f"(bridge) _fetch_mp4_via_browser: Got JPEG image instead of MP4 video!")
                    else:
                        log.info(f"(bridge) _fetch_mp4_via_browser OK via as29s: {len(body)} bytes (MP4)")
                        return body
                log.warning(f"(bridge) _fetch_mp4_via_browser status={status}, size={len(body) if body else 0}")
            except Exception as e:
                log.warning(f"(bridge) _fetch_mp4_via_browser error on signed URL: {e}")

        # Method 3: fallback to labs.google trpc redirect
        trpc_url = f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={media_id}"
        try:
            status, body, resp_headers = await bridge.proxy_fetch_binary(
                url=trpc_url,
                method="GET",
                timeout_ms=300000,
            )
            if status == 200 and body and len(body) > 100_000 and not body.startswith(b"\xff\xd8\xff"):
                log.info(f"(bridge) _fetch_mp4_via_browser OK via trpc: {len(body)} bytes")
                return body
        except Exception as e:
            log.error(f"(bridge) _fetch_mp4_via_browser trpc error: {e}")

        return None

    async def download_video(self, url: str, output_path: str) -> bool:
        """Download mp4 from a signed URL via bridge (preserves cookies
        for any subsequent auth redirect)."""
        if not url:
            return False
        try:
            status, body, _ = await bridge.proxy_fetch_binary(
                url=url, method="GET", timeout_ms=300000,
            )
            if status == 200 and body and len(body) > 100_000:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(body)
                log.info(f"(bridge) Downloaded video: {output_path} ({len(body)} bytes)")
                return True
            log.warning(f"(bridge) download_video: status={status}, size={len(body) if body else 0}")
            return False
        except Exception as e:
            log.error(f"(bridge) download_video error: {e}")
            return False

    async def download_image(self, url: str, output_path: str, raw_bytes: bytes | None = None) -> bool:
        """Download generated image from URL via bridge or save pre-fetched raw bytes."""
        if raw_bytes and len(raw_bytes) > 100:
            try:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(raw_bytes)
                log.info(f"(bridge) Saved pre-fetched image: {output_path} ({len(raw_bytes)} bytes)")
                return True
            except Exception as e:
                log.warning(f"(bridge) Direct write failed: {e}")

        if not url:
            return False
        try:
            status, body, _ = await bridge.proxy_fetch_binary(
                url=url, method="GET", timeout_ms=60000,
            )
            if status == 200 and body and len(body) > 100:
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(body)
                log.info(f"(bridge) Downloaded image: {output_path} ({len(body)} bytes)")
                return True
            log.warning(f"(bridge) download_image: status={status}, size={len(body) if body else 0}")
            return False
        except Exception as e:
            log.error(f"(bridge) download_image error: {e}")
            return False

    # ── Lifecycle ──────────────────────────────────────────────────

    async def close(self) -> None:
        """No-op — bridge is a shared module-level singleton, nothing
        to dispose per-client."""
        return
