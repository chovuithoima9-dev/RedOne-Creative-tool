"""Google Flow Direct HTTP Transport Engine (FlowSession).

Handles:
- Cookie domain/host filtering (strictly adhering to G-Labs Studio v2.0.4).
- Automatic scraping and caching of CSRF token `at` (SNlM0e), build label (cfb2h), and session id (FdrFJe).
- Direct HTTP POST to batchexecute endpoints with proper browser headers and Client Hints.
- Parsing and unpacking of Google BOQ response envelopes (wrb.fr).
- Google Resumable Media Upload protocol for video assets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import urllib.parse
from typing import Any, Optional

import httpx

from .config import (
    DEFAULT_SEC_CH_UA,
    DEFAULT_USER_AGENT,
    FLOW_ABOUT_URL,
    FLOW_APP_NAME,
    FLOW_ORIGIN,
)

log = logging.getLogger("redone.flow.transport")

# Mapping of gRPC status code to descriptive names
GRPC_STATUS_MAP: dict[int, str] = {
    1: "CANCELLED",
    2: "UNKNOWN",
    3: "INVALID_ARGUMENT",
    4: "DEADLINE_EXCEEDED",
    5: "NOT_FOUND",
    6: "ALREADY_EXISTS",
    7: "PERMISSION_DENIED",
    8: "RESOURCE_EXHAUSTED",
    9: "FAILED_PRECONDITION",
    10: "ABORTED",
    11: "OUT_OF_RANGE",
    12: "UNIMPLEMENTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
    15: "DATA_LOSS",
    16: "UNAUTHENTICATED",
}


class FlowAuthError(Exception):
    """Raised when Google Flow session is unauthenticated or cookie is dead."""


class FlowTokenError(Exception):
    """Raised when CSRF token `at` is invalid or expired."""


class FlowRpcError(Exception):
    """Raised when Google batchexecute returns an RPC error code."""
    def __init__(self, message: str, code: Optional[int] = None, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def parse_envelope(text: str) -> tuple[list[dict | list], list[str]]:
    """Parse Google batchexecute response envelope.

    Google formats responses as:
        )]}'\\n\\n<length>\\n<JSON array>\\n<length>\\n<JSON array>...
    """
    chunks: list[dict | list] = []
    errors: list[str] = []

    clean = text.strip()
    if clean.startswith(")]}'"):
        clean = clean[4:].strip()

    lines = clean.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.isdigit():
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, list):
                chunks.append(parsed)
        except Exception:
            pass

    # Fallback to bracket depth parsing if line parsing yielded nothing
    if not chunks and "[" in clean:
        in_str = False
        esc = False
        depth = 0
        start = -1
        for i, ch in enumerate(clean):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "[":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0 and start != -1:
                        block = clean[start : i + 1]
                        try:
                            parsed = json.loads(block)
                            if isinstance(parsed, list):
                                chunks.append(parsed)
                        except Exception:
                            pass
                        start = -1

    return chunks, errors


def extract_rpc_result(chunks: list, rpc_id: str) -> tuple[Optional[Any], Optional[str]]:
    """Extract inner RPC data or error from parsed chunks.

    Standard row format:
        ["wrb.fr", rpc_id, "<inner JSON string>", null, null, null, "generic"]
    Error row format:
        ["wrb.fr", rpc_id, null, null, null, [code, message, details], "generic"]
    """
    for chunk in chunks:
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if not isinstance(row, list) or len(row) < 3:
                continue
            if row[0] == "wrb.fr" and row[1] == rpc_id:
                inner_raw = row[2]
                if inner_raw is not None:
                    if isinstance(inner_raw, str):
                        try:
                            return json.loads(inner_raw), None
                        except Exception as e:
                            log.warning(f"Failed to parse inner JSON for {rpc_id}: {e}")
                            return inner_raw, None
                    return inner_raw, None

                # Search error slot
                err_data = None
                for idx in range(3, len(row)):
                    val = row[idx]
                    if val is not None and val != "generic":
                        err_data = val
                        break

                if err_data:
                    code = None
                    if isinstance(err_data, list) and len(err_data) > 0 and isinstance(err_data[0], int):
                        code = err_data[0]
                    status_name = GRPC_STATUS_MAP.get(code, f"CODE_{code}") if code else "UNKNOWN_ERROR"
                    return None, f"RPC error: [{code}, {status_name}, {err_data}]"

                return None, f"RPC error: no data returned for {rpc_id}"

    return None, None


class FlowSession:
    """Manages an authenticated Google Flow session directly from Python."""

    def __init__(
        self,
        account_email: str = "",
        cookie_str: str = "",
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.account_email = account_email
        self.cookie_str = cookie_str
        self.user_agent = user_agent

        # Scraped session tokens
        self.at_token: Optional[str] = None
        self.build_label: Optional[str] = None
        self.session_id: Optional[str] = None
        self.auth_index: str = "0"
        self.last_bootstrap_ts: float = 0.0

        self._lock = asyncio.Lock()
        self._http_client: Optional[httpx.AsyncClient] = None

    def update_cookies_from_list(self, cookies: list[dict]) -> None:
        """Filter and update cookie string from extension cookie list."""
        host_cookies: dict[str, str] = {}
        domain_cookies: dict[str, str] = {}

        for c in cookies:
            name = c.get("name")
            val = c.get("value")
            dom = (c.get("domain") or "").lower().strip()
            if not name or val is None:
                continue

            if dom in ("flow.google.com", ".flow.google.com"):
                host_cookies[name] = val
            elif dom in (".google.com", "google.com"):
                if name not in domain_cookies:
                    domain_cookies[name] = val

        merged = dict(domain_cookies)
        merged.update(host_cookies)

        parts = [f"{k}={v}" for k, v in merged.items()]
        self.cookie_str = "; ".join(parts)
        log.info(f"[{self.account_email}] FlowSession cookie updated: {len(parts)} cookies")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(120.0, connect=15.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._http_client

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def bootstrap(self, force: bool = False) -> None:
        """Scrape `at` (CSRF), `cfb2h` (bl), `FdrFJe` (sid) from flow.google.com/about."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if not force and self.at_token and (now - self.last_bootstrap_ts < 1800):
                return

            if not self.cookie_str:
                raise FlowAuthError("No cookies configured for FlowSession")

            client = await self._get_client()
            headers = {
                "User-Agent": self.user_agent,
                "Cookie": self.cookie_str,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            url = f"https://flow.google.com/u/{self.auth_index}/about" if self.auth_index != "0" else FLOW_ABOUT_URL
            log.info(f"[{self.account_email}] Bootstrapping FlowSession via {url}...")

            try:
                resp = await client.get(url, headers=headers)
            except Exception as e:
                raise FlowAuthError(f"Failed to reach flow.google.com: {e}") from e

            final_url = str(resp.url)
            if "accounts.google.com" in final_url:
                raise FlowAuthError("Google Flow session expired (redirected to Google Login)")

            m_auth = re.search(r"/u/(\d+)", final_url)
            if m_auth:
                self.auth_index = m_auth.group(1)

            html = resp.text

            # 1. Scrape SNlM0e (CSRF token at)
            m_at = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html) or re.search(r'SNlM0e["\']?\s*:\s*["\']([^"\']+)["\']', html)
            if not m_at:
                raise FlowAuthError("CSRF token SNlM0e not found in Flow page")
            self.at_token = m_at.group(1)

            # 2. Scrape cfb2h (build label bl)
            m_bl = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', html)
            self.build_label = m_bl.group(1) if m_bl else "boq_labs-ai-sandbox-frontend_20260922.00_p0"

            # 3. Scrape FdrFJe (session ID f.sid)
            m_sid = re.search(r'"FdrFJe"\s*:\s*"?(-?\w+)"?', html)
            self.session_id = m_sid.group(1) if m_sid else "-1"

            self.last_bootstrap_ts = now
            log.info(f"[{self.account_email}] Bootstrap OK: auth_index={self.auth_index}, bl={self.build_label}")

    async def execute(
        self,
        rpc_id: str,
        inner_payload: Any,
        source_path: str = "/",
        timeout_ms: int = 120000,
    ) -> dict:
        """Execute a batchexecute RPC directly from Python."""
        for attempt in range(2):
            if not self.at_token or not self.build_label:
                try:
                    await self.bootstrap()
                except Exception as e:
                    return {"status": 401, "rpc_result": None, "error": str(e), "chunks": []}

            client = await self._get_client()
            eff_source = source_path
            if eff_source == "/" and self.auth_index != "0":
                eff_source = f"/u/{self.auth_index}/"

            req_id = random.randint(100000, 999999)
            base_url = (
                f"{FLOW_ORIGIN}/u/{self.auth_index}/_/{FLOW_APP_NAME}/data/batchexecute"
                if self.auth_index != "0"
                else f"{FLOW_ORIGIN}/_/{FLOW_APP_NAME}/data/batchexecute"
            )

            params = {
                "rpcids": rpc_id,
                "source-path": eff_source,
                "bl": self.build_label or "",
                "f.sid": self.session_id or "-1",
                "hl": "vi",
                "_reqid": str(req_id),
                "rt": "c",
            }
            url = f"{base_url}?{urllib.parse.urlencode(params)}"

            inner_json_str = (
                json.dumps(inner_payload, ensure_ascii=False)
                if not isinstance(inner_payload, str)
                else inner_payload
            )
            f_req = json.dumps([[[rpc_id, inner_json_str, None, "generic"]]], ensure_ascii=False)

            form_data = {
                "f.req": f_req,
                "at": self.at_token or "",
            }

            headers = {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": FLOW_ORIGIN,
                "Referer": f"{FLOW_ORIGIN}/",
                "X-Same-Domain": "1",
                "User-Agent": self.user_agent,
                "Cookie": self.cookie_str,
                "sec-ch-ua": DEFAULT_SEC_CH_UA["sec-ch-ua"],
                "sec-ch-ua-mobile": DEFAULT_SEC_CH_UA["sec-ch-ua-mobile"],
                "sec-ch-ua-platform": DEFAULT_SEC_CH_UA["sec-ch-ua-platform"],
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9,vi;q=0.8",
            }

            timeout_sec = max(5.0, timeout_ms / 1000.0)
            try:
                resp = await client.post(
                    url,
                    data=form_data,
                    headers=headers,
                    timeout=httpx.Timeout(timeout_sec, connect=15.0),
                )
            except Exception as e:
                log.warning(f"[{self.account_email}] POST {rpc_id} network error: {e}")
                return {"status": 0, "rpc_result": None, "error": f"network error: {e}", "chunks": []}

            status = resp.status_code
            raw_text = resp.text

            if status == 400 and attempt == 0 and ("at" in raw_text or "token" in raw_text):
                log.warning(f"[{self.account_email}] batchexecute HTTP 400 (token invalid?), re-bootstrapping...")
                await self.bootstrap(force=True)
                continue

            if status != 200:
                log.warning(f"[{self.account_email}] batchexecute HTTP {status}: {raw_text[:200]}")
                return {
                    "status": status,
                    "rpc_result": None,
                    "error": f"batchexecute HTTP {status}",
                    "body_text": raw_text[:500],
                    "chunks": [],
                }

            chunks, errors = parse_envelope(raw_text)
            rpc_result, rpc_err = extract_rpc_result(chunks, rpc_id)

            if rpc_err and attempt == 0 and "UNAUTHENTICATED" in rpc_err:
                log.warning(f"[{self.account_email}] RPC UNAUTHENTICATED, re-bootstrapping...")
                await self.bootstrap(force=True)
                continue

            return {
                "status": 200,
                "rpc_result": rpc_result,
                "error": rpc_err,
                "chunks": chunks,
                "body_text": raw_text if rpc_result is None else None,
            }

        return {"status": 0, "rpc_result": None, "error": "Max retries exceeded in execute", "chunks": []}

    async def upload_video_resumable(
        self,
        project_id: str,
        video_bytes: bytes,
        filename: str = "input.mp4",
        mime_type: str = "video/mp4",
        chunk_size: int = 8 * 1024 * 1024,
    ) -> dict:
        """Upload video using Google Resumable Upload protocol."""
        client = await self._get_client()
        content_len = len(video_bytes)

        start_url = f"{FLOW_ORIGIN}/upload/v1/flow/upload/video/{project_id}"
        headers_start = {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_str,
            "Origin": FLOW_ORIGIN,
            "Referer": f"{FLOW_ORIGIN}/project/{project_id}",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(content_len),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

        log.info(f"[{self.account_email}] Starting resumable video upload ({content_len} bytes)...")
        r_start = await client.post(start_url, headers=headers_start, timeout=30.0)
        if r_start.status_code != 200:
            return {"status": r_start.status_code, "error": f"Start upload failed: {r_start.text[:200]}"}

        upload_url = r_start.headers.get("x-goog-upload-url") or r_start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            return {"status": 0, "error": "No X-Goog-Upload-URL header returned"}

        # Upload & Finalize
        headers_upload = {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_str,
            "Origin": FLOW_ORIGIN,
            "Referer": f"{FLOW_ORIGIN}/project/{project_id}",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Type": mime_type,
        }

        r_upload = await client.post(upload_url, headers=headers_upload, content=video_bytes, timeout=120.0)
        if r_upload.status_code != 200:
            return {"status": r_upload.status_code, "error": f"Upload finalize failed: {r_upload.text[:200]}"}

        try:
            res_json = r_upload.json()
            media_id = res_json.get("mediaId") or res_json.get("media_id")
            return {"status": 200, "media_id": media_id, "data": res_json}
        except Exception:
            return {"status": 200, "media_id": None, "raw_text": r_upload.text}
