"""Google Flow reCAPTCHA Enterprise Token Provider.

Supplies high-score Google reCAPTCHA Enterprise tokens without any DOM interaction:
1. Primary Provider: RedOne Auth Helper Chrome extension bridge (`browser_bridge.harvest_recaptcha`).
   Executes `grecaptcha.enterprise.execute` directly in the user's authenticated Chrome session.
2. Fallback Provider: Standalone Headless Chrome Worker (equivalent to G-Labs `SingleChromeCaptchaWorker`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .config import FLOW_RECAPTCHA_SITE_KEY

log = logging.getLogger("redone.flow.captcha")


class FlowCaptchaProvider:
    """Manages acquisition of reCAPTCHA tokens for Google Flow operations."""

    def __init__(self, site_key: str = FLOW_RECAPTCHA_SITE_KEY):
        self.site_key = site_key
        self._lock = asyncio.Lock()

    async def get_token(self, action: str = "IMAGE_GENERATION", timeout_ms: int = 25000) -> str:
        """Acquire a fresh reCAPTCHA token for the specified action.

        Args:
            action: Action string (IMAGE_GENERATION, VIDEO_GENERATION, UPLOAD_IMAGE, AUDIO_GENERATION).
            timeout_ms: Timeout in milliseconds.

        Returns:
            Token string. Raises RuntimeError if acquisition fails.
        """
        async with self._lock:
            # 1. Primary path: RedOne Extension Bridge
            try:
                from ..browser_bridge import bridge
                log.info(f"Minting reCAPTCHA token via Extension Bridge (action={action})...")
                token = await bridge.harvest_recaptcha(site_key=self.site_key, action=action)
                if token:
                    log.info(f"Minted reCAPTCHA token successfully (len={len(token)})")
                    return token
            except Exception as e:
                log.warning(f"Extension Bridge reCAPTCHA harvest failed: {e}")

            # 2. Fallback path: Local Headless Captcha Worker if extension not available
            log.info(f"Attempting local headless captcha provider for action {action}...")
            token = await self._mint_headless_token(action=action, timeout_ms=timeout_ms)
            if token:
                return token

            raise RuntimeError(f"Unable to mint reCAPTCHA token for action {action}")

    async def _mint_headless_token(self, action: str, timeout_ms: int = 25000) -> Optional[str]:
        """Local headless minting fallback (using playwright if installed)."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
                )
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto("https://labs.google/fx", timeout=15000, wait_until="networkidle")

                    js = f"""
                    new Promise((resolve, reject) => {{
                        const timer = setTimeout(() => reject('timeout'), 15000);
                        function runExecute() {{
                            if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {{
                                grecaptcha.enterprise.ready(() => {{
                                    grecaptcha.enterprise.execute('{self.site_key}', {{ action: '{action}' }})
                                        .then((t) => {{ clearTimeout(timer); resolve(t); }})
                                        .catch((err) => {{ clearTimeout(timer); reject(err); }});
                                }});
                            }} else {{
                                clearTimeout(timer);
                                reject('grecaptcha not found');
                            }}
                        }}

                        const deadline = Date.now() + 8000;
                        const check = () => {{
                            if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise && grecaptcha.enterprise.execute) {{
                                runExecute();
                            }} else if (Date.now() < deadline) {{
                                setTimeout(check, 250);
                            }} else {{
                                clearTimeout(timer);
                                reject('grecaptcha load timeout');
                            }}
                        }};
                        check();
                    }})
                    """
                    token = await page.evaluate(js)
                    if token and isinstance(token, str):
                        return token
                finally:
                    await browser.close()
        except Exception as e:
            log.warning(f"Headless token minting failed: {e}")
        return None


# Global singleton instance
captcha_provider = FlowCaptchaProvider()
