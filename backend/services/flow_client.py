"""RedOne — Google Flow AI Sandbox API client.

Auth: Playwright → /fx/api/auth/session → ya29.* access_token
API:
  - Upload:   POST aisandbox-pa.googleapis.com/v1/flow/uploadImage
  - Generate: POST aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideo*
  - Poll:     POST aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus
  - Download: tRPC media.getMediaUrlRedirect
Flow: get_token → upload_images → generate → poll → download
"""

import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

import logging
log = logging.getLogger("redone.flow")

from ..config import (
    AISANDBOX_BASE, X_CLIENT_DATA, X_BROWSER_VALIDATION,
    POLL_INTERVAL_SECONDS, MAX_RETRY_COUNT
)

# Reference images can be huge (e.g. 17-19MB video frames). Sending 20MB+ as
# base64 through the extension bridge (per-char XOR envelope + executeScript
# arg) times out. Shrink anything over the budget to a sane size before upload;
# small images pass through untouched so existing flows are unaffected.
_UPLOAD_MAX_BYTES = 2_000_000
_UPLOAD_MAX_SIDE = 1920


def _shrink_image_for_upload(path: "Path") -> "tuple[bytes, str]":
    """Return (bytes, mime) for uploadImage. Downscales + re-encodes only when
    the file exceeds the budget; otherwise returns the original bytes. Never
    raises — on any failure it falls back to the original file."""
    raw = path.read_bytes()
    base_mime = {
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(path.suffix.lower(), "image/jpeg")
    if len(raw) <= _UPLOAD_MAX_BYTES:
        return raw, base_mime
    try:
        import io
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
        w, h = img.size
        scale = min(1.0, _UPLOAD_MAX_SIDE / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        buf = io.BytesIO()
        if has_alpha:
            img.convert("RGBA").save(buf, format="PNG", optimize=True)
            out, mime = buf.getvalue(), "image/png"
        else:
            img.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True)
            out, mime = buf.getvalue(), "image/jpeg"
        log.info(f"Shrunk upload {path.name}: {len(raw)//1024}KB → {len(out)//1024}KB "
                 f"({w}x{h} → {img.size[0]}x{img.size[1]})")
        return out, mime
    except Exception as e:
        log.warning(f"Image shrink failed for {path.name} ({e}); uploading original")
        return raw, base_mime


# Per-account reCAPTCHA lock
_RECAPTCHA_LOCKS: dict[str, asyncio.Lock] = {}

def _get_recaptcha_lock(account_email: str) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    key = f"{loop_id}:{account_email or '?'}"
    if key not in _RECAPTCHA_LOCKS:
        _RECAPTCHA_LOCKS[key] = asyncio.Lock()
    return _RECAPTCHA_LOCKS[key]


class SessionDeadError(Exception):
    """Raised when the labs.google session has expired and re-login is required.

    Detected via:
      - /fx/api/auth/session returning 404
      - aisandbox-pa API returning 401 "Request is missing required authentication credential"
      - page URL containing accounts.google.com/signin

    Routers catch this and disable the offending account + notify the UI.
    """
    def __init__(self, email: str, reason: str = "Session hết hạn"):
        self.email = email
        self.reason = reason
        super().__init__(f"[{email}] {reason}")


class FlowClient:
    """Google Flow API client.

    All API calls go through browser fetch (page.evaluate) for proper
    session handling, CORS, and reCAPTCHA bypass.
    """

    TRPC = "https://labs.google/fx/api/trpc"
    SESSION_URL = "https://labs.google/fx/api/auth/session"
    
    # Persistent project_id per account — so extend/upscale finds media created by generate
    _PROJECT_IDS: dict[str, str] = {}
    
    def __init__(self, page: "Page", cookie_path: str = "", account_email: str = ""):
        self._page = page
        self._token: Optional[str] = None
        self._token_lock: Optional[asyncio.Lock] = None
        self._cookie_path = cookie_path
        self._account_email = account_email or "?"
        self._last_remaining_credits = None
        self._last_credit_cost = 0
        self._last_model_key = None
        self._recaptcha_provider = None
        self._recaptcha_fail_count = 0
        # Per-task cache of uploaded reference images → media_id. The same
        # reference image is otherwise re-uploaded once per prompt: I2I shares
        # the same N reference images across EVERY prompt (dozens of duplicate
        # uploads of the same files), and I2V re-uploads on every "Gen lại".
        # Keyed by (resolved path, mtime, size) so a replaced file re-uploads;
        # different files (different paths) never collide, so I2V's
        # one-image-per-prompt mapping is preserved exactly. Scoped to this
        # client instance (one client per task) → no stale id leaks across
        # tasks. `_upload_locks` serializes concurrent uploads of the SAME
        # file within a parallel batch so each image is uploaded exactly once.
        self._uploaded_media: dict[str, str] = {}
        self._upload_locks: dict[str, asyncio.Lock] = {}
        # Use persistent project_id per account (not random each time!)
        if account_email not in FlowClient._PROJECT_IDS:
            FlowClient._PROJECT_IDS[account_email] = str(uuid.uuid4())
        self.project_id = FlowClient._PROJECT_IDS[account_email]

    def _get_token_lock(self) -> asyncio.Lock:
        """Lazy-init the token lock (must run in an event loop)."""
        if self._token_lock is None:
            self._token_lock = asyncio.Lock()
        return self._token_lock

    # ── Subprocess reCAPTCHA provider (DISABLED) ──────────────────
    # Earlier in this session we tried wiring a SubprocessTokenProvider
    # pattern (separate CloakBrowser instance harvesting tokens) to reduce
    # 403 cascades. In practice it made things worse: the headless Cloak
    # harvester had a different fingerprint from the main visible Cloak
    # session, so Google flagged the mismatch immediately and — because
    # the harvester was cached as a singleton per account — that flagged
    # state then poisoned every subsequent task on the account.
    #
    # The code in `recaptcha_provider.py` is kept as-is (dead code) so we
    # can revisit when we figure out how to match fingerprints. For now,
    # `ensure_provider()` is a no-op and `get_recaptcha_token()` uses the
    # in-page harvest path only.

    async def ensure_provider(self) -> None:
        """No-op. Subprocess harvester disabled — see comment above."""
        return

    async def close(self) -> None:
        """No-op cleanup hook. Kept so callers in routers don't need
        conditional checks; will become useful again if/when we re-enable
        the subprocess harvester."""
        return

    # ── Session & Auth ────────────────────────────────────────────
    
    async def ensure_token(self):
        """Get ya29.* access token from NextAuth session.

        Concurrent callers serialize on a lock so we only navigate once even
        when multiple parallel items boot together with no cached token.
        """
        if self._token:
            return

        async with self._get_token_lock():
            # Recheck after acquiring lock — another caller may have set it
            if self._token:
                return
            await self._do_get_token()

    async def _do_get_token(self):
        log.info(f"[{self._account_email}] Getting session token...")
        
        # Navigate to flow.google.com or labs.google if not already there
        current_url = self._page.url or ""
        if "labs.google/fx/" not in current_url and "flow.google.com" not in current_url:
            try:
                await self._page.goto("https://flow.google.com", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                if "interrupted by another navigation" in str(e):
                    pass
                else:
                    raise
            await asyncio.sleep(2)
        
        # Wait for page to settle after any redirects
        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            await asyncio.sleep(3)
        
        # Click Sign In if visible
        try:
            btn = await self._page.query_selector(
                'a[href*="accounts.google.com"], button:has-text("Sign in"), a:has-text("Sign in")'
            )
            if btn and await btn.is_visible():
                log.info("Clicking Sign in...")
                ctx = self._page.context
                try:
                    async with ctx.expect_page(timeout=15000) as pi:
                        await btn.click()
                    popup = await pi.value
                    try:
                        await popup.wait_for_event("close", timeout=20000)
                    except Exception:
                        await asyncio.sleep(5)
                except Exception:
                    pass
                await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"[{self._account_email}] Sign-in button check skipped: {e}")
        
        # Fetch session token via browser — try labs.google session endpoint
        # first (NextAuth), then try flow.google.com internal endpoint
        result = await self._page.evaluate("""
            async () => {
                try {
                    // Try labs.google session endpoint first (cross-origin with cookies)
                    let r = await fetch("https://labs.google/fx/api/auth/session", {credentials: "include"});
                    if (r.ok) {
                        const data = await r.json();
                        if (data && data.access_token) return data;
                    }
                    // Fallback: try same-origin on current domain
                    r = await fetch("/fx/api/auth/session");
                    if (r.ok) return await r.json();
                    return {error: r.status};
                } catch(e) { return {error: e.message}; }
            }
        """)
        
        if result and result.get("access_token"):
            self._token = result["access_token"]
            log.info(f"[{self._account_email}] Got token: ya29...{self._token[-8:]}")
        else:
            log.error(f"[{self._account_email}] Failed to get token: {result}")
            # 404 from /fx/api/auth/session = no NextAuth session = SIGNED OUT
            err_val = (result or {}).get("error") if isinstance(result, dict) else None
            if err_val == 404 or err_val == "404":
                raise SessionDeadError(
                    self._account_email,
                    "Session Google đã hết hạn hoặc bị đăng xuất. Vui lòng login lại account này.",
                )
            # Also check if the page got redirected to sign-in
            try:
                current_url = self._page.url or ""
                if "accounts.google.com" in current_url and "signin" in current_url.lower():
                    raise SessionDeadError(
                        self._account_email,
                        "Tài khoản đã bị Google đăng xuất. Vui lòng login lại.",
                    )
            except SessionDeadError:
                raise
            except Exception:
                pass
    
    async def renew_token(self, bad_token: Optional[str] = None):
        """Force refresh of session token AND reset reCAPTCHA risk score.

        Uses `page.reload()` (full F5) rather than `goto()` — reload wipes the
        page's per-session reCAPTCHA history more reliably than goto, which
        Google's bot detector may treat as just another navigation.
        """
        log.info(f"[{self._account_email}] Renewing session token (page reload)...")
        self._token = None
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.5)
        except Exception as e:
            log.warning(f"[{self._account_email}] reload failed during token renewal: {e}")
            # Fallback: try goto if reload errored
            try:
                await self._page.goto("https://flow.google.com",
                                       wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2.0)
            except Exception as e2:
                log.warning(f"[{self._account_email}] goto fallback also failed: {e2}")
        await self.ensure_token()
    
    # ── tRPC API ─────────────────────
    
    async def _api(self, procedure: str, payload: dict, method: str = "POST") -> dict:
        """Call tRPC endpoint using Playwright HTTP client."""
        await self.ensure_token()
        token = self._token or ""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "x-client-data": X_CLIENT_DATA,
            "x-browser-validation": X_BROWSER_VALIDATION,
        }
        try:
            if method == "GET":
                import urllib.parse
                inp = json.dumps({"json": payload})
                url = f"{self.TRPC}/{procedure}?input={urllib.parse.quote(inp)}"
                resp = await self._page.request.get(url, headers=headers)
            else:
                url = f"{self.TRPC}/{procedure}"
                body = json.dumps({"json": payload})
                resp = await self._page.request.post(url, headers=headers, data=body)
            if resp.ok:
                return await resp.json()
            text = await resp.text()
            return {"error": resp.status, "text": text[:500]}
        except Exception as e:
            return {"error": str(e)}
    
    def _extract(self, result: dict):
        """Extract result.data.json.result from tRPC response."""
        return result.get("result", {}).get("data", {}).get("json", {}).get("result", {})
    
    # ── Browser Sandbox Request ──────
    
    async def _browser_sandbox_request(self, endpoint: str, payload: dict, is_text_plain: bool = False) -> dict:
        """Send request via browser fetch — needed for reCAPTCHA-protected APIs.
        
        This is the CORE method. All aisandbox API calls go through here.
        Uses page.evaluate(fetch()) to inherit browser cookies + session.
        """
        await self.ensure_token()
        token = self._token or ""
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        body_json = json.dumps(payload)
        
        for attempt in range(1, MAX_RETRY_COUNT + 1):
            try:
                result = await self._page.evaluate("""async ([url, token, body, is_text_plain]) => {
                    try {
                        const headers = {
                            "Authorization": "Bearer " + token,
                            "Content-Type": is_text_plain ? "text/plain;charset=UTF-8" : "application/json",
                        };
                        const r = await fetch(url, {
                            method: "POST",
                            headers: headers,
                            body: body,
                        });
                        const text = await r.text();
                        try {
                            return {status: r.status, ok: r.ok, data: JSON.parse(text)};
                        } catch(e) {
                            return {status: r.status, ok: r.ok, text: text.substring(0, 2000)};
                        }
                    } catch(e) {
                        return {error: e.message};
                    }
                }""", [url, token, body_json, is_text_plain])
                
                if not result:
                    return {"error": "No result from browser fetch"}
                
                if result.get("error"):
                    log.warning(f"Browser sandbox {endpoint} attempt {attempt}: {result['error']}")
                    if attempt < MAX_RETRY_COUNT:
                        await asyncio.sleep(2 * attempt)
                        continue
                    return {"error": result["error"]}
                
                status = result.get("status", 0)
                
                # 401/403 → renew token and retry. After MAX_RETRY_COUNT failures
                # on 401, it's almost certainly a dead session (NextAuth wiped),
                # not just a stale token → raise SessionDeadError so the router
                # can disable the account + notify UI.
                if status in (401, 403) and attempt < MAX_RETRY_COUNT:
                    err_text = result.get("text", "") or json.dumps(result.get("data", {}))[:200]
                    log.warning(f"Browser sandbox {endpoint}: HTTP {status}: {err_text[:100]}")
                    if "recaptcha" in err_text.lower() or "unusual_activity" in err_text.lower():
                        return {"error": f"HTTP {status}", "text": err_text}
                    try:
                        await self.renew_token(self._token)
                    except SessionDeadError:
                        raise
                    token = self._token or ""
                    await asyncio.sleep(1)
                    continue

                # Persistent 401 after all retries = session dead
                if status == 401 and attempt >= MAX_RETRY_COUNT:
                    err_text = result.get("text", "")
                    if (
                        "missing required authentication" in err_text.lower()
                        or "unauthenticated" in err_text.lower()
                    ):
                        raise SessionDeadError(
                            self._account_email,
                            "Server Google trả 401 sau nhiều lần thử — session đã chết, hãy login lại.",
                        )
                
                if result.get("ok"):
                    return result.get("data", {})
                
                err = result.get("text", json.dumps(result.get("data", {})))[:300]
                log.warning(f"Browser sandbox {endpoint} HTTP {status}: {err}")
                
                if attempt < MAX_RETRY_COUNT and status >= 500:
                    await asyncio.sleep(2 * attempt)
                    continue
                
                return {"error": f"HTTP {status}", "text": err}
                
            except Exception as e:
                log.error(f"Browser sandbox {endpoint} exception: {e}")
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2)
                    continue
                return {"error": str(e)}
        
        return {"error": "Max retries exceeded"}

    async def _sandbox_request(self, endpoint: str, payload: dict | str, method: str="POST") -> dict:
        """Call aisandbox-pa.googleapis.com endpoint with httpx to bypass CORS."""
        import httpx
        await self.ensure_token()
        token = self._token or ""
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        
        body_bytes = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "x-client-data": X_CLIENT_DATA,
            "x-browser-validation": X_BROWSER_VALIDATION
        }
        
        for attempt in range(1, MAX_RETRY_COUNT + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(url, headers=headers, content=body_bytes)
                    status = resp.status_code
                    
                    if status in (401, 403) and attempt < MAX_RETRY_COUNT:
                        log.warning(f"Sandbox {endpoint}: HTTP {status}, renewing token...")
                        await self.renew_token(self._token)
                        headers["Authorization"] = f"Bearer {self._token}"
                        continue
                        
                    if 200 <= status < 300:
                        try:
                            return resp.json()
                        except Exception:
                            return {"text": resp.text[:2000]}
                            
                    err_text = resp.text[:300]
                    log.warning(f"Sandbox {endpoint} HTTP {status}: {err_text}")
                    
                    if attempt < MAX_RETRY_COUNT and status >= 500:
                        await asyncio.sleep(2 * attempt)
                        continue
                        
                    return {"error": status, "text": err_text}
            except Exception as e:
                log.error(f"Sandbox {endpoint} attempt {attempt} exception: {e}")
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2 * attempt)
                    
        return {"error": "Max retries exceeded"}
    
    # ── Upload Image ─────────────────────
    
    async def upload_image(self, image_path: str) -> Optional[str]:
        """Upload a reference image to Google Flow, return its media_id.

        Cached + deduped: the same file is uploaded ONCE per task (see the
        `_uploaded_media` / `_upload_locks` comment in __init__). A cache hit
        returns the stored media_id without touching the network; concurrent
        callers for the same file serialize on a per-file lock so a parallel
        batch (e.g. I2I sharing 3 refs across many prompts) uploads each image
        exactly once. The actual upload lives in `_upload_image_raw`.
        """
        path = Path(image_path)
        if not path.exists():
            log.error(f"Image not found: {image_path}")
            return None

        # Cache key: path + mtime + size so a replaced file re-uploads, while
        # distinct files (distinct paths) never share a media_id.
        try:
            stt = path.stat()
            key = f"{path.resolve()}::{int(stt.st_mtime)}::{stt.st_size}"
        except OSError:
            key = str(path.resolve())

        cached = self._uploaded_media.get(key)
        if cached:
            log.info(f"[{self._account_email}] Upload cache hit: {path.name} → {cached}")
            return cached

        # Serialize concurrent uploads of the SAME file (parallel batch).
        lock = self._upload_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._upload_locks[key] = lock

        async with lock:
            # Another concurrent caller may have uploaded it while we waited.
            cached = self._uploaded_media.get(key)
            if cached:
                log.info(
                    f"[{self._account_email}] Upload cache hit (post-lock): "
                    f"{path.name} → {cached}"
                )
                return cached

            media_id = await self._upload_image_raw(path)
            if media_id:
                self._uploaded_media[key] = media_id
            return media_id

    async def _upload_image_raw(self, path: "Path") -> Optional[str]:
        """Do the actual base64 POST to /flow/uploadImage (no caching).

        Returns the 'name' field (UUID/media_id) from the response, or None
        on failure. Raises ValueError on a server-side upload error so the
        caller can surface a friendly message.
        """
        # Read + (if oversized) shrink, then base64-encode. Large refs (e.g.
        # 17-19MB video frames) otherwise time out through the bridge. Runs in
        # a thread so the resize doesn't block concurrent uploads.
        raw, mime = await asyncio.to_thread(_shrink_image_for_upload, path)
        b64 = base64.b64encode(raw).decode("utf-8")

        payload = {
            "imageBytes": b64,
            "mimeType": mime,
            "clientContext": {
                "tool": "PINHOLE"
            }
        }

        log.info(f"[{self._account_email}] Uploading {path.name} ({len(raw)} bytes, {mime})...")
        result = await self._browser_sandbox_request("flow/uploadImage", payload)
        
        if "error" in result:
            err_msg = str(result.get("text", result.get("error", "")))
            log.error(f"[{self._account_email}] Upload failed for {path.name}: {result}")
            raise ValueError(f"Upload lỗi: {err_msg}")
        
        # Extract media_id from response (uses 'name' field)
        media_id = result.get("name") or result.get("mediaId") or result.get("media_id")
        
        if not media_id:
            # Deep search in nested response
            for key in ["media", "image", "result", "response"]:
                nested = result.get(key, {})
                if isinstance(nested, dict):
                    media_id = nested.get("name") or nested.get("mediaId")
                    if media_id:
                        break
        
        if media_id:
            log.info(f"[{self._account_email}] Upload OK: {path.name} → {media_id}")
        else:
            log.error(f"[{self._account_email}] No media_id in upload response: {json.dumps(result)[:300]}")
        
        return media_id
    
    # ── Generate Image (Google Labs Image FX) ────────────────
    
    # Model name mapping for image generation
    IMAGE_MODEL_MAP = {
        "nano_banana_pro": "GEM_PIX_2",
        "nano_banana_2": "NARWHAL",
        # Confirmed via labs.google HAR 2026-07-20. Unlike Imagen, HARBOR_SEAL
        # keeps its own model name when reference images are attached (the
        # capture included one) — no R2I swap needed.
        "nano_banana_lite": "HARBOR_SEAL",
        "imagen_4": "IMAGEN_3_5",
    }
    
    IMAGE_ASPECT_RATIO_MAP = {
        "1:1": "IMAGE_ASPECT_RATIO_SQUARE",
        "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "3:4": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "4:3": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
    }
    
    async def generate_image(
        self,
        prompt: str,
        model_key: str = "nano_banana_pro",
        aspect_ratio: str = "1:1",
        reference_images: list[str] | None = None,
        seed: int | None = None,
    ) -> dict:
        """Generate an image using Google Labs Image FX API.
        
        Unlike video generation, image generation is SYNCHRONOUS —
        the response contains the image URL directly (no polling needed).
        
        Args:
            prompt: Text prompt for image generation
            model_key: Model key (nano_banana_pro, nano_banana_2, imagen_4)
            aspect_ratio: Aspect ratio (1:1, 16:9, 9:16, 3:4, 4:3)
            reference_images: List of media_ids from upload_image() for reference
            seed: Random seed (auto-generated if None)
            
        Returns:
            dict with keys: media_id, download_url, seed, width, height
        """
        await self.ensure_token()
        
        import random
        if seed is None:
            seed = random.randint(100000, 999999)
        
        # Resolve model name
        model_name = self.IMAGE_MODEL_MAP.get(model_key, model_key)
        # Imagen's text-to-image model (IMAGEN_3_5) rejects reference images with
        # a 400 INVALID_ARGUMENT. Google Flow itself switches to the "R2I"
        # (reference-to-image) model when an Imagen request carries reference
        # images — captured from the live Flow request. Mirror that exactly.
        # Nano Banana keeps its own model (its I2I already works as-is).
        if reference_images and model_key.startswith("imagen"):
            model_name = "R2I"

        # Resolve aspect ratio
        ar_enum = self.IMAGE_ASPECT_RATIO_MAP.get(aspect_ratio, "IMAGE_ASPECT_RATIO_SQUARE")
        
        # Build imageInputs
        image_inputs = []
        if reference_images:
            for media_id in reference_images:
                image_inputs.append({
                    "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                    "name": media_id,
                })
        
        # Get reCAPTCHA token
        recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")
        
        # Build client context
        client_context = {
            "projectId": self.project_id,
            "tool": "PINHOLE",
            "sessionId": f";{int(time.time() * 1000)}",
        }
        if recaptcha_token:
            client_context["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }
        
        # Build request
        request_item = {
            "clientContext": client_context,
            "imageModelName": model_name,
            "imageAspectRatio": ar_enum,
            "structuredPrompt": {
                "parts": [{"text": prompt}]
            },
            "seed": seed,
            "imageInputs": image_inputs,
        }
        
        payload = {
            "clientContext": client_context,
            "mediaGenerationContext": {
                "batchId": os.urandom(16).hex()[:8] + "-" + 
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(12).hex()[:12],
            },
            "useNewMedia": True,
            "requests": [request_item],
        }
        
        endpoint = f"projects/{self.project_id}/flowMedia:batchGenerateImages"
        
        log.info(f"[{self._account_email}] Generating image: model={model_name}, "
                 f"ratio={ar_enum}, refs={len(image_inputs)}, seed={seed}")
        
        # Retry loop (same pattern as generate_video — 5 attempts with
        # reload-on-403 to drop the per-session bot risk score).
        import random as _rand
        result = None
        for attempt in range(5):
            if attempt > 0:
                # Backoff before each retry
                await asyncio.sleep(_rand.uniform(1.5, 3.0))
                # Refresh reCAPTCHA token on retry
                recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")
                if recaptcha_token:
                    client_context["recaptchaContext"] = {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    }

            result = await self._browser_sandbox_request(endpoint, payload, is_text_plain=True)

            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                err_lc = err_str.lower()
                is_recaptcha = (
                    "403" in err_str or "recaptcha" in err_lc
                    or "permission_denied" in err_lc or "unusual_activity" in err_lc
                )
                is_throttled = (
                    "429" in err_str
                    or "resource_exhausted" in err_lc
                    or "throttled" in err_lc
                    or "user_requests_throttled" in err_lc
                    or "rate" in err_lc and "limit" in err_lc
                )
                if is_throttled and attempt < 4:
                    # Exponential backoff for rate limits — Google asks us to slow down
                    delay = min(60, (5 * (attempt + 1)) + _rand.uniform(2, 8))
                    log.warning(
                        f"[{self._account_email}] Image gen throttled 429 "
                        f"(attempt {attempt + 1}/5) — backing off {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                    continue
                if is_recaptcha and attempt < 4:
                    log.warning(
                        f"[{self._account_email}] Image gen reCAPTCHA rejected "
                        f"(attempt {attempt + 1}/5) — reloading page to reset score..."
                    )
                    try:
                        await self.renew_token(self._token)
                    except Exception as e:
                        log.warning(f"renew_token during image-gen retry failed: {e}")
                    continue
                log.error(f"[{self._account_email}] Image gen failed: {result}")
                raise ValueError(err_str)

            break
        else:
            err_str = str(result.get("text", result.get("error", "Unknown Error")))
            log.error(f"[{self._account_email}] Image gen failed after 5 attempts: {result}")
            raise ValueError(err_str)
        
        # Extract result — response is synchronous, contains image URL directly
        return self._extract_image_result(result)
    
    def _extract_image_result(self, result: dict) -> dict:
        """Extract image data from batchGenerateImages response.
        
        Response format:
        {
          "media": [{
            "name": "<media_id>",
            "image": {
              "generatedImage": {
                "fifeUrl": "https://flow-content.google/image/<id>?...",
                "seed": 754077,
                "mediaId": "<media_id>",
                ...
              },
              "dimensions": {"width": 1376, "height": 768}
            }
          }],
          "workflows": [...]
        }
        """
        media_list = result.get("media", [])
        if not media_list:
            log.error(f"No media in image response: {json.dumps(result)[:300]}")
            raise ValueError("No media in image generation response")
        
        media = media_list[0]
        gen_image = media.get("image", {}).get("generatedImage", {})
        dims = media.get("image", {}).get("dimensions", {})
        
        media_id = gen_image.get("mediaId") or media.get("name", "")
        download_url = gen_image.get("fifeUrl", "")
        seed = gen_image.get("seed", 0)
        width = dims.get("width", 0)
        height = dims.get("height", 0)
        
        if not download_url:
            log.error(f"No fifeUrl in image response: {json.dumps(result)[:300]}")
            raise ValueError("No download URL in image generation response")
        
        log.info(f"[{self._account_email}] Image generated: {media_id} ({width}x{height})")
        
        return {
            "media_id": media_id,
            "download_url": download_url,
            "seed": seed,
            "width": width,
            "height": height,
        }
    
    async def download_image(self, url: str, output_path: str) -> bool:
        """Download image from fifeUrl to local file."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 500:
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(resp.content)
                    log.info(f"Downloaded image: {output_path} ({len(resp.content)} bytes)")
                    return True
                log.warning(f"Image download failed: HTTP {resp.status_code}, size={len(resp.content)}")
        except Exception as e:
            log.error(f"Image download error: {e}")
        return False
    
    # ── Generative Fill (image + mask → generate) ───────────

    async def generate_image_with_mask(
        self,
        prompt: str,
        base_image_id: str,
        mask_image_id: str,
        model_key: str = "nano_banana_pro",
        seed: int | None = None,
    ) -> dict:
        """Generate an image using generative fill (mask-based editing).

        Takes a base image and a black/white mask (white = area to fill)
        and generates new content in the masked region guided by the prompt.
        Used by the Photoshop GenFill plugin.

        Args:
            prompt: Text prompt describing what to generate in the masked area
            base_image_id: media_id of the uploaded base image
            mask_image_id: media_id of the uploaded mask image (white = fill)
            model_key: Model key (nano_banana_pro, nano_banana_2)
            seed: Random seed (auto-generated if None)

        Returns:
            dict with keys: media_id, download_url, seed, width, height
        """
        await self.ensure_token()

        import random
        if seed is None:
            seed = random.randint(100000, 999999)

        # Resolve model name — mask editing uses same model map
        model_name = self.IMAGE_MODEL_MAP.get(model_key, model_key)

        # Build imageInputs — both are REFERENCE type
        # The applet config (flowSdkInfo) tells the API which is base vs mask
        image_inputs = [
            {
                "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                "name": base_image_id,
            },
            {
                "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE",
                "name": mask_image_id,
            },
        ]

        # Augment prompt with mask context (matches the mask magic tool)
        full_prompt = (
            f"Task: {prompt}. Blend naturally into the background. "
            "The mask indicates the edit area.. Match the exact aspect ratio "
            "and framing of the reference image."
        )

        # Get reCAPTCHA token
        recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")

        # Build client context
        client_context = {
            "projectId": self.project_id,
            "tool": "PINHOLE",
            "sessionId": f";{int(time.time() * 1000)}",
        }
        if recaptcha_token:
            client_context["recaptchaContext"] = {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }

        # Build request — matches the mask magic tool's exact payload structure
        request_item = {
            "clientContext": client_context,
            "imageModelName": model_name,
            "imageAspectRatio": "IMAGE_ASPECT_RATIO_UNSPECIFIED",
            "structuredPrompt": {
                "parts": [{"text": full_prompt}]
            },
            "seed": seed,
            "imageInputs": image_inputs,
            "requestContext": {
                "flowSdkInfo": {
                    "appletId": "54ee85a4-a175-4d14-a699-544f4bf86a21",
                    "appletVersionId": "4bde581b-6813-4639-9b72-337222808bbc",
                }
            },
        }

        payload = {
            "clientContext": client_context,
            "mediaGenerationContext": {
                "batchId": os.urandom(16).hex()[:8] + "-" +
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(4).hex()[:4] + "-" +
                           os.urandom(12).hex()[:12],
            },
            "useNewMedia": True,
            "requests": [request_item],
        }

        endpoint = f"projects/{self.project_id}/flowMedia:batchGenerateImages"

        log.info(f"[{self._account_email}] GenFill: model={model_name}, "
                 f"base={base_image_id[:12]}…, mask={mask_image_id[:12]}…, seed={seed}")

        # Retry loop (same pattern as generate_image)
        import random as _rand
        result = None
        for attempt in range(5):
            if attempt > 0:
                await asyncio.sleep(_rand.uniform(1.5, 3.0))
                recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")
                if recaptcha_token:
                    client_context["recaptchaContext"] = {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    }

            result = await self._browser_sandbox_request(endpoint, payload, is_text_plain=True)

            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                err_lc = err_str.lower()
                is_recaptcha = (
                    "403" in err_str or "recaptcha" in err_lc
                    or "permission_denied" in err_lc or "unusual_activity" in err_lc
                )
                is_throttled = (
                    "429" in err_str
                    or "resource_exhausted" in err_lc
                    or "throttled" in err_lc
                    or "user_requests_throttled" in err_lc
                    or "rate" in err_lc and "limit" in err_lc
                )
                if is_throttled and attempt < 4:
                    delay = min(60, (5 * (attempt + 1)) + _rand.uniform(2, 8))
                    log.warning(
                        f"[{self._account_email}] GenFill throttled 429 "
                        f"(attempt {attempt + 1}/5) — backing off {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                    continue
                if is_recaptcha and attempt < 4:
                    log.warning(
                        f"[{self._account_email}] GenFill reCAPTCHA rejected "
                        f"(attempt {attempt + 1}/5) — reloading page to reset score..."
                    )
                    try:
                        await self.renew_token(self._token)
                    except Exception as e:
                        log.warning(f"renew_token during genfill retry failed: {e}")
                    continue
                log.error(f"[{self._account_email}] GenFill failed: {result}")
                raise ValueError(err_str)

            break
        else:
            err_str = str(result.get("text", result.get("error", "Unknown Error")))
            log.error(f"[{self._account_email}] GenFill failed after 5 attempts: {result}")
            raise ValueError(err_str)

        return self._extract_image_result(result)

    # ── Upscale Image (2K / 4K) ──────────────────────────────
    
    UPSCALE_RESOLUTION_MAP = {
        "2k": "UPSAMPLE_IMAGE_RESOLUTION_2K",
        "4k": "UPSAMPLE_IMAGE_RESOLUTION_4K",
    }
    
    async def upscale_image(self, media_id: str, resolution: str = "4k") -> dict:
        """Upscale a generated image to 2K or 4K.
        
        Args:
            media_id: The media_id from generate_image() result
            resolution: "2k" or "4k"
            
        Returns:
            dict with keys: media_id, download_url, width, height
        """
        await self.ensure_token()
        
        res_enum = self.UPSCALE_RESOLUTION_MAP.get(
            resolution.lower(), "UPSAMPLE_IMAGE_RESOLUTION_4K"
        )
        
        endpoint = "flow/upsampleImage"
        
        log.info(f"[{self._account_email}] Upscaling image {media_id} to {resolution}")
        
        import random as _rand
        result = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(_rand.uniform(1.0, 3.0))
            
            recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")
            
            client_context = {
                "projectId": self.project_id,
                "tool": "PINHOLE",
                "userPaygateTier": "PAYGATE_TIER_TWO",
                "sessionId": f";{int(time.time() * 1000)}",
            }
            if recaptcha_token:
                client_context["recaptchaContext"] = {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                }
            
            payload = {
                "mediaId": media_id,
                "targetResolution": res_enum,
                "clientContext": client_context,
            }
            
            result = await self._browser_sandbox_request(endpoint, payload, is_text_plain=True)
            
            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                if "403" in err_str or "reCAPTCHA" in err_str or "PERMISSION_DENIED" in err_str:
                    log.warning(f"[{self._account_email}] Upscale reCAPTCHA rejected (attempt {attempt+1}), retrying...")
                    continue
                log.error(f"[{self._account_email}] Upscale failed: {err_str}")
                raise ValueError(err_str)
            
            break
        else:
            err_str = str(result.get("text", result.get("error", "Unknown Error")))
            log.error(f"[{self._account_email}] Upscale failed after 3 attempts: {err_str}")
            raise ValueError(err_str)
        
        # Log response keys for debugging
        resp_keys = list(result.keys()) if isinstance(result, dict) else str(type(result))
        log.info(f"[{self._account_email}] Upscale response keys: {resp_keys}")
        
        # The API returns encodedImage (base64 JPEG) directly
        if "encodedImage" in result:
            import base64 as b64
            image_data = result["encodedImage"]
            raw_bytes = b64.b64decode(image_data)
            
            # Read the ACTUAL dimensions from the returned image. Google's 4K
            # upscale now returns a larger, source-aspect image (e.g. 5504x3072),
            # not a fixed 3840x2160 — so never assume; detect from the bytes.
            width, height = 0, 0
            try:
                import io as _io
                from PIL import Image as _Image
                with _Image.open(_io.BytesIO(raw_bytes)) as _im:
                    width, height = _im.size
            except Exception:
                width, height = {"2k": (2560, 1440), "4k": (3840, 2160)}.get(resolution.lower(), (3840, 2160))
            
            log.info(f"[{self._account_email}] Upscaled to {resolution}: {len(raw_bytes)} bytes (encoded image)")
            
            return {
                "media_id": media_id,
                "download_url": None,  # No URL, data is embedded
                "encoded_image": raw_bytes,  # Raw bytes to save directly
                "width": width,
                "height": height,
            }
        
        # Fallback: search for fifeUrl
        def _deep_find(obj, key):
            if isinstance(obj, dict):
                if key in obj:
                    return obj[key]
                for v in obj.values():
                    found = _deep_find(v, key)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _deep_find(item, key)
                    if found:
                        return found
            return None
        
        download_url = _deep_find(result, "fifeUrl") or ""
        if download_url:
            log.info(f"[{self._account_email}] Upscaled via fifeUrl")
            return {
                "media_id": _deep_find(result, "mediaId") or media_id,
                "download_url": download_url,
                "encoded_image": None,
                "width": _deep_find(result, "width") or 0,
                "height": _deep_find(result, "height") or 0,
            }
        
        log.error(f"Upscale unknown response: {json.dumps(result)[:500]}")
        raise ValueError(f"Unknown upscale response format")
    
    # ── Generate Video ────────────────────
    
    async def generate_video(
        self,
        prompt: str,
        reference_image: Optional[str] = None,
        end_image: Optional[str] = None,
        model_key: str = "veo_3_generate_video_fast",
        aspect_ratio: str = "LANDSCAPE",
        duration: int = 8,
    ) -> Optional[str]:
        """Submit video generation request. Returns operation/generation ID.

        Args:
            prompt: Text prompt for video generation
            reference_image: media_id from upload_image() (UUID)
            model_key: Model key (e.g. veo_3_generate_video_fast)
            aspect_ratio: LANDSCAPE or PORTRAIT
            duration: Video length in seconds. Most Veo 3.1 models accept 4/6/8;
                Omni Flash also accepts 10. Field name `videoLengthSeconds` is
                what Google Labs sends per Network capture.
        """
        await self.ensure_token()
        
        # Build aspect ratio enum
        ar_map = {
            "LANDSCAPE": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "PORTRAIT": "VIDEO_ASPECT_RATIO_PORTRAIT",
            "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
        }
        ar_enum = ar_map.get(aspect_ratio, "VIDEO_ASPECT_RATIO_LANDSCAPE")
        
        import random
        result = None
        for attempt in range(5):
            # Stagger concurrent tasks slightly to avoid triggering Google's rate limiter
            if attempt > 0:
                await asyncio.sleep(random.uniform(1.0, 3.0))
            else:
                await asyncio.sleep(random.uniform(0.1, 1.0))
                
            # Get fresh reCAPTCHA token
            recaptcha_token = await self.get_recaptcha_token("VIDEO_GENERATION")
            
            if not prompt or prompt.strip() == "":
                prompt = "Static shot"
                
            if reference_image and end_image:
                # Loop / interpolation: first-frame + last-frame → video.
                # When start == end (same mediaId) the clip loops back to its
                # opening frame. Payload mirrors the T2V shape (per labs.google
                # HAR) plus startImage + endImage.
                ar_int = ("VIDEO_ASPECT_RATIO_PORTRAIT"
                          if ("9:16" in str(aspect_ratio)
                              or "PORTRAIT" in str(aspect_ratio).upper())
                          else "VIDEO_ASPECT_RATIO_LANDSCAPE")
                client_context = {
                    "projectId": self.project_id,
                    "tool": "PINHOLE",
                    # HAR shows userPaygateTier present even for the
                    # _low_priority interpolation model (unlike free T2V) —
                    # replicate exactly so the validator accepts it.
                    "userPaygateTier": "PAYGATE_TIER_TWO",
                    "sessionId": f";{int(time.time() * 1000)}",
                }
                if recaptcha_token:
                    client_context["recaptchaContext"] = {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    }
                payload = {
                    "mediaGenerationContext": {
                        "batchId": str(uuid.uuid4()),
                        "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
                    },
                    "clientContext": client_context,
                    "requests": [{
                        "aspectRatio": ar_int,
                        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                        "videoModelKey": model_key,
                        "seed": random.randint(10000, 99999),
                        "metadata": {},
                        "startImage": {"mediaId": reference_image},
                        "endImage": {"mediaId": end_image},
                    }],
                    "useV2ModelConfig": True,
                }
            elif reference_image:
                # Image-to-Video. Payload mirrors the T2V shape (per labs.google
                # HAR capture 2026-07-20) plus startImage. CONFIRMED identical
                # for both Veo 3.1 (veo_3_1_i2v_lite_low_priority) and Omni
                # Flash (abra_i2v_4s) — the only difference is videoModelKey.
                import random as _rng_i2v
                ar_i2v = ("VIDEO_ASPECT_RATIO_PORTRAIT"
                          if ("9:16" in str(aspect_ratio)
                              or "PORTRAIT" in str(aspect_ratio).upper())
                          else "VIDEO_ASPECT_RATIO_LANDSCAPE")

                client_context = {
                    "projectId": self.project_id,
                    "tool": "PINHOLE",
                    # HAR shows userPaygateTier present even for the
                    # _low_priority I2V model (unlike free T2V, which 500s when
                    # it's sent) — replicate exactly so the validator accepts it.
                    "userPaygateTier": "PAYGATE_TIER_TWO",
                    "sessionId": f";{int(time.time() * 1000)}",
                }
                if recaptcha_token:
                    client_context["recaptchaContext"] = {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                    }

                # Duration is encoded in model_key itself (Omni Flash:
                # abra_i2v_<N>s). Veo 3.1 I2V keys carry no duration suffix and
                # always render 8s — see VIDEO_DURATIONS_BY_MODEL in config.
                _ = duration  # silence unused-var, keep API signature stable

                # NOTE: Labs also sends startImage.cropCoordinates, but those
                # come from its own crop UI. We never crop, so it's omitted.
                payload = {
                    "mediaGenerationContext": {
                        "batchId": str(uuid.uuid4()),
                        "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
                    },
                    "clientContext": client_context,
                    "requests": [{
                        "aspectRatio": ar_i2v,
                        "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                        "videoModelKey": model_key,
                        "seed": _rng_i2v.randint(10000, 99999),
                        "metadata": {},
                        "startImage": {"mediaId": reference_image},
                    }],
                    "useV2ModelConfig": True,
                }
            else:
                # Text-to-Video: completely different payload & endpoint
                import random as _rng
                ar_t2v = "VIDEO_ASPECT_RATIO_PORTRAIT" if "9:16" in str(aspect_ratio) or "PORTRAIT" in str(aspect_ratio).upper() else "VIDEO_ASPECT_RATIO_LANDSCAPE"

                # Determine if this is a free queue model (LP / relaxed).
                # For free queues we must NOT send `userPaygateTier` or Google
                # returns HTTP 500 INTERNAL ("free model on paid tier" mismatch).
                # For paid queues (fast/quality/lite), we send PAYGATE_TIER_TWO
                # which is what Labs UI sends — even free accounts can get
                # past the validator with this tier as long as the model itself
                # is paid (credits will be deducted normally).
                mk_lower = (model_key or "").lower()
                is_free_queue = (
                    "_low_priority" in mk_lower
                    or "_relaxed" in mk_lower
                    or "_ultra_relaxed" in mk_lower
                )

                client_context = {
                    "projectId": self.project_id,
                    "tool": "PINHOLE",
                    "sessionId": f";{int(time.time() * 1000)}",
                }
                if not is_free_queue:
                    client_context["userPaygateTier"] = "PAYGATE_TIER_TWO"

                payload = {
                    "mediaGenerationContext": {
                        "batchId": str(uuid.uuid4()),
                        "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"
                    },
                    "clientContext": client_context,
                    "requests": [{
                        "aspectRatio": ar_t2v,
                        "textInput": {
                            "structuredPrompt": {
                                "parts": [{"text": prompt}]
                            }
                        },
                        "videoModelKey": model_key,
                        # duration field disabled — see I2V branch note above
                        "metadata": {},
                        "seed": _rng.randint(10000, 99999)
                    }],
                    "useV2ModelConfig": True
                }

                if recaptcha_token:
                    payload["clientContext"]["recaptchaContext"] = {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    }
            
            self._last_model_key = model_key
            if attempt == 0:
                log.info(f"[{self._account_email}] Generating video: model={model_key}, ref={reference_image is not None}")
            
            if reference_image and end_image:
                endpoint = "video:batchAsyncGenerateVideoStartAndEndImage"
            elif reference_image:
                endpoint = "video:batchAsyncGenerateVideoStartImage"
            else:
                endpoint = "video:batchAsyncGenerateVideoText"
            result = await self._browser_sandbox_request(endpoint, payload, is_text_plain=True)
            
            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                err_lc = err_str.lower()
                # Rate limit — back off exponentially
                if "429" in err_str or "resource_exhausted" in err_lc or "throttled" in err_lc:
                    delay = min(60, (5 * (attempt + 1)) + random.uniform(2, 8))
                    log.warning(
                        f"[{self._account_email}] Video gen throttled 429 "
                        f"(attempt {attempt + 1}/5) — backing off {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                    continue
                if "403" in err_str or "reCAPTCHA" in err_str or "PERMISSION_DENIED" in err_str or "execution context" in err_lc:
                    if "unusual_activity" in err_lc or "recaptcha" in err_lc:
                        log.info(f"[{self._account_email}] Renewing session token due to reCAPTCHA error...")
                        await self.renew_token(self._token)
                    else:
                        log.warning(f"[{self._account_email}] reCAPTCHA rejected or 403 (attempt {attempt+1}), retrying with new token...")
                    continue
                log.error(f"[{self._account_email}] Generate failed: {result}")
                raise ValueError(err_str)
            
            # Success, break the retry loop
            break
        else:
            err_str = str(result.get("text", result.get("error", "Unknown Error")))
            log.error(f"[{self._account_email}] Generate failed after 3 attempts: {result}")
            raise ValueError(err_str)
        
        # Extract generation ID
        gen_id = self._extract_generation_id(result)
        if gen_id:
            log.info(f"[{self._account_email}] Generation started: {gen_id}")
        else:
            err_msg = json.dumps(result)[:300]
            log.error(f"[{self._account_email}] No generation ID in response: {err_msg}")
            raise ValueError(f"Invalid Response: {err_msg}")
        
        return gen_id
        
    async def extend_video(self, prompt: str, media_id: str, workflow_id: str, model_key: str, aspect_ratio: str) -> str:
        """Extend an existing generated video."""
        import random as _rng
        ar_str = str(aspect_ratio).upper()
        ar_t2v = "VIDEO_ASPECT_RATIO_PORTRAIT" if ("9:16" in ar_str or "PORTRAIT" in ar_str) else "VIDEO_ASPECT_RATIO_LANDSCAPE"

        mk_lower = (model_key or "").lower()
        is_free_queue = "_low_priority" in mk_lower or "_relaxed" in mk_lower or "_extension" in mk_lower

        for attempt in range(5):
            recaptcha_token = await self.get_recaptcha_token(action="VIDEO_GENERATION")

            client_context = {
                "projectId": self.project_id,
                "tool": "PINHOLE",
                "sessionId": f";{int(time.time() * 1000)}",
            }
            if not is_free_queue:
                client_context["userPaygateTier"] = "PAYGATE_TIER_TWO"

            payload = {
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4()),
                    "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"
                },
                "clientContext": client_context,
                "requests": [{
                    "aspectRatio": ar_t2v,
                    "textInput": {"structuredPrompt": {"parts": [{"text": prompt}]}},
                    "videoModelKey": model_key,
                    "metadata": {"workflowId": workflow_id},
                    "seed": _rng.randint(10000, 99999),
                    "videoInput": {"mediaId": media_id}
                }],
                "useV2ModelConfig": True
            }
            if recaptcha_token:
                payload["clientContext"]["recaptchaContext"] = {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                }
            
            log.info(f"[{self._account_email}] Extending video {media_id} (attempt {attempt+1})")
            result = await self._browser_sandbox_request("video:batchAsyncGenerateVideoExtendVideo", payload, is_text_plain=True)
            
            # Log full response for debugging
            log.info(f"[{self._account_email}] Extend response: {json.dumps(result)[:500]}")
            
            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                if "403" in err_str or "reCAPTCHA" in err_str or "PERMISSION_DENIED" in err_str or "execution context" in err_str.lower():
                    if "unusual_activity" in err_str.lower() or "recaptcha" in err_str.lower():
                        log.info(f"[{self._account_email}] Renewing session token due to reCAPTCHA error...")
                        await self.renew_token(self._token)
                    continue
                raise ValueError(err_str)
            break
        else:
            raise ValueError(str(result.get("text", result.get("error", "Unknown Error"))))
            
        gen_id = self._extract_generation_id(result)
        if not gen_id:
            raise ValueError(f"No generation ID in extend response: {str(result)[:200]}")
        return gen_id

    async def upscale_video(self, media_id: str, workflow_id: str, aspect_ratio: str, resolution: str = "1080p") -> str:
        """Upscale an existing generated video."""
        import random as _rng
        ar_str = str(aspect_ratio).upper()
        ar_t2v = "VIDEO_ASPECT_RATIO_PORTRAIT" if ("9:16" in ar_str or "PORTRAIT" in ar_str) else "VIDEO_ASPECT_RATIO_LANDSCAPE"

        model_key = "veo_3_1_upsampler_1080p" if "1080" in resolution else "veo_3_1_upsampler_4k"
        res_enum = "VIDEO_RESOLUTION_1080P" if "1080" in resolution else "VIDEO_RESOLUTION_4K"

        for attempt in range(5):
            recaptcha_token = await self.get_recaptcha_token(action="VIDEO_GENERATION")

            # Upscaler is a paid feature — keep userPaygateTier
            payload = {
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4()),
                    "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"
                },
                "clientContext": {
                    "projectId": self.project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": "PAYGATE_TIER_TWO",
                    "sessionId": f";{int(time.time() * 1000)}",
                },
                "requests": [{
                    "resolution": res_enum,
                    "aspectRatio": ar_t2v,
                    "videoModelKey": model_key,
                    "metadata": {"workflowId": workflow_id},
                    "seed": _rng.randint(10000, 99999),
                    "videoInput": {"mediaId": media_id}
                }],
                "useV2ModelConfig": True
            }
            if recaptcha_token:
                payload["clientContext"]["recaptchaContext"] = {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                }
            
            log.info(f"[{self._account_email}] Upscaling video {media_id} to {resolution} (attempt {attempt+1})")
            result = await self._browser_sandbox_request("video:batchAsyncGenerateVideoUpsampleVideo", payload, is_text_plain=True)
            
            if "error" in result:
                err_str = str(result.get("text", result.get("error", "")))
                if "403" in err_str or "reCAPTCHA" in err_str or "PERMISSION_DENIED" in err_str or "execution context" in err_str.lower():
                    if "unusual_activity" in err_str.lower() or "recaptcha" in err_str.lower():
                        log.info(f"[{self._account_email}] Renewing session token due to reCAPTCHA error...")
                        await self.renew_token(self._token)
                    continue
                raise ValueError(err_str)
            break
        else:
            raise ValueError(str(result.get("text", result.get("error", "Unknown Error"))))
            
        gen_id = self._extract_generation_id(result)
        if not gen_id:
            raise ValueError(f"No generation ID in upscale response: {str(result)[:200]}")
        return gen_id
    
    def _extract_generation_id(self, result: dict, _depth: int = 0) -> Optional[str]:
        """Extract generation/operation ID from aisandbox response.
        
        Response formats:
        1. I2V: {generateVideoResponses: [{generationId: "..."}]}
        2. T2V/Extend/Upscale: {operations: [...], workflows: [...], media: [...]}
        
        For extend responses, primaryMediaId is the ORIGINAL video (wrong!).
        The correct ID is in operations[0].operation.name or media[0].name.
        """
        if _depth > 5:
            return None
        
        if not isinstance(result, dict):
            return None
        
        # Priority 1: operations array (extend/upscale/T2V responses)
        # operations[0].operation.name = new generation ID
        operations = result.get("operations")
        if isinstance(operations, list) and operations:
            op = operations[0]
            if isinstance(op, dict):
                op_inner = op.get("operation", {})
                if isinstance(op_inner, dict):
                    name = op_inner.get("name")
                    if name and isinstance(name, str) and len(name) >= 16:
                        return name
                # Also check operationId directly
                op_id = op.get("operationId") or op.get("name")
                if op_id and isinstance(op_id, str) and len(op_id) >= 16:
                    return op_id
        
        # Priority 2: media array — media[0].name = new media ID
        media = result.get("media")
        if isinstance(media, list) and media:
            m = media[0]
            if isinstance(m, dict):
                name = m.get("name")
                if name and isinstance(name, str) and len(name) >= 16:
                    return name
        
        # Priority 3: Direct ID fields (old format)
        for key in ["generationId", "operationId"]:
            val = result.get(key)
            if val and isinstance(val, str) and len(val) >= 16:
                return val
        
        # Priority 4: generateVideoResponses (I2V old format)
        for key in ["generateVideoResponses", "responses"]:
            nested = result.get(key)
            if isinstance(nested, list) and nested:
                found = self._extract_generation_id(nested[0], _depth + 1)
                if found:
                    return found
        
        # Priority 5: workflows — ONLY use primaryMediaId if nothing else found
        # (for T2V generate, workflows[0].metadata.primaryMediaId IS the gen_id)
        # (for extend, it's the ORIGINAL video — wrong! But we won't reach here
        #  because operations/media arrays are checked first)
        workflows = result.get("workflows")
        if isinstance(workflows, list) and workflows:
            wf = workflows[0]
            if isinstance(wf, dict):
                meta = wf.get("metadata", {})
                if isinstance(meta, dict):
                    mid = meta.get("primaryMediaId")
                    if mid and isinstance(mid, str) and len(mid) >= 16:
                        return mid
        
        # Priority 6: Fallback — top-level name/id
        for key in ["name", "id"]:
            val = result.get(key)
            if val and isinstance(val, str) and len(val) >= 16:
                return val
        
        # Priority 7: Recurse into nested dicts
        for key, val in result.items():
            if isinstance(val, dict):
                found = self._extract_generation_id(val, _depth + 1)
                if found:
                    return found
        
        return None
    
    # ── reCAPTCHA ─────────────────────────
    
    async def get_recaptcha_token(self, action: str = "VIDEO_GENERATION") -> str:
        """Get reCAPTCHA token via in-page harvest on the main browser
        page (CDP pattern). Subprocess provider path was tried
        and reverted — see `ensure_provider()` comment.
        """
        lock = _get_recaptcha_lock(self._account_email)

        async with lock:
            log.info(f"Harvesting reCAPTCHA (action={action})...")

            # Simulate human-like cursor activity to keep reCAPTCHA risk
            # score high. Multiple curved moves + occasional click are much
            # more convincing than a single mouse.move.
            try:
                import random as _rand
                num_moves = _rand.randint(3, 6)
                last_x, last_y = 100 + _rand.randint(0, 400), 100 + _rand.randint(0, 400)
                for _ in range(num_moves):
                    nx = max(20, min(900, last_x + _rand.randint(-180, 180)))
                    ny = max(20, min(600, last_y + _rand.randint(-120, 120)))
                    steps = _rand.randint(8, 15)
                    await self._page.mouse.move(nx, ny, steps=steps)
                    await asyncio.sleep(_rand.uniform(0.04, 0.12))
                    last_x, last_y = nx, ny
                # Occasionally tap (humans click around while thinking)
                if _rand.random() < 0.45:
                    await self._page.mouse.down()
                    await asyncio.sleep(_rand.uniform(0.03, 0.08))
                    await self._page.mouse.up()
                # Tiny scroll wheel jiggle
                if _rand.random() < 0.35:
                    await self._page.mouse.wheel(0, _rand.randint(-40, 40))
                    await asyncio.sleep(0.1)
            except Exception:
                pass
            
            # We rely on ensure_token() to navigate to the correct page initially.
            # Navigating here would destroy the execution context for concurrent tasks.
            
            # Wait for grecaptcha to load
            for i in range(30):
                try:
                    has = await self._page.evaluate(
                        "typeof grecaptcha !== 'undefined' && (!!grecaptcha.enterprise || !!grecaptcha.execute)"
                    )
                except Exception as e:
                    log.debug(f"grecaptcha check error: {e}")
                    has = False
                    
                if has:
                    log.info(f"grecaptcha loaded after {i * 0.5}s")
                    break
                await asyncio.sleep(0.5)
            
            try:
                token_result = await self._page.evaluate("""async (action) => {
                    let siteKey = null;
                    const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                    for (const s of scripts) {
                        const m = s.src.match(/[?&]render=([^&]+)/);
                        if (m) { siteKey = m[1]; break; }
                    }
                    if (!siteKey) return {error: 'no_site_key'};
                    if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                        try {
                            const token = await grecaptcha.enterprise.execute(
                                siteKey, {action: action}
                            );
                            if (token) return {token, key: siteKey};
                        } catch(e) {
                            return {error: e.message};
                        }
                    }
                    return {error: 'grecaptcha_not_available'};
                }""", action)
                
                if token_result and isinstance(token_result, dict) and token_result.get("token"):
                    log.info(f"Got reCAPTCHA token: {token_result['token'][:20]}...")
                    self._recaptcha_fail_count = 0
                    return token_result["token"]
                else:
                    log.warning(f"Could not get reCAPTCHA token: {token_result}")
                    self._recaptcha_fail_count += 1
                    return ""
            except Exception as e:
                log.error(f"reCAPTCHA harvester error: {e}")
                self._recaptcha_fail_count += 1
                return ""
    
    # ── Poll Status ───────────────────────
    
    async def poll_status(self, generation_id: str) -> dict:
        """Poll video generation status via browser fetch."""
        payload = {
            "media": [{"name": generation_id, "projectId": self.project_id}]
        }
        result = await self._browser_sandbox_request(
            "video:batchCheckAsyncVideoGenerationStatus", payload
        )
        
        if "error" in result:
            return result
            
        # Parse status from response
        # Dump to file to debug structure
        try:
            import json
            with open("debug_poll.json", "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
            
        # Old: generateVideoStatuses, New: media or mediaStatuses
        statuses = result.get("media", result.get("generateVideoStatuses", result.get("statuses", [])))
        if statuses and isinstance(statuses, list):
            # Print the actual media object for debugging
            log.info(f"Poll media[0]: {str(statuses[0])[:500]}")
            return statuses[0]
            
        log.info(f"Poll raw result: {str(result)[:300]}...")
        return result
    
    async def wait_for_completion(
        self,
        generation_id: str,
        timeout: int = 600,
        callback=None,
        cancel_check=None,
    ) -> dict:
        """Poll until video completes or times out.
        
        v1.2.11: Handle Low Priority models where media object
        isn't created immediately ("Media not found" is transient).
        """
        start = time.time()
        consecutive_errors = 0
        max_consecutive_errors = 12  # More retries for Low Priority
        media_not_found_count = 0
        max_media_not_found = 20  # Low Priority can take minutes to start
        
        # Wait before first poll — Low Priority models need time to register
        await asyncio.sleep(10)
        
        while time.time() - start < timeout:
            # Check cancellation
            if cancel_check and cancel_check():
                return {"state": "FAILED", "error": "CANCELLED"}
            
            status_info = await self.poll_status(generation_id)
            
            # Handle errors
            if "error" in status_info:
                consecutive_errors += 1
                log.warning(f"Poll error ({consecutive_errors}/{max_consecutive_errors}): {status_info}")
                if consecutive_errors >= max_consecutive_errors:
                    return {"state": "FAILED", "error": f"Too many poll errors: {status_info}"}
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            
            consecutive_errors = 0
            
            # Priority 1: Check mediaMetadata.mediaStatus (Ultra model response)
            state = "RUNNING"
            failure_msg = ""
            if "mediaMetadata" in status_info and "mediaStatus" in status_info["mediaMetadata"]:
                media_status_obj = status_info["mediaMetadata"]["mediaStatus"]
                media_status = media_status_obj.get("mediaGenerationStatus")
                if media_status:
                    state = media_status
                # Get failure message for "Media not found" detection
                err_obj = media_status_obj.get("error", {})
                if isinstance(err_obj, dict):
                    failure_msg = err_obj.get("message", "")
                reasons = media_status_obj.get("failureReasons", [])
                if reasons:
                    failure_msg = failure_msg or str(reasons[0])
            
            # Priority 2: Fallback to top-level state fields
            if state == "RUNNING":
                state = status_info.get("state", status_info.get("status", "RUNNING"))
            
            # Check for actual video data
            has_video = False
            if "mediaMetadata" in status_info and "videoUri" in status_info["mediaMetadata"]:
                has_video = True
            
            # Callback
            if callback:
                try:
                    await callback(status_info)
                except Exception:
                    pass
            
            # Check completion
            if state in ("COMPLETED", "COMPLETE", "MEDIA_GENERATION_STATUS_SUCCESSFUL") or has_video:
                status_info["state"] = "COMPLETED"
                log.info(f"Video generation completed! state={state}, has_video={has_video}")
                return status_info
            
            # Check failure — but "Media not found" is TRANSIENT for Low Priority
            if state in ("FAILED", "ERROR", "MEDIA_GENERATION_STATUS_FAILED"):
                if "Media not found" in failure_msg or "not found" in failure_msg.lower():
                    media_not_found_count += 1
                    log.info(f"Media not found ({media_not_found_count}/{max_media_not_found}) — "
                             f"waiting for Low Priority media to register...")
                    if media_not_found_count >= max_media_not_found:
                        return {"state": "FAILED", "error": "Media never became available"}
                    await asyncio.sleep(POLL_INTERVAL_SECONDS * 2)  # Wait longer
                    continue
                # Real failure
                status_info["state"] = "FAILED"
                status_info["error"] = failure_msg or state
                log.warning(f"Video generation FAILED: {failure_msg or state}")
                return status_info
            
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        
        return {"state": "FAILED", "error": "Timeout"}
    
    # ── Check Credits ─────────────────
    
    async def check_credits(self) -> Optional[dict]:
        """Check remaining AI credits.
        
        Strategy (in priority order):
        1. Try /fx/api/trpc/user.getUserSettings API (fast, no DOM interaction)
        2. Try reading credit text directly from page (no popup click needed)
        3. Click profile avatar → read popup "XX AI credits" text
        """
        await self.ensure_token()
        
        # Make sure we're on the Flow page
        current_url = self._page.url or ""
        if "labs.google" not in current_url:
            await self._page.goto("https://labs.google/fx/tools/video-fx", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            await asyncio.sleep(3)
        
        # ── Method 1: Hit the aisandbox-pa credits endpoint directly ──
        # CONFIRMED via user's network capture (2026-05-22):
        #   GET https://aisandbox-pa.googleapis.com/v1/credits?key=<AIza...>
        #   Headers: Authorization: Bearer <NextAuth token>
        #   Response (JSON):
        #     {
        #       "credits": 35871,              ← total = subscription + topUp
        #       "userPaygateTier": "PAYGATE_TIER_TWO",
        #       "sku": "G1_TIER2",
        #       "serviceTier": "SERVICE_TIER_ADVANCED",
        #       "topUpCredits": 25000,         ← purchased (one-off packs)
        #       "subscriptionCredits": 10871   ← THE NUMBER WE WANT — what
        #                                       Google shows in popup as
        #                                       "10871 Tín dụng Flow"
        #     }
        # User explicitly wants the "Tín dụng Flow" subscription number,
        # not the total — that matches what the avatar popup shows so they
        # can sanity-check by clicking the ULTRA badge.
        #
        # NOTE on the ?key= param: Google's other aisandbox endpoints we
        # already call (videoGen, imageGen, upsampleImage) work with Bearer
        # alone — no key needed. We try the same way first. If that 401s
        # we fall back to extracting the page's embedded API key from a
        # global config object.
        try:
            api_credits = await self._page.evaluate('''async ([token]) => {
                async function tryFetch(url, extraHeaders) {
                    try {
                        const r = await fetch(url, {
                            method: "GET",
                            headers: Object.assign(
                                {"Authorization": "Bearer " + token},
                                extraHeaders || {},
                            ),
                            credentials: "include",
                        });
                        if (!r.ok) return null;
                        const data = await r.json();
                        let tier = "FREE";
                        const pg = (data.userPaygateTier || "").toUpperCase();
                        const sv = (data.serviceTier || "").toUpperCase();
                        const sk = (data.sku || "").toUpperCase();
                        if (pg.includes("TIER_TWO") || sv.includes("ADVANCED") || sk.includes("TIER2")) {
                            tier = "ULTRA";
                        } else if (pg.includes("TIER_ONE") || sv.includes("STANDARD") || sk.includes("TIER1")) {
                            tier = "PRO";
                        }
                        // Prefer subscriptionCredits — matches the "Tín dụng
                        // Flow" number Google shows in the popup. Fall back
                        // to `credits` (total) only if subscriptionCredits
                        // is missing.
                        let val = null;
                        if (typeof data.subscriptionCredits === "number")
                            val = data.subscriptionCredits;
                        else if (typeof data.credits === "number")
                            val = data.credits;
                        if (val != null && val >= 500 && tier === "FREE") tier = "ULTRA";
                        return { value: val, tier: tier };
                    } catch(e) { return null; }
                }

                // 1st attempt: Bearer only (same pattern as videoGen etc.)
                const baseUrl = "https://aisandbox-pa.googleapis.com/v1/credits";
                let res = await tryFetch(baseUrl);
                if (res != null && res.value != null) return { value: res.value, tier: res.tier, source: "bearer" };

                // 2nd: with ?key= extracted from page bundle globals.
                let apiKey = null;
                try {
                    const html = document.documentElement.outerHTML;
                    const m = html.match(/AIza[0-9A-Za-z_\\-]{35}/);
                    if (m) apiKey = m[0];
                } catch(e) {}
                if (apiKey) {
                    res = await tryFetch(baseUrl + "?key=" + apiKey);
                    if (res != null && res.value != null) return { value: res.value, tier: res.tier, source: "bearer+key" };
                }

                // 3rd: relative fetch via labs.google
                res = await tryFetch("/fx/api/credits");
                if (res != null && res.value != null) return { value: res.value, tier: res.tier, source: "labs-proxy" };

                return null;
            }''', [self._token or ""])

            if api_credits and isinstance(api_credits, dict) and isinstance(api_credits.get("value"), (int, float)):
                value = int(api_credits["value"])
                tier = api_credits.get("tier") or ("ULTRA" if value >= 500 else "FREE")
                log.info(
                    f"[{self._account_email}] Credits from aisandbox: {value} (tier: {tier}) "
                    f"(auth: {api_credits.get('source')})"
                )
                return {"remainingCredits": value, "tier": tier}
        except Exception as e:
            log.debug(f"[{self._account_email}] aisandbox credits endpoint failed: {e}")
        
        # ── Method 2: Read credit text from page directly (no popup) ──
        try:
            page_credits = await self._page.evaluate(r'''
                () => {
                    const text = document.body.innerText;
                    // Try multiple patterns
                    const patterns = [
                        // Google renamed label in May 2026: "T\u00edn d\u1ee5ng AI" \u2192 "T\u00edn d\u1ee5ng Flow"
                        /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*Flow/i,
                        /(\d[\d,.\s]*)\s*Flow\s*credits?/i,
                        /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*AI/i,
                        /(\d[\d,.\s]*)\s*AI\s*credits?/i,
                        /T\u00edn\s*d\u1ee5ng\s*(?:AI|Flow)\s*:?\s*(\d[\d,.\s]*)/i,
                        /credits?\s*:?\s*(\d[\d,.\s]*)/i,
                        /(\d[\d,.\s]*)\s*credits?\s*remaining/i,
                    ];
                    for (const p of patterns) {
                        const m = text.match(p);
                        if (m) {
                            return parseInt(m[1].replace(/[,.\s]/g, ''));
                        }
                    }
                    return null;
                }
            ''')
            
            if page_credits is not None and page_credits >= 0:
                tier = "ULTRA" if page_credits >= 500 else "FREE"
                log.info(f"[{self._account_email}] Credits from page text: {page_credits} (tier: {tier})")
                return {"remainingCredits": page_credits, "tier": tier}
        except Exception as e:
            log.debug(f"[{self._account_email}] Page text credit check failed: {e}")
        
        # ── Method 3: Click ULTRA/tier badge → read popup ──
        try:
            # The credit info is shown when clicking the "ULTRA" or "AI trial" badge
            # in the top-right area of Google Labs header (NOT the profile avatar)
            badge = None
            
            # First try: find by text content using JS (most reliable)
            badge = await self._page.evaluate_handle('''
                () => {
                    // Look for elements containing "ULTRA", "FREE", "AI trial" text
                    const keywords = ['ULTRA', 'AI trial', 'FREE', 'credits'];
                    const allElements = document.querySelectorAll('button, a, div[role="button"], [class*="badge"], [class*="tier"], [class*="credit"], [class*="plan"]');
                    for (const el of allElements) {
                        const text = el.textContent?.trim() || '';
                        if (keywords.some(kw => text.includes(kw)) && el.offsetWidth > 0 && el.offsetHeight > 0) {
                            return el;
                        }
                    }
                    // Broader search: any clickable element with tier/credit keywords
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                    while (walker.nextNode()) {
                        const node = walker.currentNode;
                        const text = node.textContent?.trim() || '';
                        const tag = node.tagName;
                        if ((tag === 'BUTTON' || tag === 'A' || node.getAttribute('role') === 'button' || node.style?.cursor === 'pointer') 
                            && keywords.some(kw => text.includes(kw))
                            && text.length < 50
                            && node.offsetWidth > 0) {
                            return node;
                        }
                    }
                    return null;
                }
            ''')
            
            if badge:
                try:
                    await badge.as_element().click()
                    log.info(f"[{self._account_email}] Clicked tier badge via JS text search")
                except Exception:
                    badge = None
            
            # Fallback: try CSS selectors for common avatar/profile patterns
            if not badge:
                for selector in [
                    'img[data-profileimageid]',
                    'button[aria-label*="Account"]',
                    'a[aria-label*="Account"]',
                    'img[src*="googleusercontent"]',
                    '[data-ogsr-up]',
                    '.gb_d',
                ]:
                    try:
                        el = await self._page.query_selector(selector)
                        if el and await el.is_visible():
                            await el.click()
                            log.info(f"[{self._account_email}] Clicked fallback avatar: {selector}")
                            badge = el
                            break
                    except Exception:
                        continue
            
            if not badge:
                log.warning(f"[{self._account_email}] Could not find ULTRA badge or profile button")
                try:
                    debug_text = await self._page.evaluate("document.body.innerText.substring(0, 2000)")
                    log.info(f"[{self._account_email}] Page text sample: {debug_text[:500]}")
                except Exception:
                    pass
                return {"error": "Badge/Avatar not found"}
            
            await asyncio.sleep(2)
            
            # Read credit text with broader patterns
            credits = await self._page.evaluate(r'''
                () => {
                    try {
                        const text = document.body.innerText;
                        const patterns = [
                            // Google renamed label May 2026: "T\u00edn d\u1ee5ng AI" \u2192 "T\u00edn d\u1ee5ng Flow"
                            /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*Flow/i,
                            /(\d[\d,.\s]*)\s*Flow\s*credits?/i,
                            /(\d[\d,.\s]*)\s*T\u00edn\s*d\u1ee5ng\s*AI/i,
                            /(\d[\d,.\s]*)\s*AI\s*credits?/i,
                            /T\u00edn\s*d\u1ee5ng\s*(?:AI|Flow)\s*:?\s*(\d[\d,.\s]*)/i,
                            /AI\s*credits?\s*:?\s*(\d[\d,.\s]*)/i,
                            /credits?\s*remaining\s*:?\s*(\d[\d,.\s]*)/i,
                            /(\d[\d,.\s]*)\s*credits?\s*left/i,
                            /(\d[\d,.\s]*)\s*credits?/i,
                        ];
                        for (const p of patterns) {
                            const m = text.match(p);
                            if (m) {
                                const num = parseInt(m[1].replace(/[,.\s]/g, ''));
                                if (num >= 0 && num < 10000000) return num;
                            }
                        }
                        return null;
                    } catch (e) {
                        return null;
                    }
                }
            ''')
            
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass
            
            # Accept 0 — see note in Method 2. A genuine "0 credits" reading
            # is more useful than False-None which the UI also rendered as 0.
            if credits is not None and credits >= 0:
                tier = "ULTRA" if credits >= 500 else "FREE"
                log.info(f"[{self._account_email}] Credits from badge popup: {credits} (tier: {tier})")
                return {"remainingCredits": credits, "tier": tier}
            
            # Debug: dump popup content 
            try:
                debug_text = await self._page.evaluate("document.body.innerText.substring(0, 3000)")
                log.warning(f"[{self._account_email}] Popup text (no credits found): {debug_text[:800]}")
                # Also take a debug screenshot
                await self._page.screenshot(path=f"debug_credits.png", full_page=False)
                log.info(f"[{self._account_email}] Saved debug screenshot: debug_credits.png")
            except Exception:
                pass
            
            return {"error": "Credits not found in popup"}
            
        except Exception as e:
            log.error(f"[{self._account_email}] Credit check error: {e}")
            return {"error": str(e)}
    
    # ── Get Video Models ──────────────────────────
    
    async def get_video_models(self) -> list:
        """Fetch available video models from tRPC API."""
        result = await self._api("videoFx.getVideoModelConfig", None, "GET")
        data = self._extract(result)
        models = data.get("videoModelConfigs", [])
        if models:
            log.info(f"VideoModelRegistry: {len(models)} models from API")
        return models
    
    # ── Download ──────────────────────────
    
    async def get_download_url(self, media_id: str) -> Optional[str]:
        """Get video download URL via tRPC redirect endpoint.
        
        Google Labs format (confirmed via network capture):
          GET /fx/api/trpc/media.getMediaUrlRedirect?name=<media_id>
          → 307 redirect to https://flow-content.google/video/<id>?Expires=...&Signature=...
        
        Uses Playwright's page.request API (not browser fetch) to bypass CORS
        on the cross-origin redirect to flow-content.google.
        """
        url = f"{self.TRPC}/media.getMediaUrlRedirect?name={media_id}"
        
        try:
            # Use Playwright request API — bypasses CORS, uses browser cookies
            resp = await self._page.request.get(url, max_redirects=0)
            
            # 307 redirect → extract Location header (signed CDN URL)
            if resp.status in (301, 302, 307, 308):
                location = resp.headers.get('location', '')
                if location:
                    log.info(f"Got download URL (307 redirect): {location[:100]}...")
                    return location
            
            # If server returned 200 directly (unlikely but handle it)
            if resp.ok:
                # Check if it's JSON with a URL inside
                try:
                    data = await resp.json()
                    redirect_url = (
                        data.get("url") or
                        data.get("result", {}).get("data", {}).get("json", {}).get("url")
                    )
                    if redirect_url:
                        return redirect_url
                except Exception:
                    pass
                # The URL itself might serve the video
                return url
            
            log.warning(f"get_download_url: HTTP {resp.status}")
            
        except Exception as e:
            log.error(f"get_download_url error: {e}")
        
        return None
    
    async def _fetch_mp4_via_browser(self, media_id: str) -> Optional[bytes]:
        """Fetch MP4 bytes directly via Playwright request API.
        
        Uses page.request.get() which follows redirects and bypasses CORS.
        The 307 redirect to flow-content.google CDN is followed automatically.
        """
        url = f"{self.TRPC}/media.getMediaUrlRedirect?name={media_id}"
        
        try:
            # Follow redirects automatically, download video bytes
            resp = await self._page.request.get(url, timeout=120000)
            
            if resp.ok:
                body = await resp.body()
                if len(body) > 1000:
                    log.info(f"_fetch_mp4_via_browser OK: {len(body)} bytes")
                    return body
                log.warning(f"_fetch_mp4_via_browser: response too small ({len(body)} bytes)")
            else:
                log.warning(f"_fetch_mp4_via_browser: HTTP {resp.status}")
                
        except Exception as e:
            log.error(f"_fetch_mp4_via_browser error: {e}")
        
        return None
    
    async def download_video(self, url: str, output_path: str) -> bool:
        """Download video from URL to local file using httpx."""
        if not url:
            log.error("download_video called with no URL")
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(output_path).write_bytes(resp.content)
                    log.info(f"Downloaded video: {output_path} ({len(resp.content)} bytes)")
                    return True
                log.warning(f"Download failed: HTTP {resp.status_code}, size={len(resp.content)}")
        except Exception as e:
            log.error(f"Download error: {e}")
        return False

    async def download_to(self, media_id: str, output_path: str) -> bool:
        """Save a generated video (by media_id) to a local file.

        Tries the most reliable path first: Playwright's request API which
        follows the 307 redirect to flow-content.google with browser cookies.
        Falls back to httpx + signed URL only if the browser fetch returns
        nothing.
        """
        if not media_id:
            log.error("download_to: no media_id")
            return False

        # Method 1: browser fetch (preferred — handles auth + redirects).
        # Retry a few times: the fetch runs inside the user's Chrome tab, so a
        # concurrent CPU/RAM hog (watermark removal = ffmpeg + LaMa/torch) can
        # starve that tab or get it discarded, making a *claimed* proxy_fetch
        # blow its 120s budget. That timeout is transient — once the spike
        # passes a retry usually succeeds, so don't lose the whole download on
        # one miss. Only genuine failures hit the 120s wait; a healthy fetch
        # returns in seconds, so the happy path adds no latency.
        _browser_fetch_backoffs = (5, 15)   # sleeps BETWEEN the 3 attempts
        body = None
        for _attempt in range(3):
            body = await self._fetch_mp4_via_browser(media_id)
            if body:
                break
            if _attempt < len(_browser_fetch_backoffs):
                wait = _browser_fetch_backoffs[_attempt]
                log.warning(
                    f"download_to: browser fetch trống "
                    f"(attempt {_attempt + 1}/3) media_id={media_id} — "
                    f"thử lại sau {wait}s (có thể do xóa WM/gen đang nghẽn máy)"
                )
                await asyncio.sleep(wait)
        if body:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(body)
            log.info(f"Downloaded video via browser: {output_path} ({len(body)} bytes)")
            return True

        # Method 2: get signed URL then download via httpx
        url = await self.get_download_url(media_id)
        if url:
            return await self.download_video(url, output_path)

        log.error(f"download_to: no download method worked for media_id={media_id}")
        return False
