"""FlowUIWorker — Google Flow image generation worker via Chrome automation.

Architecture:
- Bypasses Google reCAPTCHA Enterprise bot detection (PUBLIC_ERROR_UNUSUAL_ACTIVITY)
  by generating natively inside the user's real Chrome session.
- Interacts with the Google Flow Angular SPA (.ProseMirror + .generate-icon-button).
- Keeps persistent browser context open across batch tasks for maximum performance (~20-25s per image).
- Automatically downloads full-resolution image bytes (1376x768 / 1024x1024) inside the authenticated session.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from ..config import DATA_DIR

log = logging.getLogger("redone.flow_ui_worker")


def find_chrome_executable() -> Optional[str]:
    """Locate the system Chrome executable on Windows, macOS, or Linux."""
    if os.name == "nt":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome Beta\Application\chrome.exe"),
        ]
        try:
            import winreg
            for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                    path, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    if os.path.isfile(path):
                        return path
                except Exception:
                    pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for p in candidates:
        if os.path.isfile(p):
            return p

    return shutil.which("chrome") or shutil.which("google-chrome")


def format_cookies_for_playwright(raw_cookies: list[dict]) -> list[dict]:
    """Convert Chrome extension cookie dicts to Playwright-compatible format."""
    pw_cookies = []
    for c in raw_cookies:
        name = c.get("name")
        val = c.get("value")
        dom = c.get("domain")
        if not name or not dom:
            continue
        item = {
            "name": name,
            "value": val or "",
            "domain": dom,
            "path": c.get("path") or "/",
        }
        if c.get("expirationDate"):
            item["expires"] = float(c["expirationDate"])
        if c.get("httpOnly") is not None:
            item["httpOnly"] = bool(c["httpOnly"])
        if c.get("secure") is not None:
            item["secure"] = bool(c["secure"])
        ss = (c.get("sameSite") or "").lower()
        if ss in ["strict", "lax", "no_restriction"]:
            map_ss = {"strict": "Strict", "lax": "Lax", "no_restriction": "None"}
            item["sameSite"] = map_ss.get(ss, "Lax")
        pw_cookies.append(item)
    return pw_cookies


def resolve_profile_dir(email: str = "") -> Path:
    """Find or create the Chrome profile directory for Google Flow session.

    Checks:
    1. Exact match in G-Labs Studio profile directory: %APPDATA%/G-Labs Studio/flow_profiles/<email>
    2. Exact match in RedOne profile directory: DATA_DIR/flow_profiles/<email>
    3. If email is provided, create and return DATA_DIR/flow_profiles/<email>.
       NEVER fall back to a different email account if a specific email was requested.
    4. If email is empty, pick first available profile or default.
    """
    glabs_profiles_root = Path(os.path.expandvars(r"%APPDATA%\G-Labs Studio\flow_profiles"))
    redone_profiles_root = DATA_DIR / "flow_profiles"

    clean_email = (email or "").strip().lower()

    if clean_email:
        # 1. Exact match in G-Labs Studio
        p1 = glabs_profiles_root / clean_email
        if p1.is_dir():
            return p1
        # 2. Exact match in RedOne
        p2 = redone_profiles_root / clean_email
        if p2.is_dir():
            return p2
        # Create dedicated profile for this requested email
        target = redone_profiles_root / clean_email
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ONLY when email is NOT specified: check existing profiles
    if redone_profiles_root.is_dir():
        for sub in redone_profiles_root.iterdir():
            if sub.is_dir() and "@" in sub.name:
                return sub

    if glabs_profiles_root.is_dir():
        for sub in glabs_profiles_root.iterdir():
            if sub.is_dir() and "@" in sub.name:
                log.info(f"Using G-Labs profile for UI generation: {sub.name}")
                return sub

    # Fallback to RedOne data dir default
    target = redone_profiles_root / "default"
    target.mkdir(parents=True, exist_ok=True)
    return target


def parse_ogiz0b_candidates(text: str) -> list[dict]:
    """Parse media candidates and prompt string from ogiZ0b batchexecute response."""
    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.isdigit() or line.startswith(")]}'"):
            continue
        try:
            data = json.loads(line)
            if isinstance(data, list):
                for item in data:
                    if len(item) > 2 and item[1] == "ogiZ0b" and item[2]:
                        inner = json.loads(item[2]) if isinstance(item[2], str) else item[2]
                        results = []
                        if isinstance(inner, list) and len(inner) > 0 and isinstance(inner[0], list):
                            for cand in inner[0]:
                                if isinstance(cand, list) and len(cand) > 0 and isinstance(cand[0], str):
                                    mid = cand[0]
                                    prompt_found = ""
                                    for sub in cand:
                                        if isinstance(sub, list):
                                            for sub2 in sub:
                                                if isinstance(sub2, list):
                                                    for s3 in sub2:
                                                        if isinstance(s3, str) and len(s3) > 1 and "-" not in s3:
                                                            prompt_found = s3
                                                            break
                                    results.append({"media_id": mid, "prompt": prompt_found})
                        if results:
                            return results
        except Exception:
            pass

    # Regex fallback: extract UUIDs directly from text (the first UUID is the media_id)
    import re
    uuids = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text)
    if uuids:
        return [{"media_id": uuids[0], "prompt": ""}]
    return []


def parse_video_rpc_candidates(text: str) -> list[dict]:
    """Parse video generation candidate UUIDs from Google Flow video RPC responses:
    - YhhmEf (T2V generate)
    - MZZa6b (I2V/R2V generate with reference image)
    - jwpduf (Video status/candidate tracking)
    - eb1hJf (BOQ legacy)
    - as29s (Signed URL / metadata)
    """
    results = []
    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.isdigit() or line.startswith(")]}'"):
            continue
        try:
            data = json.loads(line)
            if isinstance(data, list):
                for item in data:
                    if len(item) > 2 and item[1] in ("YhhmEf", "eb1hJf", "MZZa6b", "jwpduf", "as29s") and item[2]:
                        inner = json.loads(item[2]) if isinstance(item[2], str) else item[2]
                        # 1. Exact Google Flow candidate list in inner[3]:
                        # inner[3] = [[media_id, project_id, group_id, ...], ...]
                        if isinstance(inner, list) and len(inner) > 3 and isinstance(inner[3], list):
                            for cand in inner[3]:
                                if isinstance(cand, list) and len(cand) > 0 and isinstance(cand[0], str) and len(cand[0]) == 36 and "-" in cand[0]:
                                    gid = cand[0]
                                    results.append({"generation_id": gid, "media_id": gid})
                            if results:
                                return results

                        # 2. In MZZa6b / YhhmEf: inner[2][0][3][4] contains the media UUID
                        if isinstance(inner, list) and len(inner) > 2 and isinstance(inner[2], list):
                            for group in inner[2]:
                                if isinstance(group, list):
                                    if len(group) > 3 and isinstance(group[3], list) and len(group[3]) > 4 and isinstance(group[3][4], str):
                                        gid = group[3][4]
                                        if len(gid) == 36 and "-" in gid:
                                            results.append({"generation_id": gid, "media_id": gid})
                            if results:
                                return results

                        # 3. In jwpduf: inner[2][0][0] is the media UUID
                        if isinstance(inner, list) and len(inner) > 2 and isinstance(inner[2], list):
                            for group in inner[2]:
                                if isinstance(group, list) and len(group) > 0 and isinstance(group[0], str) and len(group[0]) == 36 and "-" in group[0]:
                                    gid = group[0]
                                    results.append({"generation_id": gid, "media_id": gid})
                            if results:
                                return results

                        # 4. In as29s: inner[0] is the media UUID
                        if isinstance(inner, list) and len(inner) > 0 and isinstance(inner[0], str) and len(inner[0]) == 36 and "-" in inner[0]:
                            gid = inner[0]
                            return [{"generation_id": gid, "media_id": gid}]
        except Exception:
            pass

    # Regex Fallback 1: Extract Google Flow [media_id, project_id, group_id, "CAE"] pattern
    cae_matches = re.findall(
        r'\[\\?"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\?",\\?"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\?",\\?"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\\?",\\?"CAE',
        text,
    )
    if cae_matches:
        for m in cae_matches:
            mid = m[0]
            if mid and len(mid) == 36:
                results.append({"generation_id": mid, "media_id": mid})
        if results:
            return results

    # Regex Fallback 2: Direct UUID search
    uuids = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text)
    if uuids:
        return [{"generation_id": uuids[0], "media_id": uuids[0]}]

    return []


FLOW_VIDEO_MODELS = {
    "omni_flash": "Omni 1.1 Flash",
    "omni": "Omni 1.1 Flash",
    "lite": "Veo 3.1 - Lite",
    "lite_lp": "Veo 3.1 - Lite",
    "fast": "Veo 3.1 - Fast",
    "quality": "Veo 3.1 - Quality",
}


def resolve_flow_video_model(key: str) -> str:
    """Resolve internal video model key or preset to exact Google Flow UI model name."""
    k = (key or "").lower()
    if "lite" in k:
        return "Veo 3.1 - Lite"
    if "quality" in k:
        return "Veo 3.1 - Quality"
    if "omni" in k:
        return "Omni 1.1 Flash"
    return "Veo 3.1 - Fast"


def resolve_flow_image_model(key: str) -> str:
    """Resolve internal image model key to exact Google Flow UI image model name."""
    k = (key or "").lower()
    if "pro" in k or "gem_pix_2" in k:
        return "Nano Banana Pro"
    if "lite" in k or "harbor_seal" in k:
        return "Nano Banana 2 Lite"
    return "Nano Banana 2"



class FlowUIWorker:
    """High-performance worker managing persistent Chrome session with DOM Pipelining.

    Features:
    - DOM Pipelining: Prompts are typed and submitted sequentially into the editor
      (DOM input lock held for only ~1.5 - 2.5s per prompt), allowing multiple prompts
      to be submitted back-to-back without waiting for previous images to finish.
    - Parallel Cloud Rendering: Listens for asynchronous ogiZ0b responses and resolves
      all pending generations concurrently as Google Cloud completes them.
    - Zero Race Conditions: Responses are mapped deterministically via media_id and
      embedded prompt tokens.
    - Full-resolution binary extraction: Fetches blobs inside authenticated context.
    - Settings automation: Automatically applies aspect ratio (16:9, 1:1, 9:16, 4:3, 3:4)
      and output counts (x1, x2, x3, x4) via the web UI toolbar.
    """

    IDLE_TIMEOUT_SECONDS = 300  # 5 minutes idle shutdown

    def __init__(self):
        self._pw = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._active_email: str = ""
        self._active_profile_path: Optional[Path] = None
        self._active_project_id: str = ""
        self._input_lock = asyncio.Lock()
        self._pending_prompts: dict[str, asyncio.Future] = {}
        self._pending_queue: list[asyncio.Future] = []
        self._pending_video_queue: list[asyncio.Future] = []
        self._last_activity: float = time.time()
        self._idle_task: Optional[asyncio.Task] = None

    def _reset_idle_timer(self) -> None:
        self._last_activity = time.time()
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_watcher())

    async def _idle_watcher(self) -> None:
        try:
            await asyncio.sleep(self.IDLE_TIMEOUT_SECONDS)
            if time.time() - self._last_activity >= self.IDLE_TIMEOUT_SECONDS:
                if not self._pending_prompts and not self._pending_queue and not self._pending_video_queue:
                    log.info("[FlowUIWorker] Idle timeout reached. Closing Chrome context...")
                    await self.close()
        except asyncio.CancelledError:
            pass

    def _on_response(self, response) -> None:
        """Handle background network responses from Google Flow batchexecute."""
        if "batchexecute" in response.url and "ogiZ0b" in response.url:
            async def _process():
                try:
                    text = await response.text()
                    candidates = parse_ogiz0b_candidates(text)
                    if not candidates:
                        log.warning(f"[FlowUIWorker] ogiZ0b empty candidates. len={len(text)}")
                        return

                    mid = candidates[0]["media_id"]
                    target_prompt = candidates[0].get("prompt", "").strip()
                    log.info(f"[FlowUIWorker] Captured ogiZ0b: mid={mid}, prompt={repr(target_prompt[:35])}")
                    matched_fut = None

                    # 1. Match by prompt content
                    for p_key, fut in list(self._pending_prompts.items()):
                        if not fut.done() and (
                            p_key in target_prompt
                            or target_prompt in p_key
                            or p_key[:30] == target_prompt[:30]
                        ):
                            matched_fut = fut
                            del self._pending_prompts[p_key]
                            break

                    # 2. FIFO queue fallback
                    if not matched_fut and self._pending_queue:
                        while self._pending_queue:
                            fut = self._pending_queue.pop(0)
                            if not fut.done():
                                matched_fut = fut
                                break

                    if matched_fut and not matched_fut.done():
                        matched_fut.set_result(candidates)
                except Exception as e:
                    log.warning(f"[FlowUIWorker] Error handling ogiZ0b: {e}")

            asyncio.create_task(_process())

        elif "batchexecute" in response.url and any(
            k in response.url for k in ("YhhmEf", "eb1hJf", "MZZa6b", "jwpduf")
        ):
            async def _process_video():
                try:
                    text = await response.text()
                    candidates = parse_video_rpc_candidates(text)
                    if not candidates:
                        log.warning(f"[FlowUIWorker] Video RPC empty candidates. len={len(text)}")
                        return

                    gid = candidates[0]["generation_id"]
                    log.info(f"[FlowUIWorker] Captured video RPC: gen_id={gid}")
                    matched_fut = None

                    if self._pending_video_queue:
                        while self._pending_video_queue:
                            fut = self._pending_video_queue.pop(0)
                            if not fut.done():
                                matched_fut = fut
                                break

                    if matched_fut and not matched_fut.done():
                        matched_fut.set_result(candidates)
                except Exception as e:
                    log.warning(f"[FlowUIWorker] Error handling video RPC: {e}")

            asyncio.create_task(_process_video())

    async def _is_settings_open(self, page: Page) -> bool:
        """Check if the settings overlay is currently open and visible."""
        return await page.evaluate("""() => {
            const panes = Array.from(document.querySelectorAll('.cdk-overlay-pane'));
            return panes.some(el => {
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
                const r = el.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) return false;
                const text = el.innerText || '';
                return text.includes('Hình ảnh') || text.includes('Video') ||
                       text.includes('Nano Banana') || text.includes('Veo') ||
                       el.querySelector('mat-button-toggle') !== null;
            });
        }""")

    async def _open_settings(self, page: Page) -> Locator:
        """Reliably open settings overlay and return the overlay locator."""
        # 1. If already open, return it immediately
        if await self._is_settings_open(page):
            panes = page.locator(".cdk-overlay-pane:has(mat-button-toggle), .cdk-overlay-pane:has-text('Hình ảnh'), .cdk-overlay-pane:has-text('Video')")
            if await panes.count() > 0:
                return panes.first
            return page.locator(".cdk-overlay-pane").first

        btn = page.locator(".settings-trigger-button").first
        if await btn.count() == 0:
            raise RuntimeError("Settings trigger button (.settings-trigger-button) not found")

        for attempt in range(3):
            # Click to open (Playwright click or JS click fallback)
            try:
                await btn.click(timeout=2000)
            except Exception:
                await page.evaluate("""() => {
                    const b = document.querySelector('.settings-trigger-button');
                    if (b) b.click();
                }""")

            # Poll for visible settings pane
            for _ in range(12):
                await page.wait_for_timeout(250)
                if await self._is_settings_open(page):
                    panes = page.locator(".cdk-overlay-pane:has(mat-button-toggle), .cdk-overlay-pane:has-text('Hình ảnh'), .cdk-overlay-pane:has-text('Video')")
                    if await panes.count() > 0:
                        return panes.first
                    return page.locator(".cdk-overlay-pane").first

            # If not opened, try Enter key on attempt 2
            try:
                await btn.focus()
                await page.keyboard.press("Enter")
            except Exception:
                pass
            await page.wait_for_timeout(300)

        # Final check if any overlay is visible
        if await self._is_settings_open(page):
            return page.locator(".cdk-overlay-pane").first

        raise RuntimeError("Failed to open Google Flow settings overlay")

    async def _close_settings(self, page: Page) -> None:
        """Safely close settings overlay if open."""
        for _ in range(3):
            if not await self._is_settings_open(page):
                break
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            if not await self._is_settings_open(page):
                break
            await page.evaluate("""() => {
                const backdrop = document.querySelector('.cdk-overlay-backdrop');
                if (backdrop) backdrop.click();
            }""")
            await page.wait_for_timeout(300)

    @staticmethod
    async def _safe_click(locator: Locator) -> None:
        """Click an element safely, falling back to JavaScript click if outside viewport or intercepted."""
        try:
            await locator.scroll_into_view_if_needed(timeout=1000)
            await locator.click(timeout=1500)
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
            except Exception:
                pass

    async def _apply_settings(
        self,
        page: Page,
        aspect_ratio: str = "16:9",
        candidate_count: int = 1,
        model_key: str = "nano_banana_pro",
    ) -> None:
        """Configure mode (Image), model, aspect ratio and candidate count (x1-x4) in Flow settings menu."""
        btn = page.locator(".settings-trigger-button").first
        if await btn.count() == 0:
            return

        btn_text = await btn.inner_text()
        is_video = "Video" in btn_text
        ar_target = aspect_ratio or "16:9"
        count_target = f"x{max(1, min(4, candidate_count or 1))}"
        target_model_name = resolve_flow_image_model(model_key)

        log.info(f"[FlowUIWorker] Applying UI image settings: is_video={is_video}, model={target_model_name}, aspect={ar_target}, count={count_target}...")
        try:
            await self._open_settings(page)
        except Exception as e:
            if not is_video:
                log.warning(f"[FlowUIWorker] Settings overlay open failed ({e}), but already in Image mode. Proceeding with current settings.")
                return
            raise

        # 1. Switch back to Image mode if currently in Video mode
        if is_video:
            img_toggle = page.locator(".cdk-overlay-pane mat-button-toggle:has-text('Hình ảnh')")
            if await img_toggle.count() > 0:
                is_img_checked = await img_toggle.first.evaluate("el => el.classList.contains('mat-button-toggle-checked')")
                if not is_img_checked:
                    log.info("[FlowUIWorker] Switching mode: Video -> Hình ảnh...")
                    t_btn = img_toggle.locator("button")
                    if await t_btn.count() > 0:
                        await self._safe_click(t_btn.first)
                    else:
                        await self._safe_click(img_toggle.first)
                    await page.wait_for_timeout(400)

        # 2. Select Image Model if needed
        model_btn = page.locator(".cdk-overlay-pane button[aria-label*='mô hình'], .cdk-overlay-pane button:has-text('Nano Banana'), .cdk-overlay-pane button:has-text('arrow_drop_down')").first
        if await model_btn.count() > 0:
            curr_model_text = await model_btn.inner_text()
            if target_model_name.lower() not in curr_model_text.lower():
                log.info(f"[FlowUIWorker] Switching image model: current='{curr_model_text.strip()}' -> target='{target_model_name}'")
                await self._safe_click(model_btn)
                await page.wait_for_timeout(400)
                opt_btn = page.locator(f".cdk-overlay-pane button:has-text('{target_model_name}')")
                if await opt_btn.count() > 0:
                    await self._safe_click(opt_btn.first)
                    await page.wait_for_timeout(300)

        # 3. Aspect ratio
        opt_ar = page.locator(f".cdk-overlay-pane button:has-text('{ar_target}')")
        if await opt_ar.count() > 0:
            await self._safe_click(opt_ar.first)
            await page.wait_for_timeout(200)

        # 4. Output count
        opt_count = page.locator(f".cdk-overlay-pane button:has-text('{count_target}')")
        if await opt_count.count() > 0:
            await self._safe_click(opt_count.first)
            await page.wait_for_timeout(200)

        await self._close_settings(page)

        # Safety Guard: Ensure UI is not stuck in Video mode
        btn_text_after = await btn.inner_text()
        if "Video" in btn_text_after:
            raise RuntimeError(f"Google Flow UI vẫn ở chế độ Video ('{btn_text_after.strip()}'), không thể tiếp tục tạo ảnh.")

    async def _apply_video_settings(
        self,
        page: Page,
        aspect_ratio: str = "16:9",
        duration: int = 8,
        model_key: str = "fast",
        candidate_count: int = 1,
        video_mode: str = "start_image",
    ) -> None:
        """Switch to Video mode and configure frames mode ('Khung hình'), model, duration, aspect ratio, and count."""
        btn = page.locator(".settings-trigger-button").first
        if await btn.count() == 0:
            return

        btn_text = await btn.inner_text()
        is_video = "Video" in btn_text
        ar_target = "9:16" if ("9:16" in str(aspect_ratio) or "PORTRAIT" in str(aspect_ratio).upper()) else "16:9"
        dur_target = f"{duration} giây"
        count_target = f"x{max(1, min(4, candidate_count or 1))}"
        target_model_name = resolve_flow_video_model(model_key)

        log.info(f"[FlowUIWorker] Applying video UI settings: mode={video_mode}, model={target_model_name}, ar={ar_target}, dur={dur_target}, count={count_target}...")
        try:
            await self._open_settings(page)
        except Exception as e:
            if is_video:
                log.warning(f"[FlowUIWorker] Settings overlay open failed ({e}), but already in Video mode. Proceeding.")
                return
            raise

        # 1. Switch to Video mode if needed
        vid_btn = page.locator(".cdk-overlay-pane button:has-text('Video')")
        if await vid_btn.count() > 0:
            await vid_btn.first.click(force=True)
            await page.wait_for_timeout(300)

        # 2. Toggle to "Khung hình" (Frames mode) or "Thành phần" (Ingredients mode)
        if video_mode in ("components", "ingredients", "r2v"):
            comp_btn = page.locator(".cdk-overlay-pane button:has-text('Thành phần'), .cdk-overlay-pane button:has-text('Ingredients')")
            if await comp_btn.count() > 0:
                log.info("[FlowUIWorker] Toggling to 'Thành phần' (Ingredients) mode...")
                await comp_btn.first.click(force=True)
                await page.wait_for_timeout(300)
        else:
            frame_btn = page.locator(".cdk-overlay-pane button:has-text('Khung hình'), .cdk-overlay-pane button:has-text('Frames')")
            if await frame_btn.count() > 0:
                log.info("[FlowUIWorker] Toggling to 'Khung hình' (Frames) mode...")
                await frame_btn.first.click(force=True)
                await page.wait_for_timeout(300)

        # 3. Select Video Model if not already selected
        kw = "Lite" if "lite" in model_key.lower() else ("Quality" if "quality" in model_key.lower() else ("Omni" if "omni" in model_key.lower() else "Fast"))
        model_btn = page.locator(".cdk-overlay-pane button[aria-label='Chọn nhóm mô hình'], .cdk-overlay-pane button:has-text('arrow_drop_down')").first
        if await model_btn.count() > 0:
            curr_model_text = await model_btn.inner_text()
            if kw.lower() not in curr_model_text.lower():
                log.info(f"[FlowUIWorker] Switching video model: current='{curr_model_text.strip()}' -> target keyword='{kw}' ({target_model_name})")
                await model_btn.click(force=True)
                await page.wait_for_timeout(400)
                opt_btn = page.locator(f".cdk-overlay-pane button:has-text('{kw}')").first
                if await opt_btn.count() > 0:
                    await opt_btn.click(force=True)
                    await page.wait_for_timeout(300)

        # 4. Aspect Ratio (16:9 / 9:16)
        opt_ar = page.locator(f".cdk-overlay-pane button:has-text('{ar_target}')")
        if await opt_ar.count() > 0:
            await opt_ar.first.click(force=True)
            await page.wait_for_timeout(200)

        # 5. Duration (4 giây / 6 giây / 8 giây / 10 giây)
        opt_dur = page.locator(f".cdk-overlay-pane button:has-text('{dur_target}')")
        if await opt_dur.count() > 0:
            await opt_dur.first.click(force=True)
            await page.wait_for_timeout(200)

        # 6. Candidate count
        opt_count = page.locator(f".cdk-overlay-pane button:has-text('{count_target}')")
        if await opt_count.count() > 0:
            await opt_count.first.click(force=True)
            await page.wait_for_timeout(200)

        await self._close_settings(page)

    async def _clear_attached_images(self, page: Page) -> None:
        """Clear any lingering reference image chips or frame slots in the prompt bar."""
        try:
            await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll(
                    '.prompt-top-row button, flow-ingredient-bar button, flow-base-prompt-box flow-chip button, flow-base-prompt-box .chip-container'
                ));
                for (const b of btns) {
                    const t = (b.innerText || '').toLowerCase();
                    const a = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (t.includes('close') || t.includes('cancel') || a.includes('đóng') || a.includes('xóa') || a.includes('hủy') || a.includes('remove')) {
                        b.click();
                    }
                }
            }""")
            await page.wait_for_timeout(200)
            chips = page.locator("flow-base-prompt-box button.chip-container, flow-base-prompt-box button:has-text('cancel')")
            cnt = await chips.count()
            for _ in range(cnt):
                if await chips.first.is_visible():
                    await chips.first.click()
                    await page.wait_for_timeout(200)
        except Exception as e:
            log.debug(f"[FlowUIWorker] Clear attached images: {e}")

    async def _attach_video_frame(self, page: Page, image_path: str, is_end_frame: bool = False) -> bool:
        """Attach an image to either the 'Bắt đầu' (Start) or 'Kết thúc' (End) frame slot in Khung hình mode."""
        img_path = Path(image_path).resolve()
        if not img_path.exists():
            log.warning(f"[FlowUIWorker] Frame image not found: {img_path}")
            return False

        slot_label = "Kết thúc" if is_end_frame else "Bắt đầu"
        slot_btn = page.locator(
            f".frame-trigger button:has-text('{slot_label}'), "
            f"button.empty-chip:has-text('{slot_label}'), "
            f"button:has-text('{slot_label}')"
        ).first

        if await slot_btn.count() == 0 or not await slot_btn.is_visible():
            log.info(f"[FlowUIWorker] Frame slot button '{slot_label}' not immediately visible, ensuring 'Khung hình' mode...")
            await self._open_settings(page)
            f_btn = page.locator(".cdk-overlay-pane button:has-text('Khung hình'), .cdk-overlay-pane button:has-text('Frames')").first
            if await f_btn.count() > 0:
                await f_btn.click(force=True)
                await page.wait_for_timeout(300)
            await self._close_settings(page)
            await page.wait_for_timeout(400)

        try:
            await slot_btn.wait_for(state="visible", timeout=5000)
        except Exception:
            log.warning(f"[FlowUIWorker] Frame slot button '{slot_label}' not found or not visible after ensuring Khung hình mode")
            return False

        await slot_btn.click(force=True)
        await page.wait_for_timeout(600)

        overlay = page.locator(".cdk-overlay-pane")
        upload_btn = overlay.locator(".sidebar-upload-btn, button:has-text('Tải nội dung'), button:has-text('Upload')").first
        if await upload_btn.count() == 0:
            log.warning(f"[FlowUIWorker] Upload button not found in '{slot_label}' frame overlay")
            await page.keyboard.press("Escape")
            return False

        async with page.expect_file_chooser(timeout=8000) as fc_info:
            await upload_btn.click()
        fc = await fc_info.value
        await fc.set_files([str(img_path)])

        # Wait for upload to complete (no longer says "Đang tải lên" / "Uploading")
        upload_success = False
        for _ in range(50):
            await page.wait_for_timeout(500)
            check = await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('.cdk-overlay-pane button.asset-item'));
                if (!btns.length) return { uploading: true };
                const first = btns[0];
                const t = first.innerText;
                const uploading = t.includes('Đang tải lên') || t.includes('Uploading');
                return { uploading, text: t };
            }""")
            if check and not check.get("uploading"):
                upload_success = True
                break

        if not upload_success:
            log.warning(f"[FlowUIWorker] Upload timed out for frame {img_path.name}")
            await page.keyboard.press("Escape")
            return False

        # Select newly uploaded asset
        await page.evaluate("""() => {
            const first = document.querySelectorAll('.cdk-overlay-pane button.asset-item')[0];
            if (first) first.click();
        }""")
        await page.wait_for_timeout(400)

        # Click 'Thêm vào câu lệnh'
        add_to_prompt = overlay.locator(".detail-add-to-prompt-btn, button:has-text('Thêm vào câu lệnh'), button:has-text('Add to prompt')").first
        if await add_to_prompt.count() > 0:
            await add_to_prompt.click()
            await page.wait_for_timeout(600)

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        log.info(f"[FlowUIWorker] Attached frame image '{img_path.name}' to '{slot_label}' slot successfully.")
        return True


    async def _attach_reference_images(self, page: Page, image_paths: list[str]) -> bool:
        """Attach one or more local reference images into Google Flow prompt bar."""
        if not image_paths:
            return False

        valid_paths = [Path(p).resolve() for p in image_paths if p and Path(p).exists()]
        if not valid_paths:
            return False

        log.info(f"[FlowUIWorker] Attaching {len(valid_paths)} reference image(s): {[p.name for p in valid_paths]}")

        for img_path in valid_paths:
            add_btn = page.locator(".add-menu-trigger")
            if await add_btn.count() == 0:
                log.warning("[FlowUIWorker] .add-menu-trigger not found")
                return False

            # Check if overlay is already open, if not click trigger
            overlay = page.locator(".cdk-overlay-pane")
            if await overlay.count() == 0 or not await overlay.first.is_visible():
                await add_btn.first.click()
                await page.wait_for_timeout(600)

            upload_btn = overlay.locator(".sidebar-upload-btn, button:has-text('Tải nội dung'), button:has-text('Upload')").first
            if await upload_btn.count() == 0:
                log.warning("[FlowUIWorker] Upload button not found in overlay")
                await page.keyboard.press("Escape")
                return False

            async with page.expect_file_chooser(timeout=8000) as fc_info:
                await upload_btn.click()
            fc = await fc_info.value
            await fc.set_files([str(img_path)])

            # Wait for upload to complete (no longer says "Đang tải lên" / "Uploading")
            upload_success = False
            for _ in range(50):  # up to 25s
                await page.wait_for_timeout(500)
                check = await page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('.cdk-overlay-pane button.asset-item'));
                    if (!btns.length) return { uploading: true };
                    const first = btns[0];
                    const t = first.innerText;
                    const uploading = t.includes('Đang tải lên') || t.includes('Uploading');
                    return { uploading, text: t };
                }""")
                if check and not check.get("uploading"):
                    upload_success = True
                    break

            if not upload_success:
                log.warning(f"[FlowUIWorker] Upload timed out for {img_path.name}")
                await page.keyboard.press("Escape")
                return False

            # Ensure the newly uploaded item is clicked / selected as active
            await page.evaluate("""() => {
                const first = document.querySelectorAll('.cdk-overlay-pane button.asset-item')[0];
                if (first) first.click();
            }""")
            await page.wait_for_timeout(500)

            add_to_prompt = overlay.locator(".detail-add-to-prompt-btn, button:has-text('Thêm vào câu lệnh'), button:has-text('Add to prompt')").first
            try:
                await add_to_prompt.wait_for(state="visible", timeout=8000)
                await page.wait_for_timeout(400)
                await add_to_prompt.click()
                await page.wait_for_timeout(600)
            except Exception as e:
                log.warning(f"[FlowUIWorker] Click detail-add-to-prompt-btn failed: {e}")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return False

        # Close overlay once done attaching
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        # Verify chips in prompt box
        try:
            chip_count = await page.evaluate("""() => {
                return document.querySelectorAll('flow-base-prompt-box .chip-container, flow-base-prompt-box flow-chip').length;
            }""")
            log.info(f"[FlowUIWorker] Attached images completed. Current chips in prompt box: {chip_count}")
        except Exception:
            pass

        return True

    async def _ensure_editor_ready(self, page: Page, requested_pid: str) -> str:
        """Navigate to requested_pid and ensure .ProseMirror is visible.
        If requested_pid results in 404 or fails to show .ProseMirror within 5 seconds,
        auto-recovers by discovering a valid project from Flow home or creating a new one.
        Returns the confirmed active project ID.
        """
        target_url = f"https://flow.google.com/project/{requested_pid}" if requested_pid else "https://flow.google.com/"
        current_url = page.url or ""

        # If already on the project URL and editor is visible, nothing to do
        if requested_pid and current_url.startswith(target_url) and "/404" not in current_url:
            try:
                pm = await page.query_selector(".ProseMirror")
                if pm and await pm.is_visible():
                    self._active_project_id = requested_pid
                    return requested_pid
            except Exception:
                pass

        log.info(f"[FlowUIWorker] Navigating to {target_url}...")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
        except Exception as e:
            log.warning(f"[FlowUIWorker] Navigation to {target_url} raised: {e}")

        # Check if redirected to Google sign-in (session missing or expired)
        if "accounts.google.com" in (page.url or ""):
            log.warning("[FlowUIWorker] Redirected to accounts.google.com. Auto-syncing cookies from Chrome extension...")
            try:
                from .browser_bridge import bridge
                if bridge.is_extension_live():
                    res = await bridge.get_cookies(domains=["flow.google.com", ".flow.google.com", ".google.com", "google.com"])
                    if res and isinstance(res, dict) and "cookies" in res:
                        pw_cookies = format_cookies_for_playwright(res["cookies"])
                        if pw_cookies:
                            await page.context.add_cookies(pw_cookies)
                            log.info(f"[FlowUIWorker] Re-synced {len(pw_cookies)} cookies. Retrying navigation to {target_url}...")
                            await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
            except Exception as e:
                log.warning(f"[FlowUIWorker] Re-syncing cookies failed: {e}")

        # Check if ProseMirror appears within 5 seconds; if not or if redirected to 404, auto-recover
        has_pm = False
        for _ in range(5):
            await page.wait_for_timeout(1000)
            if "/404" in (page.url or "") or "accounts.google.com" in (page.url or ""):
                break
            try:
                pm = await page.query_selector(".ProseMirror")
                if pm and await pm.is_visible():
                    has_pm = True
                    break
            except Exception:
                pass

        if not requested_pid or not has_pm or "/404" in (page.url or ""):
            log.warning(
                f"[FlowUIWorker] Project '{requested_pid}' invalid or inaccessible (url={page.url}). "
                f"Auto-discovering valid project from Flow home..."
            )
            try:
                await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # 1. Search for existing project links on Flow home
            first_project_link = None
            try:
                first_project_link = await page.evaluate("""() => {
                    const links = Array.from(document.querySelectorAll('a[href*="/project/"]'));
                    for (const a of links) {
                        if (a.href && !a.href.includes('/404')) return a.href;
                    }
                    return null;
                }""")
            except Exception:
                pass

            if first_project_link:
                log.info(f"[FlowUIWorker] Auto-navigating to existing valid project: {first_project_link}")
                await page.goto(first_project_link, wait_until="domcontentloaded", timeout=35000)
                m = first_project_link.split("/project/")
                if len(m) > 1:
                    self._active_project_id = m[1].split("?")[0].split("/")[0]
            else:
                # 2. If no project links found, try clicking 'New project'
                log.info("[FlowUIWorker] No existing project links found on Flow home. Creating new project...")
                new_btn = page.locator("button:has-text('New project'), button:has-text('Dự án mới'), [aria-label*='New project'], [aria-label*='Dự án mới'], span:has-text('New project')").first
                try:
                    await new_btn.wait_for(state="visible", timeout=6000)
                    await new_btn.click()
                    await page.wait_for_timeout(4000)
                    m = (page.url or "").split("/project/")
                    if len(m) > 1:
                        self._active_project_id = m[1].split("?")[0].split("/")[0]
                except Exception as btn_err:
                    log.warning(f"[FlowUIWorker] Clicking 'New project' failed: {btn_err}")

        # Wait for ProseMirror to be visible
        await page.wait_for_selector(".ProseMirror", state="visible", timeout=25000)
        if "/project/" in (page.url or "") and "/404" not in page.url:
            parts = page.url.split("/project/")
            if len(parts) > 1:
                self._active_project_id = parts[1].split("?")[0].split("/")[0]

        return self._active_project_id

    async def ensure_session(self, email: str = "", project_id: str = "") -> Page:
        """Ensure Chrome is running, authenticated, and navigated to the project page."""
        self._reset_idle_timer()

        pid = (project_id or self._active_project_id or "").strip()
        profile_path = resolve_profile_dir(email)
        chrome_exe = find_chrome_executable()

        if not chrome_exe:
            raise RuntimeError("Google Chrome not found on system. Please install Google Chrome.")

        # If context is alive
        if self._context and self._page and not self._page.is_closed():
            if self._active_profile_path and self._active_profile_path != profile_path:
                log.info(f"[FlowUIWorker] Profile changed ({self._active_profile_path} -> {profile_path}). Reopening...")
                await self.close()
            else:
                await self._ensure_editor_ready(self._page, pid)
                return self._page

        # Launch fresh persistent context
        log.info(f"[FlowUIWorker] Launching persistent Chrome context with profile: {profile_path}")
        if self._pw is None:
            self._pw = await async_playwright().start()

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            executable_path=chrome_exe,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1600, "height": 1000},
        )
        self._context.on("response", self._on_response)

        # Auto-sync live cookies from Chrome Extension if available
        try:
            from .browser_bridge import bridge
            if bridge.is_extension_live():
                ext_email = (bridge.get_active_account_email() or "").strip().lower()
                clean_email = (email or "").strip().lower()
                if not clean_email or clean_email == ext_email or not ext_email:
                    res = await bridge.get_cookies(domains=["flow.google.com", ".flow.google.com", ".google.com", "google.com"])
                    if res and isinstance(res, dict) and "cookies" in res:
                        pw_cookies = format_cookies_for_playwright(res["cookies"])
                        if pw_cookies:
                            await self._context.add_cookies(pw_cookies)
                            log.info(f"[FlowUIWorker] Synced {len(pw_cookies)} cookies from Chrome extension into profile {profile_path.name}")
        except Exception as e:
            log.debug(f"[FlowUIWorker] Auto cookie sync from extension skipped: {e}")

        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._active_email = email
        self._active_profile_path = profile_path
        self._active_project_id = pid

        await self._ensure_editor_ready(self._page, pid)
        return self._page

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        model_key: str = "nano_banana_pro",
        candidate_count: int = 1,
        reference_images: list[str] | None = None,
        project_id: str = "",
        email: str = "",
        timeout_s: int = 60,
    ) -> dict:
        """Submit image prompt via DOM Pipelining and wait for cloud rendering concurrently."""
        self._reset_idle_timer()
        start_time = time.time()
        fut = asyncio.get_running_loop().create_future()
        norm_prompt = prompt.strip()

        # ── 1. DOM Input Phase (Protected by lock) ──
        async with self._input_lock:
            page = await self.ensure_session(email=email, project_id=project_id)

            # Wait for ProseMirror editor
            try:
                await page.wait_for_selector(".ProseMirror", state="visible", timeout=20000)
            except Exception:
                raise RuntimeError("Google Flow UI editor (.ProseMirror) not found.")

            # Clear any old attached image chips from prior runs
            await self._clear_attached_images(page)

            # Apply aspect ratio & candidate count if needed (and switch to Image mode if currently Video)
            await self._apply_settings(
                page,
                aspect_ratio=aspect_ratio,
                candidate_count=candidate_count,
                model_key=model_key,
            )

            # If reference images are provided for I2I, attach them
            if reference_images:
                ok = await self._attach_reference_images(page, reference_images)
                if not ok:
                    raise RuntimeError(f"Đính kèm ảnh tham chiếu (I2I) thất bại: {reference_images}")

            # Register pending future before clicking Generate
            self._pending_prompts[norm_prompt] = fut
            self._pending_queue.append(fut)

            # Focus and clear .ProseMirror
            await page.click(".ProseMirror")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")

            # Fast natural typing
            log.info(f"[FlowUIWorker] Typing prompt ({len(prompt)} chars)...")
            await page.keyboard.type(prompt, delay=5)
            await page.wait_for_timeout(300)

            # Ensure .generate-icon-button is enabled (not disabled)
            # Ensure .generate-icon-button is enabled (not disabled)
            log.info("[FlowUIWorker] Submitting image prompt...")
            button_ready = False
            for _ in range(25):  # wait up to 5s for button readiness
                is_disabled = await page.evaluate("""() => {
                    const b = document.querySelector('.generate-icon-button');
                    return !b || b.disabled || b.classList.contains('mat-mdc-button-disabled');
                }""")
                if not is_disabled:
                    button_ready = True
                    break
                await page.wait_for_timeout(200)

            if not button_ready:
                err_diag = await page.evaluate("""() => {
                    const b = document.querySelector('.generate-icon-button');
                    const pm = document.querySelector('.ProseMirror');
                    const modal = document.querySelector('mat-dialog-container, .cdk-overlay-pane');
                    return {
                        hasButton: !!b,
                        buttonDisabled: b ? (b.disabled || b.classList.contains('mat-mdc-button-disabled')) : null,
                        pmText: pm ? pm.innerText.slice(0, 100) : '',
                        hasModal: !!modal,
                        modalText: modal ? modal.innerText.slice(0, 100) : ''
                    };
                }""")
                self._pending_prompts.pop(norm_prompt, None)
                if fut in self._pending_queue:
                    self._pending_queue.remove(fut)
                raise RuntimeError(f"Nút Generate bị vô hiệu hóa (disabled). Chi tiết trang: {err_diag}")

            # Trigger generate via a single native DOM click to eliminate duplicate triggers
            clicked = await page.evaluate("""() => {
                const b = document.querySelector('.generate-icon-button');
                if (b && !b.disabled && !b.classList.contains('mat-mdc-button-disabled')) {
                    b.click();
                    return true;
                }
                return false;
            }""")
            if not clicked:
                log.info("[FlowUIWorker] DOM evaluate click did not fire, clicking via Playwright locator...")
                await page.click(".generate-icon-button", timeout=3000)

            await page.wait_for_timeout(500)

        # ── 2. Parallel Waiting Phase (Runs concurrently outside lock!) ──
        log.info(f"[FlowUIWorker] Prompt pipelined successfully! Waiting in parallel for render completion...")

        try:
            candidates = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending_prompts.pop(norm_prompt, None)
            if fut in self._pending_queue:
                self._pending_queue.remove(fut)
            raise TimeoutError(f"Generation timed out after {timeout_s}s waiting for ogiZ0b response")

        if not candidates:
            raise RuntimeError("No media candidates returned from Google Flow.")

        primary_mid = candidates[0]["media_id"]
        log.info(f"[FlowUIWorker] Render complete for media_id: {primary_mid}. Waiting for image tile on DOM...")

        # ── 3. Poll DOM for image tile with primary_mid ──
        new_image = None
        for _ in range(35):
            await asyncio.sleep(0.8)
            img_data = await page.evaluate("""(mid) => {
                const img = document.querySelector(`flow-image-tile img.image[data-media-id="${mid}"]`);
                if (img && (img.naturalWidth > 100 || img.complete)) {
                    return {
                        mediaId: mid,
                        src: img.src,
                        width: img.naturalWidth,
                        height: img.naturalHeight
                    };
                }
                return null;
            }""", primary_mid)
            if img_data and img_data.get("src"):
                new_image = img_data
                break

        if not new_image:
            new_image = await page.evaluate("""(mid) => {
                const img = document.querySelector(`flow-image-tile img.image[data-media-id="${mid}"]`);
                return img ? { mediaId: mid, src: img.src, width: img.naturalWidth || 1376, height: img.naturalHeight || 768 } : null;
            }""", primary_mid)

        if not new_image or not new_image.get("src"):
            raise RuntimeError(f"Image tile for media_id {primary_mid} not found on grid.")

        # ── 4. Fetch full-resolution blob inside browser session ──
        log.info(f"[FlowUIWorker] Fetching full-res image blob for {primary_mid} in browser...")
        b64_res = await page.evaluate("""async (imgSrc) => {
            const res = await fetch(imgSrc);
            const blob = await res.blob();
            return new Promise((resolve) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve({
                    dataUrl: reader.result,
                    size: blob.size,
                    type: blob.type
                });
                reader.readAsDataURL(blob);
            });
        }""", new_image["src"])

        data_url = b64_res.get("dataUrl", "")
        raw_bytes = None
        if "," in data_url:
            raw_bytes = base64.b64decode(data_url.split(",")[1])

        elapsed = time.time() - start_time
        log.info(f"[FlowUIWorker] Complete in {elapsed:.1f}s! Media ID: {primary_mid} ({len(raw_bytes or b'')} bytes)")

        return {
            "media_id": primary_mid,
            "download_url": new_image["src"],
            "raw_bytes": raw_bytes,
            "width": new_image.get("width") or 1376,
            "height": new_image.get("height") or 768,
            "candidates": candidates,
            "seed": 0,
        }

    async def generate_video(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        duration: int = 8,
        model_key: str = "fast",
        reference_image: str | None = None,
        end_image: str | None = None,
        video_mode: str = "start_image",
        project_id: str = "",
        email: str = "",
        timeout_s: int = 90,
    ) -> dict:
        """Submit video prompt (T2V or I2V) via DOM Playwright and capture generation_id."""
        self._reset_idle_timer()
        start_time = time.time()
        fut = asyncio.get_running_loop().create_future()

        # ── 1. DOM Input Phase (Protected by lock) ──
        async with self._input_lock:
            page = await self.ensure_session(email=email, project_id=project_id)

            try:
                await page.wait_for_selector(".ProseMirror", state="visible", timeout=20000)
            except Exception:
                raise RuntimeError("Google Flow UI editor (.ProseMirror) not found.")

            # Clear any old attached image chips from prior runs
            await self._clear_attached_images(page)

            # Switch to Video mode & apply model/aspect/duration and mode
            await self._apply_video_settings(
                page,
                aspect_ratio=aspect_ratio,
                duration=duration,
                model_key=model_key,
                video_mode=video_mode,
            )

            # If ingredients/components mode:
            if video_mode in ("components", "ingredients", "r2v"):
                if reference_image and Path(reference_image).exists():
                    ok = await self._attach_reference_images(page, [reference_image])
                    if not ok:
                        raise RuntimeError(f"Gán ảnh vào 'Thành phần' (Ingredients) thất bại: {reference_image}")
            else:
                # Frames mode (Khung hình):
                # If reference image is provided for I2V, attach it to Start frame ("Bắt đầu") slot
                if reference_image and Path(reference_image).exists():
                    ok = await self._attach_video_frame(page, reference_image, is_end_frame=False)
                    if not ok:
                        raise RuntimeError(f"Gán ảnh tham chiếu vào khung hình 'Bắt đầu' thất bại: {reference_image}")

                # If end image is provided (e.g. for loop video or interpolation), attach to End frame ("Kết thúc") slot
                if end_image and Path(end_image).exists():
                    ok = await self._attach_video_frame(page, end_image, is_end_frame=True)
                    if not ok:
                        raise RuntimeError(f"Gán ảnh kết thúc vào khung hình 'Kết thúc' thất bại: {end_image}")

            # Register pending future before clicking Generate
            self._pending_video_queue.append(fut)

            # Focus and clear .ProseMirror
            await page.click(".ProseMirror")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")

            # Fast natural typing
            log.info(f"[FlowUIWorker] Typing video prompt ({len(prompt)} chars)...")
            await page.keyboard.type(prompt, delay=5)
            await page.wait_for_timeout(300)

            # Ensure .generate-icon-button is enabled (not disabled)
            log.info("[FlowUIWorker] Submitting video prompt...")
            button_ready = False
            for _ in range(25):  # wait up to 5s for button readiness
                is_disabled = await page.evaluate("""() => {
                    const b = document.querySelector('.generate-icon-button');
                    return !b || b.disabled || b.classList.contains('mat-mdc-button-disabled');
                }""")
                if not is_disabled:
                    button_ready = True
                    break
                await page.wait_for_timeout(200)

            if not button_ready:
                err_diag = await page.evaluate("""() => {
                    const b = document.querySelector('.generate-icon-button');
                    const pm = document.querySelector('.ProseMirror');
                    const modal = document.querySelector('mat-dialog-container, .cdk-overlay-pane');
                    return {
                        hasButton: !!b,
                        buttonDisabled: b ? (b.disabled || b.classList.contains('mat-mdc-button-disabled')) : null,
                        pmText: pm ? pm.innerText.slice(0, 100) : '',
                        hasModal: !!modal,
                        modalText: modal ? modal.innerText.slice(0, 100) : ''
                    };
                }""")
                if fut in self._pending_video_queue:
                    self._pending_video_queue.remove(fut)
                raise RuntimeError(f"Nút Generate video bị vô hiệu hóa (disabled). Chi tiết trang: {err_diag}")

            # Trigger generate via a single native DOM click to eliminate duplicate triggers
            clicked = await page.evaluate("""() => {
                const b = document.querySelector('.generate-icon-button');
                if (b && !b.disabled && !b.classList.contains('mat-mdc-button-disabled')) {
                    b.click();
                    return true;
                }
                return false;
            }""")
            if not clicked:
                log.info("[FlowUIWorker] DOM evaluate click did not fire, clicking via Playwright locator...")
                await page.click(".generate-icon-button", timeout=3000)

            await page.wait_for_timeout(500)

        # ── 2. Await generation_id from captured RPC ──
        log.info("[FlowUIWorker] Video request dispatched! Waiting for generation_id...")
        try:
            candidates = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            if fut in self._pending_video_queue:
                self._pending_video_queue.remove(fut)
            raise TimeoutError(f"Video generation timed out after {timeout_s}s waiting for RPC response")

        if not candidates:
            raise RuntimeError("No candidates returned from Google Flow video RPC.")

        primary_gid = candidates[0]["generation_id"]
        elapsed = time.time() - start_time
        log.info(f"[FlowUIWorker] Video generation started in {elapsed:.1f}s! generation_id: {primary_gid}")
        return {
            "generation_id": primary_gid,
            "media_id": primary_gid,
            "name": primary_gid,
            "candidates": candidates,
        }

    async def fetch_video_mp4(self, media_id: str, project_id: str = "", timeout_s: int = 120) -> bytes | None:
        """Fetch full MP4 video bytes via browser context using as29s RPC."""
        if not media_id:
            return None
        page = self._page
        if not page or page.is_closed():
            try:
                page = await self.ensure_session(project_id=project_id)
            except Exception as e:
                log.warning(f"[FlowUIWorker] Cannot ensure session for video download: {e}")
                return None

        log.info(f"[FlowUIWorker] Querying signed video URL and downloading MP4 for {media_id}...")
        for attempt in range(1, 6):
            try:
                dl_url = await page.evaluate("""async (mid) => {
                    const sn = (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || '';
                    const fReq = JSON.stringify([[["as29s", JSON.stringify([mid]), null, "generic"]]]);
                    const params = new URLSearchParams({
                        'rpcids': 'as29s',
                        'source-path': window.location.pathname,
                        'f.sid': (window.WIZ_global_data && window.WIZ_global_data.FdrFJe) || '',
                        'bl': (window.WIZ_global_data && window.WIZ_global_data.cfb2h) || '',
                        'hl': 'vi',
                        '_reqid': String(Math.floor(Math.random() * 900000) + 100000),
                        'rt': 'c'
                    });
                    const res = await fetch('/_/AiSandboxAngularFrontend/data/batchexecute?' + params.toString(), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                        body: 'f.req=' + encodeURIComponent(fReq) + (sn ? '&at=' + encodeURIComponent(sn) : '')
                    });
                    const text = await res.text();
                    for (const line of text.split('\\n')) {
                        if (!line || line.startsWith(\")]}'\")) continue;
                        try {
                            const data = JSON.parse(line);
                            if (Array.isArray(data)) {
                                for (const item of data) {
                                    if (item && item[1] === 'as29s' && item[2]) {
                                        const inner = typeof item[2] === 'string' ? JSON.parse(item[2]) : item[2];
                                        const strInner = JSON.stringify(inner);
                                        const urls = strInner.match(/https:\\/\\/flow-content\\.google\\/video\\/[^"\\\\]+/g);
                                        if (urls && urls.length > 0) {
                                            return urls[0];
                                        }
                                    }
                                }
                            }
                        } catch (e) {}
                    }
                    return null;
                }""", media_id)

                if dl_url:
                    log.info(f"[FlowUIWorker] Got signed VIDEO URL (attempt {attempt}): {dl_url[:75]}...")
                    # 1. First try Playwright APIRequestContext (fast, no memory bloat)
                    try:
                        resp = await page.context.request.get(dl_url, timeout=timeout_s * 1000)
                        if resp.ok:
                            raw = await resp.body()
                            if len(raw) > 100_000 and not raw.startswith(b"\xff\xd8\xff"):
                                log.info(f"[FlowUIWorker] Successfully downloaded MP4 via request: {len(raw)} bytes")
                                return raw
                    except Exception as req_err:
                        log.warning(f"[FlowUIWorker] context.request.get failed: {req_err}, falling back to page.evaluate fetch")

                    # 2. Fallback to in-page blob fetch
                    b64_res = await page.evaluate("""async (url) => {
                        const res = await fetch(url);
                        if (!res.ok) return null;
                        const blob = await res.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onloadend = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        });
                    }""", dl_url)

                    if b64_res and "," in b64_res:
                        raw = base64.b64decode(b64_res.split(",")[1])
                        if len(raw) > 100_000 and not raw.startswith(b"\xff\xd8\xff"):
                            log.info(f"[FlowUIWorker] Successfully downloaded MP4 via blob: {len(raw)} bytes")
                            return raw
                        elif raw.startswith(b"\xff\xd8\xff"):
                            log.warning("[FlowUIWorker] Got JPEG thumbnail instead of MP4 from signed URL")

                log.info(f"[FlowUIWorker] Video stream URL not ready yet for {media_id} (attempt {attempt}/5)...")
            except Exception as e:
                log.warning(f"[FlowUIWorker] Error in fetch_video_mp4 for {media_id} (attempt {attempt}): {e}")

            if attempt < 5:
                await asyncio.sleep(3)

        return None

    async def poll_video_status(self, media_id: str, timeout_s: int = 15) -> dict:
        """Poll video status in page context via jwpduf."""
        page = self._page
        if not page or page.is_closed():
            return {"state": "RUNNING"}
        try:
            status_text = await page.evaluate("""async (mid) => {
                const sn = (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || '';
                const fReq = JSON.stringify([[["jwpduf", JSON.stringify([null, null, [[mid]]]), null, "generic"]]]);
                const params = new URLSearchParams({
                    'rpcids': 'jwpduf',
                    'source-path': window.location.pathname,
                    'f.sid': (window.WIZ_global_data && window.WIZ_global_data.FdrFJe) || '',
                    'bl': (window.WIZ_global_data && window.WIZ_global_data.cfb2h) || '',
                    'hl': 'vi',
                    '_reqid': String(Math.floor(Math.random() * 900000) + 100000),
                    'rt': 'c'
                });
                const res = await fetch('/_/AiSandboxAngularFrontend/data/batchexecute?' + params.toString(), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                    body: 'f.req=' + encodeURIComponent(fReq) + (sn ? '&at=' + encodeURIComponent(sn) : '')
                });
                return await res.text();
            }""", media_id)
            if "[3]" in status_text or "COMPLETED" in status_text:
                return {
                    "state": "COMPLETED",
                    "media_id": media_id,
                    "video_id": media_id,
                    "name": media_id,
                }
            elif "[4," in status_text and "Media not found" in status_text:
                return {"state": "RUNNING"}
            elif "[6]" in status_text:
                return {"state": "RUNNING"}
        except Exception as e:
            log.warning(f"[FlowUIWorker] poll_video_status error: {e}")
        return {"state": "RUNNING"}

    async def close(self) -> None:
        """Close browser context and stop Playwright."""
        async with self._input_lock:
            for fut in list(self._pending_prompts.values()) + list(self._pending_queue) + list(self._pending_video_queue):
                if not fut.done():
                    fut.cancel()
            self._pending_prompts.clear()
            self._pending_queue.clear()
            self._pending_video_queue.clear()

            try:
                if self._context:
                    await self._context.close()
            except Exception:
                pass
            finally:
                self._context = None
                self._page = None
                self._active_profile_path = None
                self._active_email = ""
                self._active_project_id = ""

            try:
                if self._pw:
                    await self._pw.stop()
            except Exception:
                pass
            finally:
                self._pw = None


# Global singleton instance
ui_worker = FlowUIWorker()

