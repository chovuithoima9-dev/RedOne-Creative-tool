"""Configuration and Constants for Google Flow Protocol Engine.

Direct reverse-engineered constants from Google Flow (AiSandboxAngularFrontend)
and G-Labs Studio v2.0.4.
"""
from __future__ import annotations

# ── 1. RPC IDs ────────────────────────────────────────────────────────────────
FLOW_RPCS: dict[str, str] = {
    # Image Operations
    "image_generate": "ogiZ0b",
    "image_upload": "maseQ",
    "image_upscale": "SPrCad",

    # Video Operations
    "video_text": "YhhmEf",              # Text-to-Video (T2V)
    "video_start_image": "eb1hJf",       # Start Frame (I2V)
    "video_start_end_image": "anprQif",  # First & Last Frame
    "video_reference_images": "MZZa6b",  # Ingredients / Reference (R2V)
    "video_edit": "ajIps6",              # Video editing
    "video_upscale": "p0UkFb",           # Video upscale
    "video_poll": "jwpduf",              # Status polling
    "get_media": "as29s",                # Download signed URL

    # Entities, Workflows & Audio
    "entity_create": "C4BZMd",
    "entity_update": "rzMKMb",
    "audio_generate": "no0P6",
    "media_set_visibility": "lt8g5",
    "workflow_set_name": "mYWVGd",
    "set_watermark": "DA4VGb",

    # Project & Account Metadata
    "project_create": "jHPbke",
    "get_credits": "nzlxg",
    "get_models": "HTrJv",
}

# ── 2. Endpoints & reCAPTCHA ──────────────────────────────────────────────────
FLOW_ORIGIN = "https://flow.google.com"
FLOW_ABOUT_URL = "https://flow.google.com/about"
FLOW_APP_NAME = "AiSandboxAngularFrontend"
FLOW_RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
FLOW_RECAPTCHA_SCRIPT_URL = f"https://www.google.com/recaptcha/enterprise.js?render={FLOW_RECAPTCHA_SITE_KEY}"

# Default placeholder replaced inline by token providers
RECAPTCHA_PLACEHOLDER = "__MINT_RECAPTCHA__"

# ── 3. Image Models ───────────────────────────────────────────────────────────
IMAGE_MODELS: dict[str, str] = {
    "nano_banana_pro": "HARBOR_SEAL",
    "nano_banana_2": "HARBOR_SEAL",
    "nano_banana_lite": "HARBOR_SEAL",
    "nano_banana_2_lite": "HARBOR_SEAL",
    "gem_pix_2": "GEM_PIX_2",
    "narwhal": "NARWHAL",
    "harbor_seal": "HARBOR_SEAL",
    "imagen_3_5": "IMAGEN_3_5",
    "imagen_3": "IMAGEN_3",
}

# ── 4. Video Models ───────────────────────────────────────────────────────────
VIDEO_T2V_MODELS: dict[str, str] = {
    "fast": "veo_3_1_t2v_fast",
    "lite": "veo_3_1_t2v_lite",
    "quality": "veo_3_1_t2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_t2v_fast",
    "veo_3_1_lite": "veo_3_1_t2v_lite",
    "veo_3_1_quality": "veo_3_1_t2v_quality",
}

VIDEO_I2V_MODELS: dict[str, str] = {
    "fast": "veo_3_1_i2v_fast",
    "lite": "veo_3_1_i2v_lite",
    "quality": "veo_3_1_i2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_i2v_fast",
    "veo_3_1_lite": "veo_3_1_i2v_lite",
    "veo_3_1_quality": "veo_3_1_i2v_quality",
}

VIDEO_R2V_MODELS: dict[str, str] = {
    "fast": "veo_3_1_r2v_fast",
    "lite": "veo_3_1_r2v_lite",
    "quality": "veo_3_1_r2v_quality",
    "omni_flash": "omni_flash",
    "veo_3_1_fast": "veo_3_1_r2v_fast",
    "veo_3_1_lite": "veo_3_1_r2v_lite",
    "veo_3_1_quality": "veo_3_1_r2v_quality",
}

# ── 5. Aspect Ratios ──────────────────────────────────────────────────────────
IMAGE_ASPECT_RATIOS: dict[str, int] = {
    "1:1": 1,
    "square": 1,
    "9:16": 2,
    "portrait": 2,
    "16:9": 3,
    "landscape": 3,
    "3:4": 4,
    "4:3": 5,
}

VIDEO_ASPECT_RATIOS: dict[str, int] = {
    "16:9": 1,
    "landscape": 1,
    "9:16": 2,
    "portrait": 2,
}

# ── 6. Video Upscale Resolutions ──────────────────────────────────────────────
VIDEO_UPSCALE_RESOLUTIONS: dict[str, int] = {
    "720p": 1,
    "1080p": 2,
    "4k": 3,
}

# ── 7. Default Browser Client Headers ─────────────────────────────────────────
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

DEFAULT_SEC_CH_UA = {
    "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
