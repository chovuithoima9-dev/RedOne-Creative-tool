"""RedOne GenFill — Standalone Server

A lightweight, self-contained FastAPI server for the Photoshop GenFill plugin.
Runs independently of the main RedOne Creative Tool but shares the same
Chrome extension ("RedOne Auth Helper") for Google Flow API access.

Usage:
    python ps_genfill_server.py [--port 8000]

Architecture:
    PS CEP Panel ──HTTP──► This Server ──bridge──► Chrome Extension ──► Google Flow API

The server implements:
    1. /sync/* endpoints (bridge protocol for Chrome extension communication)
    2. /api/ps-genfill/* endpoints (GenFill functionality)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("genfill")

# ── Configuration ────────────────────────────────────────────────────
AISANDBOX_BASE = "https://aisandbox-pa.googleapis.com/v1"
MAX_RETRY_COUNT = 3
XOR_KEY = 0x5A
TASK_TTL_S = 120.0
EXT_LIVE_THRESHOLD_S = 60.0
OUTPUT_DIR = Path.home() / "RedOne_GenFill_Output"

# =====================================================================
# BROWSER BRIDGE (simplified from backend/services/browser_bridge.py)
# =====================================================================

class BridgeTimeoutError(Exception):
    pass

class BridgeExtensionOfflineError(Exception):
    pass

class SessionDeadError(Exception):
    def __init__(self, email: str, reason: str):
        self.email = email
        self.reason = reason
        super().__init__(f"[{email}] {reason}")


_enq_counter = iter(range(10**9))


@dataclass(order=True)
class _BridgeTask:
    sort_key: tuple = field(compare=True)
    id: str = field(compare=False)
    kind: str = field(compare=False)
    payload: dict = field(compare=False)
    created_at: float = field(default_factory=time.time, compare=False)
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_event_loop().create_future(),
        compare=False,
    )

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > TASK_TTL_S

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "payload": self.payload}


class BrowserBridge:
    def __init__(self):
        self._pending: asyncio.PriorityQueue[_BridgeTask] = asyncio.PriorityQueue()
        self._in_flight: dict[str, _BridgeTask] = {}
        self._ext_last_poll: float = 0.0
        self._ext_last_ready_poll: float = 0.0
        self._ext_last_status: str = "unknown"
        self._ext_last_url: str = ""

    def update_tab_state(self, status: str, url: str) -> None:
        self._ext_last_poll = time.time()
        self._ext_last_status = status or "unknown"
        self._ext_last_url = url or ""
        if status == "ready":
            self._ext_last_ready_poll = time.time()

    def bump_liveness(self) -> None:
        self._ext_last_poll = time.time()

    def is_extension_live(self) -> bool:
        return (time.time() - self._ext_last_poll) < EXT_LIVE_THRESHOLD_S

    def is_ready_extension_live(self) -> bool:
        return (time.time() - self._ext_last_ready_poll) < EXT_LIVE_THRESHOLD_S

    def snapshot_state(self) -> dict:
        return {
            "extension_live": self.is_extension_live(),
            "last_poll_age_s": round(time.time() - self._ext_last_poll, 1)
                if self._ext_last_poll else None,
            "last_tab_status": self._ext_last_status,
            "last_tab_url": self._ext_last_url,
            "pending_tasks": self._pending.qsize(),
            "in_flight_tasks": len(self._in_flight),
        }

    async def pop_task_for_extension(self, timeout: float = 0.0,
                                     tab_status: str = "ready") -> Optional[_BridgeTask]:
        if tab_status and tab_status != "ready":
            return None
        try:
            task = self._pending.get_nowait() if timeout <= 0 else \
                await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None
        while task.is_expired():
            try:
                task = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                return None
        self._in_flight[task.id] = task
        return task

    def deliver_result(self, task_id: str, result: dict) -> bool:
        task = self._in_flight.pop(task_id, None)
        if task is None:
            return False
        if not task.future.done():
            task.future.set_result(result)
        return True

    async def _enqueue_and_wait(self, kind: str, payload: dict) -> dict:
        if not self.is_extension_live():
            raise BridgeExtensionOfflineError(
                "Extension chưa kết nối. Mở Chrome có cài 'RedOne Auth Helper' "
                "+ tab labs.google đã đăng nhập."
            )
        if not self.is_ready_extension_live() and not self._in_flight:
            raise BridgeExtensionOfflineError(
                "Đã thấy extension nhưng CHƯA có tab labs.google đã đăng nhập. "
                "Mở tab https://labs.google/fx/tools/flow, đăng nhập, rồi thử lại."
            )
        enq = next(_enq_counter)
        task = _BridgeTask(
            sort_key=(2, 0, enq),
            id=uuid.uuid4().hex,
            kind=kind,
            payload=payload,
        )
        await self._pending.put(task)
        try:
            return await asyncio.wait_for(task.future, timeout=TASK_TTL_S)
        except asyncio.TimeoutError:
            self._in_flight.pop(task.id, None)
            raise BridgeTimeoutError(f"Extension không phản hồi sau {TASK_TTL_S}s")

    async def harvest_recaptcha(self, site_key: str = "",
                                action: str = "IMAGE_GENERATION") -> str:
        result = await self._enqueue_and_wait("recaptcha", {
            "site_key": site_key, "action": action,
        })
        token = result.get("token")
        if not token:
            raise RuntimeError(f"reCAPTCHA harvest failed: {result.get('error', 'unknown')}")
        return token

    async def proxy_fetch(self, url: str, method: str = "GET",
                          headers: Optional[dict] = None, body: Optional[str] = None,
                          response_mode: str = "json", timeout_ms: int = 60000) -> dict:
        return await self._enqueue_and_wait("proxy_fetch", {
            "url": url, "method": method, "headers": headers or {},
            "body": body, "response_mode": response_mode, "timeout_ms": timeout_ms,
        })

    async def proxy_fetch_binary(self, url: str, method: str = "GET",
                                 headers: Optional[dict] = None, body: Optional[str] = None,
                                 timeout_ms: int = 120000) -> tuple[int, bytes, dict]:
        result = await self.proxy_fetch(url=url, method=method, headers=headers,
                                        body=body, response_mode="arraybuffer",
                                        timeout_ms=timeout_ms)
        if result.get("error"):
            raise RuntimeError(f"proxy_fetch_binary: {result['error']}")
        b64 = result.get("body_b64", "")
        raw = base64.b64decode(b64) if b64 else b""
        return result.get("status", 0), raw, result.get("headers", {})


bridge = BrowserBridge()


# =====================================================================
# XOR CODEC (parity with extension/background.js)
# =====================================================================

def xor_encode(plaintext: str) -> str:
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
    result_chars: list[str] = []
    for i in range(0, len(hex_string), 2):
        byte = int(hex_string[i:i + 2], 16) ^ XOR_KEY
        result_chars.append(chr(byte))
    return "".join(result_chars)


def envelope(payload: dict) -> dict:
    return {"d": xor_encode(json.dumps(payload, ensure_ascii=True))}


def unwrap(raw: Any) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("d"), str):
        return json.loads(xor_decode(raw["d"]))
    if isinstance(raw, dict):
        return raw
    raise ValueError("expected JSON object or envelope dict")


# =====================================================================
# FLOW CLIENT (simplified — only what GenFill needs)
# =====================================================================

class FlowClient:
    """Minimal FlowClient for GenFill — upload images and generate with mask."""

    IMAGE_MODEL_MAP = {
        "nano_banana_pro": "GEM_PIX_2",
        "nano_banana_2": "HARBOR_SEAL",
        "nano_banana_lite": "HARBOR_SEAL",
        "imagen_4": "IMAGEN_3_5",
    }

    def __init__(self):
        self._token: Optional[str] = None
        self._token_ts: float = 0.0
        self.project_id = ""
        self._upload_locks: dict[str, asyncio.Lock] = {}

    async def ensure_token(self) -> None:
        if self._token and (time.time() - self._token_ts) < 1800:
            return
        await self._fetch_token()

    async def _fetch_token(self) -> None:
        log.info("Fetching auth session via extension bridge...")
        for attempt in range(1, 4):
            try:
                r = await bridge.proxy_fetch(
                    url="https://labs.google/fx/api/auth/session",
                    method="GET",
                    headers={"Accept": "application/json"},
                    response_mode="json",
                    timeout_ms=15000,
                )
            except BridgeExtensionOfflineError as e:
                raise SessionDeadError("genfill", str(e))

            status = r.get("status", 0)
            body = r.get("body")

            if status == 200 and isinstance(body, dict) and body.get("access_token"):
                self._token = body["access_token"]
                self._token_ts = time.time()
                log.info(f"Got token ya29...{self._token[-8:]}")
                # Extract project_id from page or use default
                if not self.project_id:
                    await self._fetch_project_id()
                return

            if status == 0 and attempt < 3:
                await asyncio.sleep(1.0 * attempt)
                continue

            raise SessionDeadError("genfill",
                "Không lấy được session. Kiểm tra Chrome extension + tab labs.google")

    async def _fetch_project_id(self) -> None:
        """Fetch the user's project ID from the Flow API."""
        try:
            r = await bridge.proxy_fetch(
                url="https://labs.google/fx/api/trpc/user.getOrCreateUser",
                method="GET",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                response_mode="json",
                timeout_ms=15000,
            )
            body = r.get("body", {})
            # tRPC response structure: { result: { data: { json: { ... } } } }
            data = body
            if isinstance(data, dict):
                result = data.get("result", {})
                if isinstance(result, dict):
                    json_data = result.get("data", {}).get("json", {})
                    pid = json_data.get("projectId", "")
                    if pid:
                        self.project_id = pid
                        log.info(f"Project ID: {pid}")
                        return
            # Fallback: try to get from the credits endpoint
            log.warning("Could not get projectId from tRPC, trying credits...")
        except Exception as e:
            log.warning(f"_fetch_project_id error: {e}")

        # Fallback — extract from any aisandbox call
        if not self.project_id:
            self.project_id = "default"

    async def _browser_sandbox_request(self, endpoint: str, payload: dict,
                                       is_text_plain: bool = False) -> dict:
        """Route request through Chrome extension bridge."""
        await self.ensure_token()
        token = self._token or ""
        url = f"{AISANDBOX_BASE}/{endpoint.lstrip('/')}"
        body_json = json.dumps(payload, ensure_ascii=False)
        ct = "text/plain;charset=UTF-8" if is_text_plain else "application/json"

        for attempt in range(1, MAX_RETRY_COUNT + 1):
            try:
                r = await bridge.proxy_fetch(
                    url=url, method="POST",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": ct},
                    body=body_json, response_mode="json", timeout_ms=120000,
                )
            except BridgeExtensionOfflineError as e:
                return {"error": str(e)}
            except BridgeTimeoutError as e:
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2 * attempt)
                    continue
                return {"error": str(e)}

            if r.get("error"):
                if attempt < MAX_RETRY_COUNT:
                    await asyncio.sleep(2 * attempt)
                    continue
                return {"error": r["error"]}

            status = r.get("status", 0)
            if status in (401, 403) and attempt < MAX_RETRY_COUNT:
                body = r.get("body")
                err_text = json.dumps(body)[:200] if isinstance(body, dict) else str(body)[:200]
                low = err_text.lower()
                if "recaptcha" in low or "unusual_activity" in low:
                    return {"error": f"HTTP {status}", "text": err_text}
                self._token = None
                await self.ensure_token()
                token = self._token or ""
                await asyncio.sleep(1)
                continue

            if 200 <= status < 300:
                body = r.get("body")
                return body if isinstance(body, dict) else {"text": str(body)[:2000]}

            body = r.get("body")
            err_text = json.dumps(body)[:300] if isinstance(body, dict) else str(body)[:300]
            if attempt < MAX_RETRY_COUNT and status >= 500:
                await asyncio.sleep(2 * attempt)
                continue
            return {"error": f"HTTP {status}", "text": err_text}

        return {"error": "Max retries exceeded"}

    async def upload_image(self, file_path: str) -> Optional[str]:
        """Upload an image to Flow and return its media_id."""
        import hashlib
        file_bytes = Path(file_path).read_bytes()
        sha = hashlib.sha256(file_bytes).hexdigest()[:16]

        if sha not in self._upload_locks:
            self._upload_locks[sha] = asyncio.Lock()

        async with self._upload_locks[sha]:
            return await self._do_upload_image(file_bytes, file_path)

    async def _do_upload_image(self, file_bytes: bytes, file_path: str) -> Optional[str]:
        """Actual upload implementation."""
        await self.ensure_token()
        token = self._token or ""

        # Step 1: Get upload URL
        import mimetypes
        mime = mimetypes.guess_type(file_path)[0] or "image/png"
        size = len(file_bytes)

        init_payload = {
            "protocolVersion": "0.8",
            "createSessionRequest": {
                "fields": [{
                    "external": {
                        "name": "file",
                        "filename": Path(file_path).name,
                        "put": {},
                        "size": size,
                    }
                }]
            }
        }

        try:
            r = await bridge.proxy_fetch(
                url=f"https://content-autopush.googleapis.com/upload/aisandbox/v1/"
                    f"projects/{self.project_id}/flowMedia:uploadMedia"
                    f"?uploadType=resumable&upload_id=&upload_protocol=resumable",
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Header-Content-Length": str(size),
                    "X-Goog-Upload-Header-Content-Type": mime,
                },
                body=json.dumps(init_payload),
                response_mode="json",
                timeout_ms=30000,
            )

            upload_url = r.get("headers", {}).get("x-goog-upload-url", "")
            if not upload_url:
                # Try alternate header case
                for k, v in r.get("headers", {}).items():
                    if k.lower() == "x-goog-upload-url":
                        upload_url = v
                        break

            if not upload_url:
                log.error(f"No upload URL in response: {r}")
                return None

            # Step 2: Upload the bytes
            b64_data = base64.b64encode(file_bytes).decode()
            r2 = await bridge.proxy_fetch(
                url=upload_url,
                method="PUT",
                headers={
                    "Content-Type": mime,
                    "X-Goog-Upload-Command": "upload, finalize",
                    "X-Goog-Upload-Offset": "0",
                },
                body=b64_data,
                response_mode="json",
                timeout_ms=60000,
            )

            body = r2.get("body", {})
            if isinstance(body, dict):
                media_id = body.get("name", "")
                if media_id:
                    log.info(f"Uploaded image: {media_id}")
                    return media_id

            log.error(f"Upload response missing name: {r2}")
            return None

        except Exception as e:
            log.error(f"Upload image error: {e}")
            return None

    async def get_recaptcha_token(self, action: str = "IMAGE_GENERATION") -> str:
        try:
            token = await bridge.harvest_recaptcha(site_key="", action=action)
            return token
        except Exception as e:
            log.warning(f"reCAPTCHA harvest failed: {e}")
            return ""

    async def generate_image_with_mask(
        self, prompt: str, base_image_id: str, mask_image_id: str,
        model_key: str = "nano_banana_pro", seed: Optional[int] = None,
    ) -> dict:
        """Generate image with mask-based editing (generative fill)."""
        await self.ensure_token()

        import random
        if seed is None:
            seed = random.randint(100000, 999999)

        model_name = self.IMAGE_MODEL_MAP.get(model_key, model_key)

        image_inputs = [
            {"imageInputType": "IMAGE_INPUT_TYPE_BASE", "name": base_image_id},
            {"imageInputType": "IMAGE_INPUT_TYPE_MASK", "name": mask_image_id},
        ]

        recaptcha_token = await self.get_recaptcha_token("IMAGE_GENERATION")

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

        request_item = {
            "clientContext": client_context,
            "imageModelName": model_name,
            "imageAspectRatio": "IMAGE_ASPECT_RATIO_UNSPECIFIED",
            "structuredPrompt": {"parts": [{"text": prompt}]},
            "seed": seed,
            "imageInputs": image_inputs,
        }

        payload = {
            "clientContext": client_context,
            "mediaGenerationContext": {
                "batchId": uuid.uuid4().hex[:8] + "-" + uuid.uuid4().hex[:4] + "-" +
                           uuid.uuid4().hex[:4] + "-" + uuid.uuid4().hex[:4] + "-" +
                           uuid.uuid4().hex[:12],
            },
            "useNewMedia": True,
            "requests": [request_item],
        }

        endpoint = f"projects/{self.project_id}/flowMedia:batchGenerateImages"
        log.info(f"GenFill: model={model_name}, seed={seed}")

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
                is_throttled = "429" in err_str or "resource_exhausted" in err_lc
                is_recaptcha = "403" in err_str or "recaptcha" in err_lc

                if is_throttled and attempt < 4:
                    delay = min(60, (5 * (attempt + 1)) + _rand.uniform(2, 8))
                    log.warning(f"GenFill throttled (attempt {attempt+1}/5), backoff {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                if is_recaptcha and attempt < 4:
                    log.warning(f"GenFill reCAPTCHA rejected (attempt {attempt+1}/5)")
                    self._token = None
                    await self.ensure_token()
                    continue
                raise ValueError(err_str)
            break
        else:
            raise ValueError(str(result.get("text", result.get("error", "Unknown"))))

        return self._extract_image_result(result)

    def _extract_image_result(self, result: dict) -> dict:
        """Extract image result from batchGenerateImages response."""
        # Response structure: { "imagePanels": [{ "generatedImages": [{ ... }] }] }
        panels = result.get("imagePanels") or result.get("image_panels", [])
        if not panels:
            # Try flat structure
            images = result.get("generatedImages") or result.get("generated_images", [])
            if not images:
                raise ValueError(f"No images in response: {json.dumps(result)[:500]}")
        else:
            images = panels[0].get("generatedImages") or panels[0].get("generated_images", [])

        if not images:
            raise ValueError(f"No generated images in panel: {json.dumps(result)[:500]}")

        img = images[0]
        media_id = img.get("mediaGenerationId") or img.get("media_generation_id", "")
        encoded = img.get("encodedImage") or img.get("encoded_image", "")

        # Build download URL from fifeUrl or encoded image
        fife_url = ""
        if isinstance(encoded, dict):
            fife_url = encoded.get("fifeUrl") or encoded.get("fife_url", "")
        elif isinstance(encoded, str) and encoded.startswith("http"):
            fife_url = encoded

        seed_val = img.get("seed") or img.get("randomSeed", 0)
        width = img.get("width", 0)
        height = img.get("height", 0)

        if isinstance(encoded, dict):
            width = width or encoded.get("imageWidth") or encoded.get("width", 0)
            height = height or encoded.get("imageHeight") or encoded.get("height", 0)

        return {
            "media_id": media_id,
            "download_url": fife_url,
            "seed": seed_val,
            "width": width,
            "height": height,
        }


# =====================================================================
# FASTAPI APP
# =====================================================================

flow_client = FlowClient()

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("═══ RedOne GenFill Server starting ═══")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    yield
    log.info("═══ RedOne GenFill Server stopping ═══")


app = FastAPI(title="RedOne GenFill", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # PS CEP panel needs unrestricted access
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Bridge Protocol Endpoints (for Chrome extension) ─────────────────

@app.get("/sync/status")
async def sync_status():
    bridge.bump_liveness()
    return {"ok": True, "ts": time.time(), "service": "redone-bridge"}


@app.get("/sync/next-task")
async def sync_next_task(tab_status: str = "ready", tab_url: str = "",
                         capacity: int = 1):
    bridge.update_tab_state(tab_status, tab_url)
    if capacity <= 0:
        return {"task": None}
    task = await bridge.pop_task_for_extension(timeout=0.0, tab_status=tab_status)
    if task is None:
        return {"task": None}
    return envelope({"task": task.to_dict()})


@app.post("/sync/task-result")
async def sync_task_result(request: Request):
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
    return {"ok": delivered}


@app.get("/sync/state")
async def sync_state():
    return bridge.snapshot_state()


# Stub endpoints the extension may call — return empty/ok to prevent errors
@app.get("/sync/shared-google")
async def sync_shared_google():
    return envelope({"ok": False, "reason": "Standalone GenFill mode — no shared accounts"})


# ── GenFill Endpoints ────────────────────────────────────────────────

def _ensure_mask_format(mask_bytes: bytes) -> bytes:
    """Ensure mask is proper black/white PNG."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(mask_bytes))
        if img.mode != "L":
            img = img.convert("L")
        img = img.point(lambda x: 255 if x > 127 else 0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        log.warning("Pillow not installed — using raw mask bytes")
        return mask_bytes
    except Exception as e:
        log.warning(f"Mask format conversion failed ({e}), using raw bytes")
        return mask_bytes


@app.post("/api/ps-genfill/generate")
async def generate_genfill(
    image: UploadFile = File(...),
    mask: UploadFile = File(...),
    prompt: str = Form(...),
    model: str = Form("nano_banana_pro"),
):
    """Generate fill for a masked region of an image."""
    image_bytes = await image.read()
    mask_bytes = await mask.read()

    if len(image_bytes) < 100:
        raise HTTPException(400, "Image file quá nhỏ hoặc rỗng")
    if len(mask_bytes) < 100:
        raise HTTPException(400, "Mask file quá nhỏ hoặc rỗng")

    valid_models = {"nano_banana_pro", "nano_banana_2", "imagen_4"}
    if model not in valid_models:
        raise HTTPException(400, f"Model không hợp lệ. Chọn: {', '.join(valid_models)}")

    mask_bytes = _ensure_mask_format(mask_bytes)

    # Save to temp files
    tmp_dir = Path(tempfile.mkdtemp(prefix="genfill_"))
    image_path = tmp_dir / "source.png"
    mask_path = tmp_dir / "mask.png"
    image_path.write_bytes(image_bytes)
    mask_path.write_bytes(mask_bytes)

    try:
        await flow_client.ensure_token()

        log.info(f"GenFill: prompt='{prompt[:60]}', model={model}, "
                 f"image={len(image_bytes)}B, mask={len(mask_bytes)}B")

        base_media_id = await flow_client.upload_image(str(image_path))
        if not base_media_id:
            raise HTTPException(500, "Upload ảnh gốc thất bại")

        mask_media_id = await flow_client.upload_image(str(mask_path))
        if not mask_media_id:
            raise HTTPException(500, "Upload mask thất bại")

        log.info(f"Uploaded: base={base_media_id}, mask={mask_media_id}")

        result = await flow_client.generate_image_with_mask(
            prompt=prompt,
            base_image_id=base_media_id,
            mask_image_id=mask_media_id,
            model_key=model,
        )

        download_url = result.get("download_url", "")
        if not download_url:
            raise HTTPException(500, "Không có URL kết quả từ Flow API")

        import httpx
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
            resp = await http.get(download_url)
            if resp.status_code != 200 or len(resp.content) < 500:
                raise HTTPException(500, f"Download kết quả thất bại (HTTP {resp.status_code})")
            result_bytes = resp.content

        out_dir = OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"genfill_{int(time.time())}.png"
        out_path = out_dir / out_name
        out_path.write_bytes(result_bytes)

        result_b64 = base64.b64encode(result_bytes).decode("utf-8")

        log.info(f"GenFill done: {result.get('width')}x{result.get('height')}, saved to {out_path}")

        return {
            "ok": True,
            "image_base64": result_b64,
            "media_id": result.get("media_id"),
            "width": result.get("width"),
            "height": result.get("height"),
            "seed": result.get("seed"),
            "output_path": str(out_path),
        }

    except SessionDeadError as sde:
        raise HTTPException(401, f"Session hết hạn: {sde.reason}")
    except ValueError as ve:
        raise HTTPException(500, str(ve))
    except HTTPException:
        raise
    except Exception as ex:
        log.error(f"GenFill error: {ex}", exc_info=True)
        raise HTTPException(500, f"Lỗi: {str(ex)}")
    finally:
        for p in [image_path, mask_path]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


@app.get("/api/ps-genfill/health")
async def genfill_health():
    return {
        "ok": True,
        "mode": "standalone",
        "has_extension": bridge.is_extension_live(),
        "extension_status": bridge._ext_last_status,
    }


@app.get("/api/health")
async def health():
    """Generic health check — also queried by the Chrome extension."""
    return {"ok": True, "service": "redone-genfill-standalone"}


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RedOne GenFill Standalone Server")
    parser.add_argument("--port", type=int, default=8001,
                        help="Port to listen on (default: 8001)")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Host to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════╗
║      RedOne GenFill — Standalone Server      ║
╠══════════════════════════════════════════════╣
║  Port:   {args.port:<36} ║
║  Output: {str(OUTPUT_DIR):<36} ║
╠══════════════════════════════════════════════╣
║  1. Mở Chrome + cài 'RedOne Auth Helper'     ║
║  2. Mở tab labs.google/fx + đăng nhập Google  ║
║  3. Mở Photoshop → Extensions → RedOne GenFill║
╚══════════════════════════════════════════════╝
""")

    uvicorn.run(app, host=args.host, port=args.port,
                log_level="info", access_log=False)
