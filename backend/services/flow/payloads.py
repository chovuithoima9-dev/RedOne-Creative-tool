"""Google Flow batchexecute RPC Payload Builders.

Exact JSON structures required by Google Flow RPCs:
- Matches G-Labs Studio v2.0.4 `src.flow_google.payloads` specifications.
- Fully typed and zero DOM dependencies.
"""
from __future__ import annotations

import base64
import random
import uuid
from typing import Any, List, Optional

from .config import (
    IMAGE_ASPECT_RATIOS,
    IMAGE_MODELS,
    RECAPTCHA_PLACEHOLDER,
    VIDEO_ASPECT_RATIOS,
    VIDEO_I2V_MODELS,
    VIDEO_R2V_MODELS,
    VIDEO_T2V_MODELS,
    VIDEO_UPSCALE_RESOLUTIONS,
)


def make_client_ctx(project_id: str, captcha_token: str = RECAPTCHA_PLACEHOLDER) -> list:
    """Standard client_ctx array matching Google Flow Angular frontend."""
    return [
        None, 22, None, None, None,
        project_id,
        None, None, None, None,
        [captcha_token, 1],
    ]


# ── Image Payloads ────────────────────────────────────────────────────────────

def build_image_generate_payload(
    prompt: str,
    project_id: str,
    model_name: str = "GEM_PIX_2",
    aspect_ratio: str = "16:9",
    reference_image_ids: Optional[List[str]] = None,
    candidate_count: int = 4,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build ogiZ0b payload (Image Generation)."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = make_client_ctx(project_id, captcha_token)
    ar_code = IMAGE_ASPECT_RATIOS.get(str(aspect_ratio).lower(), 3)
    norm_model = IMAGE_MODELS.get(model_name.lower(), model_name)

    ref_arr = None
    if reference_image_ids:
        ref_arr = [[ref_id, None, None, None, 1] for ref_id in reference_image_ids]

    prompt_struct = [[[prompt]]]
    candidates = []
    count = max(1, min(candidate_count, 4))
    for i in range(count):
        c_seed = random.randint(100000000, 2147483647)
        c_batch = b_uuid if i == 0 else str(uuid.uuid4()).upper()
        c_op = str(uuid.uuid4()).upper()
        candidates.append([
            None,
            None,
            ref_arr,
            c_seed,
            ar_code,
            norm_model,
            None,
            client_ctx,
            prompt_struct,
            None,
            None,
            None,
            c_batch,
            c_op,
        ])

    return [
        None,
        candidates,
        1,
        client_ctx,
        [b_uuid],
    ]


def build_image_upload_payload(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    filename: str = "upload.jpg",
    project_id: str = "",
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
) -> list:
    """Build maseQ payload (Image Upload RPC)."""
    b64_str = base64.b64encode(image_bytes).decode("ascii")
    client_ctx = make_client_ctx(project_id, captcha_token)
    return [
        [b64_str, mime_type, filename],
        client_ctx,
    ]


def build_image_upscale_payload(
    media_id: str,
    project_id: str = "",
    level: int = 2,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
) -> list:
    """Build SPrCad payload (Image Upscale)."""
    client_ctx = make_client_ctx(project_id, captcha_token)
    return [
        media_id,
        level,
        client_ctx,
    ]


# ── Video Payloads ────────────────────────────────────────────────────────────

def build_video_t2v_payload(
    prompt: str,
    project_id: str,
    model_key: str = "veo_3_1_t2v_lite",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build YhhmEf payload (Text-to-Video)."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    norm_model = VIDEO_T2V_MODELS.get(model_key.lower(), model_key)
    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    candidate = [
        prompt_struct,
        norm_model,
        ar_code,
        None,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    return [
        [candidate],
        client_ctx,
        [b_uuid, 2],
    ]


def build_video_start_frame_payload(
    prompt: str,
    start_media_id: str,
    project_id: str,
    model_key: str = "veo_3_1_i2v_lite",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build eb1hJf payload (Start Frame / Khung hình đầu I2V)."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    norm_model = VIDEO_I2V_MODELS.get(model_key.lower(), model_key)
    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    image_struct = [
        None,
        start_media_id,
        None,
        None,
        None,
        [None, None, 1, 1],
    ]

    candidate = [
        prompt_struct,
        norm_model,
        ar_code,
        None,
        image_struct,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    return [
        [candidate],
        client_ctx,
        [b_uuid, 2],
    ]


def build_video_first_last_payload(
    prompt: str,
    start_media_id: str,
    end_media_id: str,
    project_id: str,
    model_key: str = "veo_3_1_i2v_lite",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build anprQif payload (Start + End Frame / Khung hình đầu & cuối)."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    norm_model = VIDEO_I2V_MODELS.get(model_key.lower(), model_key)
    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    image_struct = [
        None,
        start_media_id,
        None,
        None,
        end_media_id,
        [None, None, 1, 1],
    ]

    candidate = [
        prompt_struct,
        norm_model,
        ar_code,
        None,
        image_struct,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    return [
        [candidate],
        client_ctx,
        [b_uuid, 2],
    ]


def build_video_ingredients_payload(
    prompt: str,
    ref_media_ids: List[str],
    project_id: str,
    model_key: str = "veo_3_1_r2v_lite",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build MZZa6b payload (Ingredients / Reference Images / Thành phần)."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    norm_model = VIDEO_R2V_MODELS.get(model_key.lower(), model_key)
    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    refs_struct = [[ref_id, None, None, None, 1] for ref_id in ref_media_ids]

    candidate = [
        prompt_struct,
        norm_model,
        ar_code,
        None,
        refs_struct,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    return [
        [candidate],
        client_ctx,
        [b_uuid, 2],
    ]


def build_video_upscale_payload(
    project_id: str,
    media_id: str,
    resolution: str = "1080p",
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
) -> list:
    """Build p0UkFb payload (Video Upscale)."""
    client_ctx = make_client_ctx(project_id, captcha_token)
    res_code = VIDEO_UPSCALE_RESOLUTIONS.get(resolution.lower(), 2)
    return [
        media_id,
        res_code,
        client_ctx,
    ]


def build_video_poll_payload(
    project_id: str,
    media_ids: List[str],
) -> list:
    """Build jwpduf payload to poll video generation status."""
    return [None, None, [media_ids]]


def build_get_media_payload(
    media_id: str,
) -> list:
    """Build as29s payload to retrieve full signed media download URL."""
    return [media_id]


# ── Project & Metadata Payloads ───────────────────────────────────────────────

def build_project_create_payload(
    display_name: str = "Untitled Project",
) -> list:
    """Build jHPbke payload to create a new project."""
    return [display_name]


def build_credits_payload() -> list:
    """Build nzlxg payload to query account quota & subscription tier."""
    return []


def build_models_payload() -> list:
    """Build HTrJv payload to list enabled models."""
    return []
