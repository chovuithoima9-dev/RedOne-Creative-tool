"""Bridge endpoints between RedOne backend and the Chrome extension.

The extension polls these endpoints to pick up tasks the backend wants
performed inside the user's real Chrome (the win: requests originate from
the same browser fingerprint Google has been serving normally, so
reCAPTCHA scores stay high + 403 cascades dry up).

Protocol summary:

    GET  /sync/status                       heartbeat / ping
    GET  /sync/next-task?tab_status=...     extension polls for next task
    POST /sync/task-result                  extension submits result

Payload envelope: `{ "d": "<hex>" }` where the inner JSON is XOR'd byte-by-byte
with 0x5A. Lightweight obfuscation only — NOT security.

Tasks supported:
    - "recaptcha":     site_key + action → token
    - "proxy_fetch":   url + method + headers + body → {status, headers, body}

Tasks are produced by `BrowserBridge.harvest_recaptcha()` /
`BrowserBridge.proxy_fetch()` (see services/browser_bridge.py). Each task
lives in the queue until either the extension picks it up + posts a result,
or it expires after `TASK_TTL_S`.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Optional, Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("redone.sync")
router = APIRouter(prefix="/sync", tags=["sync-bridge"])


XOR_KEY = 0x5A
TASK_TTL_S = 120.0


# ── XOR codec (parity with extension/background.js) ─────────────────

def xor_encode(plaintext: str) -> str:
    """Encode a string as hex, each byte XOR'd with XOR_KEY.

    To stay compatible with the extension's JS encoder, non-ASCII chars
    are first escaped as `\\uXXXX` sequences so we only XOR over a
    well-defined byte stream.
    """
    out_chars: list[str] = []
    for ch in plaintext:
        code = ord(ch)
        if code < 0x20 or code > 0x7E:
            out_chars.append("\\u" + format(code, "04x"))
        else:
            out_chars.append(ch)
    ascii_str = "".join(out_chars)
    return "".join(format(ord(c) ^ XOR_KEY, "02x") for c in ascii_str)


def xor_decode(hex_string: str) -> str:
    """Reverse of `xor_encode`. The `\\uXXXX` escapes are left as-is in
    the result — JSON parsers handle them natively."""
    result_chars: list[str] = []
    for i in range(0, len(hex_string), 2):
        byte = int(hex_string[i:i + 2], 16) ^ XOR_KEY
        result_chars.append(chr(byte))
    return "".join(result_chars)


def envelope(payload: dict) -> dict:
    """Wrap a payload in `{d: <xor-hex>}` envelope for sending to the
    extension. The extension's decoder pulls `d`, decodes it, and
    `JSON.parse`s the result."""
    return {"d": xor_encode(json.dumps(payload, ensure_ascii=True))}


def unwrap(raw: Any) -> dict:
    """Inverse of envelope — accepts either an envelope dict
    `{d: <hex>}` or a plain dict and returns the inner payload."""
    if isinstance(raw, dict) and isinstance(raw.get("d"), str):
        decoded = xor_decode(raw["d"])
        return json.loads(decoded)
    if isinstance(raw, dict):
        return raw
    raise ValueError("expected JSON object or envelope dict")


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/status")
async def status():
    """Heartbeat — extension hits this every ~15s to keep its connection
    indicator green. Also useful for backend → extension liveness checks
    via the ext popup's metrics display.

    IMPORTANT: also bumps `_ext_last_poll` so the bridge's
    `is_extension_live()` check stays True even when the ext is busy
    running a long proxy_fetch (during which it doesn't hit
    /sync/next-task). Without this, large file uploads would falsely
    trip "Extension offline" on concurrent tasks.
    """
    from ..services.browser_bridge import bridge
    # Don't overwrite tab_status here — we don't know it. Just mark live.
    bridge.bump_liveness()
    return {"ok": True, "ts": time.time(), "service": "redone-bridge"}


class TaskResultBody(BaseModel):
    # Envelope-wrapped or plain — we accept both for flexibility.
    d: Optional[str] = None
    task_id: Optional[str] = None
    kind: Optional[str] = None
    result: Optional[dict] = None


@router.get("/next-task")
async def next_task(
    request: Request,
    tab_status: str = "ready",
    tab_url: str = "",
    tab_email: str = "",
    tab_tier: str = "",
    tab_credits: str = "",
    capacity: int = 1,
):
    """Extension short-polls (~1.5s) for the next task. Returns
    `{task: null}` if the queue is empty OR the extension has no spare
    capacity right now.

    The `tab_status` query string is the extension reporting back what
    it can currently do:
        - "ready"     — at least one labs.google tab is open and signed in
        - "no_tab"    — no labs.google tab open
        - "no_login"  — tab is open but user isn't signed into Google

    `capacity` is how many MORE tasks the extension can run concurrently
    (MAX_CONCURRENT − in-flight). The extension now dispatches tasks in
    parallel instead of one-at-a-time, so it keeps polling (to refresh
    tab_status) even while busy but sends `capacity=0` — we must NOT hand
    out a task it can't run yet, or that task would sit in `_in_flight`
    and time out. Defaults to 1 so an OLD (pre-parallel) extension that
    doesn't send the param still gets one task per poll as before.

    Backend tracks the last-seen tab status so other routers (accounts,
    settings) can show a meaningful "Extension is offline / no tab open"
    banner without needing to poll the extension themselves.
    """
    from ..services.browser_bridge import bridge
    from ..database import db
    from ..ws_hub import hub

    parsed_credits = None
    if tab_credits:
        try:
            parsed_credits = int(tab_credits)
        except Exception:
            pass

    ua = request.headers.get("user-agent", "")
    if ua and not hasattr(bridge, "_cached_ua"):
        bridge._cached_ua = ua
        log.info(f"[SYNC] Detected extension browser User-Agent: {ua}")

    bridge.update_tab_state(
        tab_status, tab_url, email=tab_email, tier=tab_tier, credits=parsed_credits
    )

    # If extension identified a valid Google account on the active Flow tab,
    # auto-register or re-enable it in DB so tasks immediately use it.
    if tab_email and "@" in tab_email:
        clean_email = tab_email.strip().lower()
        try:
            acc = db.get_account_by_email(clean_email)
            update_kwargs = {}
            if tab_tier and tab_tier.upper() in ("ULTRA", "PRO", "FREE"):
                update_kwargs["tier"] = tab_tier.upper()
            if parsed_credits is not None:
                update_kwargs["credit"] = parsed_credits

            if not acc:
                acc_id = db.add_account(clean_email)
                if update_kwargs:
                    db.update_account(acc_id, **update_kwargs)
                acc = db.get_account(acc_id)
                log.info(
                    f"Auto-registered active Google Flow account from tab: {clean_email} "
                    f"(id={acc_id}, tier={update_kwargs.get('tier', 'FREE')}, credits={parsed_credits})"
                )
                await hub.broadcast("account_added", {
                    "id": acc_id,
                    "email": clean_email,
                    "tier": update_kwargs.get("tier", "FREE"),
                    "credit": parsed_credits or 0,
                    "enabled": True,
                })
            else:
                need_update = False
                if not acc.get("enabled"):
                    update_kwargs["enabled"] = 1
                    need_update = True
                
                incoming_tier = tab_tier.upper() if tab_tier else ""
                current_tier = (acc.get("tier") or "FREE").upper()
                current_credits = acc.get("credit") or 0
                if incoming_tier:
                    if incoming_tier in ("ULTRA", "PRO") and incoming_tier != current_tier:
                        update_kwargs["tier"] = incoming_tier
                        need_update = True
                    elif incoming_tier == "FREE" and current_tier in ("ULTRA", "PRO"):
                        # If credits >= 500, it is guaranteed a paid Ultra/Pro subscription. Never downgrade to FREE.
                        effective_credits = parsed_credits if parsed_credits is not None else current_credits
                        if effective_credits < 500:
                            update_kwargs["tier"] = "FREE"
                            need_update = True
                    elif incoming_tier != current_tier:
                        update_kwargs["tier"] = incoming_tier
                        need_update = True

                if parsed_credits is not None and acc.get("credit") != parsed_credits:
                    need_update = True

                if need_update and update_kwargs:
                    db.update_account(acc["id"], **update_kwargs)
                    updated_acc = db.get_account(acc["id"])
                    log.info(
                        f"Updated active Flow account: {clean_email} -> tier={updated_acc.get('tier')}, "
                        f"credits={updated_acc.get('credit')}"
                    )
                    await hub.broadcast("account_updated", {
                        "id": acc["id"],
                        "email": clean_email,
                        "tier": updated_acc.get("tier"),
                        "credit": updated_acc.get("credit"),
                        "enabled": bool(updated_acc.get("enabled")),
                    })
        except Exception as ex:
            log.warning(f"Error auto-syncing tab account {clean_email}: {ex}")

    # Always pop pending session commands — even when no task is ready or at capacity.
    # This lets the extension execute commands (clear_cookies, reload, etc.)
    # independently of the task queue, on every poll cycle.
    commands = bridge.pop_session_commands()

    # At capacity → keep the heartbeat/tab_status fresh but claim no new tasks.
    if capacity <= 0:
        if commands:
            return envelope({"task": None, "session_commands": commands})
        return {"task": None}

    # Pass tab_status and tab_email so the bridge only hands tasks to a "ready" instance
    # belonging to the target account. Lets the extension run in multiple Chrome
    # profiles without collision or cross-account task execution.
    task = await bridge.pop_task_for_extension(timeout=0.0, tab_status=tab_status, tab_email=tab_email)
    if task is None:
        if commands:
            return envelope({"task": None, "session_commands": commands})
        return {"task": None}
    response = {"task": task.to_dict()}
    if commands:
        response["session_commands"] = commands
    return envelope(response)


@router.post("/task-result")
async def task_result(request: Request):
    """Extension delivers a result for a task it previously claimed.

    The body is envelope-wrapped (`{d: <hex>}`); we unwrap it before
    handing the result back to the awaiting BrowserBridge future.
    """
    from ..services.browser_bridge import bridge

    raw = await request.json()
    try:
        payload = unwrap(raw)
    except Exception as e:
        raise HTTPException(400, f"bad envelope: {e}")

    task_id = payload.get("task_id")
    result = payload.get("result") or {}
    if not task_id:
        raise HTTPException(400, "missing task_id")

    delivered = bridge.deliver_result(task_id, result)
    if not delivered:
        # Either expired or never existed. Log but don't error — the ext
        # may have raced with an internal timeout and that's recoverable.
        log.debug(f"task-result for unknown/expired task_id={task_id}")
        return {"ok": False, "reason": "task expired or unknown"}
    return {"ok": True}


@router.post("/shakker-account")
async def shakker_account_sync(request: Request):
    """Extension forwards shakker.ai auth state here.

    Payload is the envelope-wrapped `{user_uuid, token, email?, user_id?,
    account_id?, webid?}` object. Upserts into `shakker_accounts` table
    keyed by `user_uuid`.

    Bypasses the OAuth auth gate (lives under `/sync/`) for the same
    reason the Flow bridge endpoints do — extensions can't carry the
    @redone.vn OAuth session, and the local-only bind already constrains
    callers to processes on this machine.
    """
    from datetime import datetime
    from ..database import db

    raw = await request.json()
    try:
        payload = unwrap(raw)
    except Exception as e:
        raise HTTPException(400, f"bad envelope: {e}")

    user_uuid = (payload.get("user_uuid") or "").strip()
    token = (payload.get("token") or "").strip()
    if not user_uuid or not token:
        raise HTTPException(400, "user_uuid and token required")

    fields = {
        "token": token,
        "status": "OK",
        "status_msg": None,
        "last_check_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    if payload.get("email"):
        fields["email"] = str(payload["email"]).strip().lower()
    if isinstance(payload.get("user_id"), int):
        fields["user_id"] = payload["user_id"]
    if isinstance(payload.get("account_id"), int):
        fields["account_id"] = payload["account_id"]
    if payload.get("webid"):
        fields["webid"] = str(payload["webid"])

    acc_id = db.upsert_shakker_account(user_uuid, **fields)
    log.info(
        f"/sync/shakker-account upserted id={acc_id} uuid={user_uuid[:8]}…"
        f" email={fields.get('email', '-')}"
    )

    # Notify the UI so any open Shakker account list refreshes instantly.
    try:
        from ..ws_hub import hub
        await hub.broadcast(
            "shakker_account_synced",
            {"id": acc_id, "user_uuid": user_uuid},
        )
    except Exception:
        pass

    # Auto-share to the team: if this machine's tool user is a lead/admin, push
    # the just-captured token to the Hub as the team's shared Shakker token, so
    # members get it on their next login. The lead does nothing beyond logging
    # into shakker.ai. Best-effort.
    try:
        from ..services import hub_client
        await hub_client.maybe_push_shared_shakker_token(token)
    except Exception:
        pass

    return {"ok": True, "id": acc_id}


def _is_foreign_web_origin(request: Request) -> bool:
    """True only when the request carries a foreign http(s) Origin — i.e. a real
    website doing a cross-origin fetch. The extension's service-worker fetch
    sends no Origin (or a chrome-extension:// one), so it is never flagged.
    Used to refuse handing the shared credentials to a drive-by web page even if
    CORS were ever misconfigured (security review C1, defense in depth)."""
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin or origin.startswith("chrome-extension://"):
        return False
    if not origin.startswith(("http://", "https://")):
        return False  # "null", file://, etc. — not the drive-by web vector
    from ..config import SERVER_PORT
    allowed = {f"http://127.0.0.1:{SERVER_PORT}", f"http://localhost:{SERVER_PORT}"}
    return origin not in allowed


@router.get("/shared-google")
async def shared_google_login(request: Request):
    """Return the team's shared Google account (email+password) for the
    extension to auto-fill the accounts.google.com login form. Fetched from
    the Hub (decrypted) using this machine's Hub session.

    Localhost-only + bypasses the OAuth gate like the other /sync endpoints
    (same local-origin trust model). Envelope-obfuscated in transit. The
    member never sees this in any UI; only the extension consumes it."""
    # CORS already blocks a cross-origin site from READING the response; this is
    # the second layer — never even emit the password to a foreign web origin.
    if _is_foreign_web_origin(request):
        raise HTTPException(403, "forbidden origin")
    from ..services import hub_client
    try:
        creds = await hub_client.get_shared_credentials()
    except Exception as e:
        log.debug("shared-google fetch failed: %s", e)
        creds = None
    if not creds or not (creds.get("google_email") and creds.get("google_password")):
        return envelope({"ok": False, "reason": "Chưa có tài khoản chung trong Hub"})
    return envelope({
        "ok": True,
        "email": creds.get("google_email"),
        "password": creds.get("google_password"),
    })


@router.get("/state")
async def bridge_state():
    """Diagnostic — what does the bridge currently know? Used by the
    frontend Settings page to show "Extension connected / disconnected"
    chip.
    """
    from ..services.browser_bridge import bridge
    return bridge.snapshot_state()


@router.get("/debug-ua")
async def debug_ua():
    from ..services.browser_bridge import bridge
    return {
        "cached_ua": getattr(bridge, "_cached_ua", None),
        "bridge_email": bridge.get_active_account_email(),
        "bridge_tier": bridge.get_active_account_tier(),
        "bridge_credits": bridge.get_active_account_credits(),
    }







