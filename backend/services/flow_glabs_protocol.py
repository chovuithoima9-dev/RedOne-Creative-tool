"""flow_glabs_protocol.py — Protocol and Payload Builders for Google Flow batchexecute RPCs.

Re-engineered directly from G-Labs Studio v2.0.3 and G-Labs Automation v9.0.4:
- Matches `src.flow_google.config` RPC IDs and actions.
- Matches `src.flow_google.payloads` exact candidate structures.
- Provides standard enums and mapping for Aspect Ratio, Models, and Video Modes.
"""
from __future__ import annotations

import random
import uuid
from typing import Any, List, Optional

# ── 1. RPC IDs (Identical to G-Labs src.flow_google.config) ───────────────────

FLOW_RPCS = {
    # Image operations
    "image_generate": "ogiZ0b",
    "image_upload": "maseQ",
    "image_upscale": "SPrCad",

    # Video operations
    "video_text": "YhhmEf",              # Text-to-Video (T2V)
    "video_start_image": "eb1hJf",       # Start frame / Khung hình đầu (I2V)
    "video_start_end_image": "anprQif",  # First + Last frame / Khung hình đầu & cuối
    "video_reference_images": "MZZa6b",  # Ingredients / Thành phần (R2V)
    "video_edit": "ajIps6",              # Video editing
    "video_upscale": "p0UkFb",           # Video upscale
    "video_poll": "jwpduf",              # Status polling
    "get_media": "as29s",                # Download signed URL

    # Projects & account
    "project_create": "jHPbke",
    "get_credits": "nzlxg",
    "get_models": "HTrJv",
}

# ── 2. Model Mappings ─────────────────────────────────────────────────────────

IMAGE_MODELS = {
    "nano_banana_pro": "GEM_PIX_2",
    "nano_banana_2": "BELUGA",
    "nano_banana_2_1": "BELUGA",
    "nano_banana_lite": "HARBOR_SEAL",
    "nano_banana_2_lite": "HARBOR_SEAL",
    "gem_pix_2": "GEM_PIX_2",
    "beluga": "BELUGA",
    "narwhal": "BELUGA",
    "harbor_seal": "HARBOR_SEAL",
    "imagen_3_5": "IMAGEN_3_5",
    "imagen_3": "IMAGEN_3",
}

VIDEO_T2V_MODELS = {
    "fast": "veo_3_1_t2v_fast",
    "lite": "veo_3_1_t2v_lite",
    "quality": "veo_3_1_t2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_t2v_fast",
    "veo_3_1_lite": "veo_3_1_t2v_lite",
    "veo_3_1_quality": "veo_3_1_t2v_quality",
}

VIDEO_I2V_MODELS = {
    "fast": "veo_3_1_i2v_fast",
    "lite": "veo_3_1_i2v_lite",
    "quality": "veo_3_1_i2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_i2v_fast",
    "veo_3_1_lite": "veo_3_1_i2v_lite",
    "veo_3_1_quality": "veo_3_1_i2v_quality",
}

VIDEO_R2V_MODELS = {
    "fast": "veo_3_1_r2v_fast",
    "lite": "veo_3_1_r2v_lite",
    "quality": "veo_3_1_r2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_r2v_fast",
    "veo_3_1_lite": "veo_3_1_r2v_lite",
    "veo_3_1_quality": "veo_3_1_r2v_quality",
}

# ── 3. Aspect Ratio Enums ─────────────────────────────────────────────────────

IMAGE_ASPECT_RATIOS = {
    "1:1": 1,
    "9:16": 2,
    "16:9": 3,
    "3:4": 4,
    "4:3": 5,
}

VIDEO_ASPECT_RATIOS = {
    "16:9": 1,
    "landscape": 1,
    "9:16": 2,
    "portrait": 2,
}

RECAPTCHA_PLACEHOLDER = "__MINT_RECAPTCHA__"


def _make_client_ctx(project_id: str, captcha_token: str = RECAPTCHA_PLACEHOLDER) -> list:
    """Build standardized client_ctx array matching Google Flow Angular frontend."""
    return [
        None, 22, None, None, None,
        project_id,
        None, None, None, None,
        [captcha_token, 1],
    ]


# ── 4. Payload Builders ───────────────────────────────────────────────────────

def build_image_generate_payload(
    prompt: str,
    project_id: str,
    model_name: str = "GEM_PIX_2",
    aspect_ratio: str = "16:9",
    reference_image_ids: Optional[List[str]] = None,
    candidate_count: int = 1,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build ogiZ0b payload (Image Generation). Matches update1_entry6_inner.json."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = _make_client_ctx(project_id, captcha_token)
    ar_code = IMAGE_ASPECT_RATIOS.get(aspect_ratio, 3)

    ref_arr = None
    if reference_image_ids:
        ref_arr = [[ref_id, None, None, None, 1] for ref_id in reference_image_ids]

    prompt_struct = [[[prompt]]]
    candidates = []
    for i in range(max(1, min(candidate_count, 4))):
        c_seed = random.randint(100000000, 2147483647)
        c_batch = b_uuid if i == 0 else str(uuid.uuid4()).upper()
        c_op = str(uuid.uuid4()).upper()
        candidates.append([
            None,
            None,
            ref_arr,
            c_seed,
            ar_code,
            model_name,
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


def build_video_t2v_payload(
    prompt: str,
    project_id: str,
    model_key: str = "veo_3_1_t2v_lite",
    aspect_ratio: str = "16:9",
    duration: int = 8,
    captcha_token: str = RECAPTCHA_PLACEHOLDER,
    batch_uuid: Optional[str] = None,
) -> list:
    """Build YhhmEf payload (Text-to-Video). Matches update3_entry6_video_inner.json."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = _make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    candidate = [
        prompt_struct,
        model_key,
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
    """Build eb1hJf payload (Start Frame / Khung hình đầu). Matches har_eb1hJf_inner.json."""
    b_uuid = batch_uuid or str(uuid.uuid4()).upper()
    client_ctx = _make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    # Slot 4 contains the start frame media ID in position 1
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
        model_key,
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
    client_ctx = _make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    # Slot 4 contains start_media_id at index 1 and end_media_id at index 4
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
        model_key,
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
    client_ctx = _make_client_ctx(project_id, captcha_token)
    ar_code = VIDEO_ASPECT_RATIOS.get(aspect_ratio.lower(), 1)
    if "9:16" in aspect_ratio or "portrait" in aspect_ratio.lower():
        ar_code = 2

    prompt_struct = [None, None, [[[prompt]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()

    # Reference images array: [[None, ref_id], ...] as confirmed by HAR capture
    refs_struct = [[None, ref_id] for ref_id in ref_media_ids]

    candidate = [
        prompt_struct,
        refs_struct,
        model_key,
        ar_code,
        None,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    return [
        [candidate],
        client_ctx,
        [b_uuid, 2],
    ]


def build_video_poll_payload(
    project_id: str,
    media_ids: List[str],
) -> list:
    """Build jwpduf payload to poll video generation status."""
    return [None, None, [media_ids]]


def build_get_media_payload(
    project_id: str,
    media_id: str,
) -> list:
    """Build as29s payload to retrieve full signed media download URL."""
    return [media_id]
