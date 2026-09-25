"""FlowSession — Direct Python HTTP transport for Google Flow batchexecute RPCs.

Architectural Background:
=========================
Reverse-engineered from G-Labs Studio (`src.flow_google.transport.FlowSession`).
Previously, RedOne routed batchexecute calls by injecting fetch() into the user's
open Chrome tab (via `chrome.scripting.executeScript({world: 'MAIN'})`).
Because Google Flow is an Angular SPA, unsolicited fetch() calls running inside
the page without trusted user events (`event.isTrusted`) and without proper
Angular Zone telemetry trigger Google's anti-bot gateway, returning:
    [7, null, [["type.googleapis.com/google.rpc.ErrorInfo", ["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]]
(Code 7 Permission Denied).

In G-Labs Studio:
- The Chrome extension NEVER executes batchexecute! It only harvests reCAPTCHA tokens
  and exports decrypted cookies.
- All batchexecute calls are made DIRECTLY FROM PYTHON via HTTP POST with standard
  headers (Origin, Referer, X-Same-Domain: 1, Cookie, User-Agent) + CSRF token `at`
  (scraped from https://flow.google.com/about) + genuine reCAPTCHA Enterprise token.
- Google treats direct Python calls as clean client-to-server RPCs without Angular
  DOM validation traps, bypassing error 403 / Code 7 completely.
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

log = logging.getLogger("redone.flow_session")

# Standard reCAPTCHA Enterprise site key for Google Flow
FLOW_RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

# gRPC status code to descriptive name
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

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def get_sec_ch_ua_headers(user_agent: str) -> dict[str, str]:
    """Derive Sec-Ch-Ua client hint headers from user agent string (parity with G-Labs src.ua_utils)."""
    m = re.search(r"Chrome/(\d+)\.", user_agent)
    ver = m.group(1) if m else "153"

    if "Macintosh" in user_agent:
        plat = '"macOS"'
    elif "Linux" in user_agent:
        plat = '"Linux"'
    else:
        plat = '"Windows"'

    return {
        "sec-ch-ua": f'"Google Chrome";v="{ver}", "Not_A Brand";v="8", "Chromium";v="{ver}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": plat,
    }


class FlowAuthError(Exception):
    """Raised when Google Flow session is dead or requires re-login."""


class FlowTokenError(Exception):
    """Raised when CSRF token `at` is invalid or expired."""


class FlowRpcError(Exception):
    """Raised when Google batchexecute returns an RPC-level error."""
    def __init__(self, message: str, code: Optional[int] = None, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def parse_envelope(text: str) -> tuple[list[dict], list[str]]:
    """Parse Google batchexecute response envelope.

    Google formats responses as:
        )]}'\\n\\n<length>\\n<JSON array>\\n<length>\\n<JSON array>...

    Returns:
        tuple of (chunks, errors) where each chunk is a parsed JSON object.
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
            # Not a complete line JSON or non-JSON boundary, try substring match
            pass

    # If line parsing didn't find anything, try bracket depth parsing
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

    Chunk structure:
        [["wrb.fr", rpc_id, "<inner JSON string>", null, null, null, "generic"], ...]
    Or on error:
        [["wrb.fr", rpc_id, null, null, null, null, null, [code, message, details]]]
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

                # Inner is None -> check genuine error slot (typically index 7 or a list at index > 2)
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

        # Scraped state from bootstrap
        self.at_token: Optional[str] = None
        self.build_label: Optional[str] = None
        self.session_id: Optional[str] = None
        self.auth_index: str = "0"
        self.last_bootstrap_ts: float = 0.0

        self._lock = asyncio.Lock()
        self._http_client: Optional[httpx.AsyncClient] = None

    def update_cookies_from_list(self, cookies: list[dict]) -> None:
        """Update cookie string from list of cookie dicts returned by Chrome extension.

        Applies strict G-Labs Studio cookie filtering rules for flow.google.com:
        1. Only include cookies valid for flow.google.com:
           - Host-only cookies (domain == 'flow.google.com' or '.flow.google.com')
           - Root domain cookies (domain == '.google.com' or 'google.com')
           - Reject foreign hosts (e.g. accounts.google.com, youtube.com, labs.google)
        2. Host-specific cookies (especially OSID & __Secure-OSID on flow.google.com)
           take precedence over any root .google.com cookies of the same name.
        """
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

        # Host-specific cookies override domain cookies
        merged = dict(domain_cookies)
        merged.update(host_cookies)

        has_osid = "OSID" in merged or "__Secure-OSID" in merged
        has_sid = "SID" in merged or "__Secure-1PSID" in merged or "__Secure-3PSID" in merged

        parts = [f"{k}={v}" for k, v in merged.items()]
        self.cookie_str = "; ".join(parts)
        log.info(
            f"[{self.account_email}] FlowSession cookies updated: {len(parts)} cookies "
            f"(host_flow={len(host_cookies)}, domain_google={len(domain_cookies)}, "
            f"has_sid={has_sid}, has_osid={has_osid}, len={len(self.cookie_str)})"
        )


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
            if not force and self.at_token and (asyncio.get_event_loop().time() - self.last_bootstrap_ts < 1800):
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

            url = f"https://flow.google.com/u/{self.auth_index}/about" if self.auth_index != "0" else "https://flow.google.com/about"
            log.info(f"[{self.account_email}] Bootstrapping FlowSession via {url}...")

            try:
                resp = await client.get(url, headers=headers)
            except Exception as e:
                raise FlowAuthError(f"Failed to reach flow.google.com: {e}") from e

            final_url = str(resp.url)
            if "accounts.google.com" in final_url:
                raise FlowAuthError(f"Session dead (redirected to {final_url})")

            # Extract auth_index from redirect URL
            m_auth = re.search(r"/u/(\d+)", final_url)
            if m_auth:
                self.auth_index = m_auth.group(1)

            html = resp.text

            # 1. Scrape SNlM0e (CSRF token `at`)
            m_at = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html)
            if not m_at:
                # Fallback: search without quotes
                m_at = re.search(r'SNlM0e["\']?\s*:\s*["\']([^"\']+)["\']', html)
            if not m_at:
                raise FlowAuthError("CSRF token SNlM0e not found in Flow page (not logged in or session expired)")
            self.at_token = m_at.group(1)

            # 2. Scrape cfb2h (build label `bl`)
            m_bl = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', html)
            self.build_label = m_bl.group(1) if m_bl else "boq_labs-ai-sandbox-frontend_20260922.00_p0"

            # 3. Scrape FdrFJe (session ID `f.sid`)
            m_sid = re.search(r'"FdrFJe"\s*:\s*"?(-?\w+)"?', html)
            self.session_id = m_sid.group(1) if m_sid else "-1"

            self.last_bootstrap_ts = asyncio.get_event_loop().time()
            log.info(
                f"[{self.account_email}] FlowSession bootstrap OK: auth_index={self.auth_index}, "
                f"at={self.at_token[:15]}..., bl={self.build_label}, sid={self.session_id}"
            )

    async def execute(
        self,
        rpc_id: str,
        inner_payload: Any,
        source_path: str = "/",
        timeout_ms: int = 120000,
    ) -> dict:
        """Execute a batchexecute RPC directly from Python.

        Matches the return interface of browser_bridge.batch_execute:
            {"status": int, "rpc_result": Any, "error": Optional[str], "chunks": list}
        """
        for attempt in range(2):
            if not self.at_token or not self.build_label:
                try:
                    await self.bootstrap()
                except Exception as e:
                    return {"status": 401, "rpc_result": None, "error": str(e), "chunks": []}

            client = await self._get_client()

            # Ensure source_path matches /u/{auth_index}/ if default
            eff_source = source_path
            if eff_source == "/" and self.auth_index != "0":
                eff_source = f"/u/{self.auth_index}/"

            req_id = random.randint(100000, 999999)
            base_url = (
                f"https://flow.google.com/u/{self.auth_index}/_/AiSandboxAngularFrontend/data/batchexecute"
                if self.auth_index != "0"
                else "https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute"
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

            ch_headers = get_sec_ch_ua_headers(self.user_agent)
            headers = {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://flow.google.com",
                "Referer": "https://flow.google.com/",
                "X-Same-Domain": "1",
                "User-Agent": self.user_agent,
                "Cookie": self.cookie_str,
                "sec-ch-ua": ch_headers["sec-ch-ua"],
                "sec-ch-ua-mobile": ch_headers["sec-ch-ua-mobile"],
                "sec-ch-ua-platform": ch_headers["sec-ch-ua-platform"],
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9,vi;q=0.8",
            }

            log.info(f"[{self.account_email}] POST {rpc_id} (attempt {attempt + 1}) to {base_url}...")

            timeout_sec = max(5.0, timeout_ms / 1000.0)
            try:
                resp = await client.post(
                    url,
                    data=form_data,
                    headers=headers,
                    timeout=httpx.Timeout(timeout_sec, connect=15.0),
                )
            except Exception as e:
                log.warning(f"[{self.account_email}] Direct POST {rpc_id} network error: {e}")
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

            log.info(f"[{self.account_email}] {rpc_id} HTTP 200, len={len(raw_text)}, preview: {raw_text[:250]}")
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
