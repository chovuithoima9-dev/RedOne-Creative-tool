"""Unified Flow Service — Protocol-based Google Flow Orchestrator (Zero-DOM).

Coordinates FlowSession, Payload Builders, and Captcha Provider to execute
all Google Flow operations without DOM clicking or browser automation.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, List, Optional

from .config import (
    FLOW_RPCS,
    IMAGE_ASPECT_RATIOS,
    IMAGE_MODELS,
    VIDEO_ASPECT_RATIOS,
    VIDEO_I2V_MODELS,
    VIDEO_R2V_MODELS,
    VIDEO_T2V_MODELS,
)
from .payloads import (
    build_credits_payload,
    build_get_media_payload,
    build_image_generate_payload,
    build_image_upload_payload,
    build_image_upscale_payload,
    build_project_create_payload,
    build_video_first_last_payload,
    build_video_ingredients_payload,
    build_video_poll_payload,
    build_video_start_frame_payload,
    build_video_t2v_payload,
    build_video_upscale_payload,
)
from .transport import FlowSession

log = logging.getLogger("redone.flow.service")


class FlowService:
    """High-level service coordinating Google Flow RPC operations."""

    def __init__(self):
        self._sessions: dict[str, FlowSession] = {}
        self._active_projects: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def get_session(self, account_email: str = "") -> FlowSession:
        """Get or create cached FlowSession for account."""
        email = account_email.lower().strip() or "default"
        if email not in self._sessions:
            self._sessions[email] = FlowSession(account_email=email)
        return self._sessions[email]

    async def ensure_project(self, account_email: str = "") -> str:
        """Ensure an active project ID exists for operations."""
        email = account_email.lower().strip() or "default"
        if email in self._active_projects and self._active_projects[email]:
            return self._active_projects[email]

        # Check extension active project ID if available
        try:
            from ..browser_bridge import bridge
            ext_pid = bridge.get_active_project_id()
            if ext_pid:
                self._active_projects[email] = ext_pid
                return ext_pid
        except Exception:
            pass

        # Create new project via RPC
        new_pid = str(uuid.uuid4())
        self._active_projects[email] = new_pid
        return new_pid

    async def _execute_rpc(
        self,
        rpc_id: str,
        inner_payload: Any,
        project_id: str,
        recaptcha_action: str = "",
        account_email: str = "",
        timeout_ms: int = 120000,
    ) -> dict:
        """Execute RPC with automatic fallback between Extension Bridge and FlowSession."""
        source_path = f"/project/{project_id}" if project_id else "/"

        # 1. Primary route: RedOne Extension Bridge with inline reCAPTCHA
        try:
            from ..browser_bridge import bridge
            if bridge.is_extension_live():
                log.info(f"[{account_email}] Executing RPC {rpc_id} via Extension Bridge (action={recaptcha_action})...")
                res = await bridge.batch_execute(
                    rpc_id=rpc_id,
                    inner_payload=inner_payload,
                    source_path=source_path,
                    timeout_ms=timeout_ms,
                    recaptcha_action=recaptcha_action,
                )
                if res.get("status") == 200 and res.get("rpc_result") is not None:
                    return res
                log.warning(f"[{account_email}] Bridge RPC {rpc_id} returned status {res.get('status')}: {res.get('error')}")
        except Exception as e:
            log.warning(f"[{account_email}] Extension Bridge RPC failed: {e}")

        # 2. Direct HTTP FlowSession route
        log.info(f"[{account_email}] Executing RPC {rpc_id} via direct FlowSession...")
        session = self.get_session(account_email)
        return await session.execute(
            rpc_id=rpc_id,
            inner_payload=inner_payload,
            source_path=source_path,
            timeout_ms=timeout_ms,
        )

    # ── Image Operations ──────────────────────────────────────────────────────

    async def generate_images(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        model_name: str = "GEM_PIX_2",
        reference_image_ids: Optional[List[str]] = None,
        candidate_count: int = 4,
        account_email: str = "",
    ) -> dict:
        """Generate images via ogiZ0b RPC (Zero DOM)."""
        pid = await self.ensure_project(account_email)
        payload = build_image_generate_payload(
            prompt=prompt,
            project_id=pid,
            model_name=model_name,
            aspect_ratio=aspect_ratio,
            reference_image_ids=reference_image_ids,
            candidate_count=candidate_count,
        )

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["image_generate"],
            inner_payload=payload,
            project_id=pid,
            recaptcha_action="IMAGE_GENERATION",
            account_email=account_email,
            timeout_ms=90000,
        )

        rpc_data = res.get("rpc_result")
        if not rpc_data:
            err = res.get("error") or "No data returned from image generation RPC"
            raise RuntimeError(f"Tạo ảnh Google Flow thất bại: {err}")

        # Parse candidates from response
        # Structure: [ [ [media_id, ...], ... ] ]
        media_items = []
        try:
            candidates_raw = rpc_data[0] if isinstance(rpc_data, list) and len(rpc_data) > 0 else []
            for item in candidates_raw:
                if isinstance(item, list) and len(item) > 0:
                    media_id = item[0]
                    media_items.append({"media_id": media_id})
        except Exception as e:
            log.warning(f"Error parsing candidates from image RPC: {e}")

        return {
            "status": "success",
            "project_id": pid,
            "items": media_items,
            "raw_result": rpc_data,
        }

    async def upload_image(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        filename: str = "upload.jpg",
        account_email: str = "",
    ) -> str:
        """Upload reference image via maseQ RPC directly without file picker DOM."""
        pid = await self.ensure_project(account_email)
        payload = build_image_upload_payload(
            image_bytes=image_bytes,
            mime_type=mime_type,
            filename=filename,
            project_id=pid,
        )

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["image_upload"],
            inner_payload=payload,
            project_id=pid,
            recaptcha_action="UPLOAD_IMAGE",
            account_email=account_email,
        )

        rpc_data = res.get("rpc_result")
        if not rpc_data:
            raise RuntimeError(f"Upload ảnh thất bại: {res.get('error')}")

        # Extract uploaded media ID
        media_id = rpc_data[0] if isinstance(rpc_data, list) and len(rpc_data) > 0 else str(rpc_data)
        return str(media_id)

    async def upscale_image(
        self,
        media_id: str,
        level: int = 2,
        account_email: str = "",
    ) -> dict:
        """Upscale image via SPrCad RPC."""
        pid = await self.ensure_project(account_email)
        payload = build_image_upscale_payload(
            media_id=media_id,
            project_id=pid,
            level=level,
        )

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["image_upscale"],
            inner_payload=payload,
            project_id=pid,
            recaptcha_action="IMAGE_GENERATION",
            account_email=account_email,
        )

        rpc_data = res.get("rpc_result")
        if not rpc_data:
            raise RuntimeError(f"Upscale ảnh thất bại: {res.get('error')}")
        return {"status": "success", "result": rpc_data}

    # ── Video Operations ──────────────────────────────────────────────────────

    async def generate_video(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        model_key: str = "veo_3_1_t2v_lite",
        duration: int = 8,
        video_mode: str = "t2v",
        start_media_id: Optional[str] = None,
        end_media_id: Optional[str] = None,
        ref_media_ids: Optional[List[str]] = None,
        account_email: str = "",
    ) -> str:
        """Submit video generation job via pure RPC (Zero DOM).

        Returns generation media_id or batch UUID.
        """
        pid = await self.ensure_project(account_email)

        mode = (video_mode or "t2v").lower()
        if mode == "i2v" and start_media_id and end_media_id:
            rpc_id = FLOW_RPCS["video_start_end_image"]
            payload = build_video_first_last_payload(
                prompt=prompt,
                start_media_id=start_media_id,
                end_media_id=end_media_id,
                project_id=pid,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                duration=duration,
            )
        elif mode == "i2v" and start_media_id:
            rpc_id = FLOW_RPCS["video_start_image"]
            payload = build_video_start_frame_payload(
                prompt=prompt,
                start_media_id=start_media_id,
                project_id=pid,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                duration=duration,
            )
        elif mode in ("ingredients", "r2v") and ref_media_ids:
            rpc_id = FLOW_RPCS["video_reference_images"]
            payload = build_video_ingredients_payload(
                prompt=prompt,
                ref_media_ids=ref_media_ids,
                project_id=pid,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                duration=duration,
            )
        else:
            rpc_id = FLOW_RPCS["video_text"]
            payload = build_video_t2v_payload(
                prompt=prompt,
                project_id=pid,
                model_key=model_key,
                aspect_ratio=aspect_ratio,
                duration=duration,
            )

        res = await self._execute_rpc(
            rpc_id=rpc_id,
            inner_payload=payload,
            project_id=pid,
            recaptcha_action="VIDEO_GENERATION",
            account_email=account_email,
            timeout_ms=120000,
        )

        rpc_data = res.get("rpc_result")
        if not rpc_data:
            err = res.get("error") or "Không nhận được phản hồi từ Google Flow video RPC"
            raise RuntimeError(f"Tạo video Google Flow thất bại: {err}")

        # Extract generation ID from response
        try:
            if isinstance(rpc_data, list) and len(rpc_data) > 0:
                first_item = rpc_data[0]
                if isinstance(first_item, list) and len(first_item) > 0:
                    gen_id = first_item[0]
                    return str(gen_id)
        except Exception:
            pass

        return str(uuid.uuid4())

    async def poll_video_status(
        self,
        media_ids: List[str],
        account_email: str = "",
    ) -> dict:
        """Poll video generation progress via jwpduf RPC."""
        pid = await self.ensure_project(account_email)
        payload = build_video_poll_payload(project_id=pid, media_ids=media_ids)

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["video_poll"],
            inner_payload=payload,
            project_id=pid,
            account_email=account_email,
            timeout_ms=30000,
        )

        return res.get("rpc_result") or {}

    async def get_media_url(
        self,
        media_id: str,
        account_email: str = "",
    ) -> Optional[str]:
        """Retrieve full signed download URL via as29s RPC."""
        pid = await self.ensure_project(account_email)
        payload = build_get_media_payload(media_id=media_id)

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["get_media"],
            inner_payload=payload,
            project_id=pid,
            account_email=account_email,
            timeout_ms=30000,
        )

        rpc_data = res.get("rpc_result")
        if isinstance(rpc_data, list) and len(rpc_data) > 0:
            url_candidate = rpc_data[0]
            if isinstance(url_candidate, str) and url_candidate.startswith("http"):
                return url_candidate
        return None

    async def upscale_video(
        self,
        media_id: str,
        resolution: str = "1080p",
        account_email: str = "",
    ) -> dict:
        """Upscale video via p0UkFb RPC."""
        pid = await self.ensure_project(account_email)
        payload = build_video_upscale_payload(
            project_id=pid,
            media_id=media_id,
            resolution=resolution,
        )

        res = await self._execute_rpc(
            rpc_id=FLOW_RPCS["video_upscale"],
            inner_payload=payload,
            project_id=pid,
            recaptcha_action="VIDEO_GENERATION",
            account_email=account_email,
        )

        rpc_data = res.get("rpc_result")
        if not rpc_data:
            raise RuntimeError(f"Upscale video thất bại: {res.get('error')}")
        return {"status": "success", "result": rpc_data}


# Global singleton instance
flow_service = FlowService()
