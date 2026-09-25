"""Application constants and enums."""
from __future__ import annotations
import os
import sys
from enum import Enum
from pathlib import Path

APP_NAME = "RedOne Creative"
APP_VERSION = "1.6.0"
SERVER_PORT = 8000

# GitHub repo for auto-update check (releases API)
GITHUB_REPO = "kiennt-bit/RedOne-Creative-tool"

# Google Form cho nút "Góp ý / Báo lỗi" trong tool. Tạo form bằng
# tools/create_feedback_form.gs (Apps Script) rồi dán "PUBLISHED URL" vào đây.
# Để rỗng "" → nút feedback tự fallback về GitHub Issues.
FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfANsadZft38RZnMDmjNYik9urxauHHJ-qpxcqHzyWBP6OEmw/viewform"

# ── Path resolution: dev vs PyInstaller frozen ─────────────
# In dev:    BASE_DIR = project root (where backend/ frontend/ live)
#            DATA/OUTPUT under BASE_DIR
# In frozen: BASE_DIR (for static assets) = PyInstaller temp/bundle dir
#            DATA/OUTPUT must be PERSISTENT — placed next to the .exe
IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    # PyInstaller: assets bundled in sys._MEIPASS, but user data must persist
    # alongside the .exe so it survives app updates and re-extractions.
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    EXE_DIR = Path(sys.executable).resolve().parent
    BASE_DIR = BUNDLE_DIR              # for serving frontend/ static
    USER_DATA_ROOT = EXE_DIR           # for data/, outputs/
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    USER_DATA_ROOT = BASE_DIR

DATA_DIR = USER_DATA_ROOT / "data"
OUTPUT_DIR = USER_DATA_ROOT / "outputs"
COOKIES_DIR = DATA_DIR / "cookies"
DB_PATH = DATA_DIR / "navtools.db"
# Kho tính năng (feature store): downloaded "frontend"/"asset" features land
# here (next to the .exe so they survive app updates) and are served via the
# /addons static mount. Must be writable — the installer grants Modify.
# NOTE: renamed from "extensions" → "addons" (v1.5.1+) to avoid user
# confusion with the Chrome "extension" folder that sits alongside.
EXT_DIR = USER_DATA_ROOT / "addons"

for d in (DATA_DIR, OUTPUT_DIR, COOKIES_DIR, OUTPUT_DIR / "video", OUTPUT_DIR / "image", EXT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Kho tính năng (feature catalog, A1111-style remote index) ─────────
# The catalog is an index.json hosted on GitHub (editable centrally, no app
# rebuild needed). Override with env REDONE_FEATURES_URL. Downloads are
# restricted to this host allowlist + a declared sha256 (see feature_installer).
FEATURES_INDEX_URL = os.getenv("REDONE_FEATURES_URL", "").strip() or (
    "https://raw.githubusercontent.com/kiennt-bit/RedOne-Creative-tool-Addon/main/features/index.json"
)
FEATURES_ALLOWED_HOSTS = (
    "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "codeload.github.com",
)

# ── HG Stock upload integration ───────────────────────────────────────
# API endpoints for uploading images/videos/audio to HG Stock.
# Client credentials stored in private_config.py (gitignored).
HGSTOCK_API_URL = {
    "dev": "https://stock-api.hgmedia.dev",
    "prod": "https://stock-api.hgmedia.app",
}
HGSTOCK_IDENTITY_URL = {
    "dev": "https://accounts.hgmedia.dev",
    "prod": "https://accounts.hgmedia.app",
}
HGSTOCK_ADMIN_URL = {
    "dev": "https://stock-admin.hgmedia.dev",
    "prod": "https://stock-admin.hgmedia.app",
}


def _resolve_hgstock() -> dict:
    """Read HG Stock credentials from private_config.py (gitignored)."""
    defaults = {
        "env": "dev",
        "client_id": "",
        "client_secret": "",
        "scope": "",
        "user_id": "",
    }
    try:
        from . import private_config as _pc  # type: ignore
        defaults["env"] = str(getattr(_pc, "HGSTOCK_ENV", "dev") or "dev").strip()
        defaults["client_id"] = str(getattr(_pc, "HGSTOCK_CLIENT_ID", "") or "").strip()
        defaults["client_secret"] = str(getattr(_pc, "HGSTOCK_CLIENT_SECRET", "") or "").strip()
        defaults["scope"] = str(getattr(_pc, "HGSTOCK_SCOPE", "") or "").strip()
        defaults["user_id"] = str(getattr(_pc, "HGSTOCK_USER_ID", "") or "").strip()
    except Exception:
        pass
    return defaults


HGSTOCK_CREDS = _resolve_hgstock()

# Preset project names for the upload UI — user can also type a custom name.
HGSTOCK_PROJECT_PRESETS = [
    "Relax", "Techno", "Sóng Âm", "Giáng Sinh", "Lofi", "Deep House",
    "Jazz", "Classical", "Piano Cover", "Afro House", "Focus", "Blues",
]

# Default folder ID on HG Stock — all uploads go here unless overridden.
HGSTOCK_DEFAULT_FOLDER_ID = "f2dc51a3-3b37-46fe-b6b2-e493297f44ca"


# ── RedOne Hub (multi-user management server) ─────────────────────────
# Central server for identity / roles / internal credit / team monitoring.
# Empty = DISABLED → tool runs standalone (single-user, current behaviour).
# Enable by setting env REDONE_HUB_URL, or HUB_BASE_URL in private_config.py
# (e.g. "https://hub.redone.vn"). All Hub calls are best-effort: if it is
# unreachable, gen still works locally.
def _resolve_hub_url() -> str:
    url = os.getenv("REDONE_HUB_URL", "").strip()
    if not url:
        try:
            from . import private_config as _pc  # type: ignore
            url = str(getattr(_pc, "HUB_BASE_URL", "") or "").strip()
        except Exception:
            url = ""
    return url.rstrip("/")


HUB_BASE_URL = _resolve_hub_url()
HUB_TIMEOUT_S = float(os.getenv("REDONE_HUB_TIMEOUT", "6"))
HUB_SESSION_FILE = DATA_DIR / "hub_session.json"


# ── Firebase Usage Tracking ───────────────────────────────────────────
# Centralized behavioural tracking (images, videos, scripts created +
# session time) pushed to Cloud Firestore so admins can monitor usage
# across all local tool instances.
#
# Credentials are loaded from private_config.py (baked into EXE at build)
# or environment variables — NEVER hardcoded in tracked source code.
#
# To DISABLE tracking on a specific machine, set
#   FIREBASE_TRACKING_ENABLED = False  in private_config.py
# or set env  REDONE_FIREBASE_TRACKING=0

def _resolve_firebase() -> tuple[dict, str, bool]:
    project_id = ""
    enabled = False
    sa_info: dict = {}

    # 1. Resolve from private_config (baked into EXE or local)
    try:
        from . import private_config as _pc  # type: ignore
        sa = getattr(_pc, "FIREBASE_SERVICE_ACCOUNT_INFO", None)
        if isinstance(sa, dict) and sa.get("project_id"):
            sa_info = sa
            project_id = sa.get("project_id", "")
        pid = getattr(_pc, "FIREBASE_PROJECT_ID", None)
        if pid:
            project_id = str(pid).strip()
        e = getattr(_pc, "FIREBASE_TRACKING_ENABLED", None)
        if e is not None:
            enabled = bool(e)
    except Exception:
        pass

    # 2. Env var overrides
    env_val = os.getenv("REDONE_FIREBASE_TRACKING", "").strip()
    if env_val in ("0", "false", "no"):
        enabled = False
    elif env_val in ("1", "true", "yes"):
        enabled = True

    env_pid = os.getenv("REDONE_FIREBASE_PROJECT_ID", "").strip()
    if env_pid:
        project_id = env_pid

    env_sa = os.getenv("REDONE_FIREBASE_SA_JSON", "").strip()
    if env_sa:
        try:
            parsed = json.loads(env_sa)
            if isinstance(parsed, dict):
                sa_info = parsed
                if not project_id:
                    project_id = sa_info.get("project_id", "")
        except Exception:
            pass

    return sa_info, project_id, (enabled and bool(project_id))


_FIREBASE_SA, FIREBASE_PROJECT_ID, FIREBASE_TRACKING_ENABLED = _resolve_firebase()
FIREBASE_SERVICE_ACCOUNT_INFO: dict = _FIREBASE_SA

# Heartbeat interval (seconds) for session-time tracking.
# 180s (3 min) conserves Spark plan 20,000 writes/day quota across team members.
FIREBASE_HEARTBEAT_INTERVAL_S = 180

# Admin emails — only these users can see the Tracking tab and view stats.
# Override in private_config.py to add/remove admins without changing code.
def _resolve_tracking_admins() -> list[str]:
    defaults = [
        "kiennt@redone.vn",
    ]
    try:
        from . import private_config as _pc  # type: ignore
        custom = getattr(_pc, "TRACKING_ADMIN_EMAILS", None)
        if custom is not None and isinstance(custom, (list, tuple)):
            return [e.strip().lower() for e in custom if e.strip()]
    except Exception:
        pass
    return [e.strip().lower() for e in defaults]


TRACKING_ADMIN_EMAILS: list[str] = _resolve_tracking_admins()


class TaskMode(str, Enum):
    IMAGE = "IMAGE"
    CHAR_IMAGE = "CHAR_IMAGE"
    CHAR_VIDEO = "CHAR_VIDEO"
    VIDEO_PLAIN = "VIDEO_PLAIN"
    VIDEO_REF = "VIDEO_REF"
    FRAME_VIDEO = "FRAME_VIDEO"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class ItemStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    GENERATING = "GENERATING"
    DOWNLOADING = "DOWNLOADING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class AccountTier(str, Enum):
    FREE = "FREE"
    ULTRA = "ULTRA"
    PRO = "PRO"
    PAYGATE_TIER_TWO = "PAYGATE_TIER_TWO"


VIDEO_MODELS = {
    "omni_flash": {"name": "Omni 1.1 Flash", "cost": 10},
    "lite": {"name": "Veo 3.1 – Lite", "cost": 5},
    "fast": {"name": "Veo 3.1 – Fast", "cost": 10},
    "quality": {"name": "Veo 3.1 – Quality", "cost": 100},
}

# NOTE: nothing reads this dict — the real model list lives in the frontend
# dropdowns (image.js / storyboard.js) and the key→codename mapping in
# FlowClient.IMAGE_MODEL_MAP. Kept in sync so it doesn't mislead.
IMAGE_MODELS = {
    "nano_banana_pro": {"name": "Nano Banana Pro", "cost": 0},
    "nano_banana_2": {"name": "Nano Banana 2", "cost": 0},
    "nano_banana_lite": {"name": "Nano Banana Lite", "cost": 0},
}

ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
RESOLUTIONS = ["720p", "1080p"]

GEMINI_MODELS_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemma-4-31b-it",
]

# Chain for the "Ý tưởng → Prompt (Ảnh)" + Storyboard + Analyzer paths.
GEMINI_IMAGE_PROMPT_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

MAX_CONCURRENT_TASKS = 20
MAX_RETRY_COUNT = 3
POLL_INTERVAL_SECONDS = 5.0
MAX_CHARACTER_IMAGES = 100
SERVER_PORT = 8000

# ── Google Flow API endpoints ────────────────────────────────
AISANDBOX_BASE = "https://aisandbox-pa.googleapis.com/v1"
TRPC_BASE = "https://labs.google/fx/api/trpc"
SESSION_URL = "https://labs.google/fx/api/auth/session"
FLOW_URL = "https://flow.google.com"
IMAGE_FX_URL = "https://labs.google/fx/tools/image-fx"

# ── Browser Headers ──────────────────────────────────────────
X_CLIENT_DATA = "CIa2yQEIprbJAQipncoBCLb9ygEIlqHLAQiFoM0BCNmqzwEY/qXPARikqM8BGMOrzwE="
X_BROWSER_VALIDATION = "AKIAtsVHZoiKbPixy+qSK1BgKWo="

DEFAULT_ACCOUNT_EXPIRY_DAYS = 30

def cost_for_model_key(model_key: str) -> int:
    if not model_key:
        return 10
    mk = model_key.lower()
    # Omni Flash (key pattern abra_<mode>_<N>s): Google prices it BY DURATION
    # (4s=7, 6s=10, 8s=12, 10s=15). Must run before the generic checks below.
    if mk.startswith("abra_"):
        import re as _re
        _m = _re.search(r"_(\d+)s", mk)
        _d = int(_m.group(1)) if _m else 8
        return {4: 7, 6: 10, 8: 12, 10: 15}.get(_d, 12)
    if "_low_priority" in mk or "_relaxed" in mk:
        return 0
    if "_extension_lite" in mk or "_extension_fast" in mk:
        return 0
    if "_lite" in mk:
        return 5
    if "_fast" in mk:
        return 10
    return 100


# Static model keys for Veo 3.1 & Omni (Standard G-Labs / Flow batchexecute keys)
T2V_MODEL_MAP = {
    "omni_flash": "omni_flash",
    "lite":       "veo_3_1_t2v_lite",
    "fast":       "veo_3_1_t2v_fast",
    "quality":    "veo_3_1_t2v_quality",
    "lite_lp":    "veo_3_1_t2v_lite",
}

I2V_MODEL_MAP = {
    "omni_flash": "omni_flash",
    "lite":       "veo_3_1_i2v_lite",
    "fast":       "veo_3_1_i2v_fast",
    "quality":    "veo_3_1_i2v_quality",
    "lite_lp":    "veo_3_1_i2v_lite",
}

# Extend video model keys
EXTEND_MODEL_MAP = {
    "omni_flash": "omni_flash",
    "lite":       "veo_3_1_t2v_lite",
    "fast":       "veo_3_1_t2v_fast",
    "quality":    "veo_3_1_t2v_quality",
    "lite_lp":    "veo_3_1_t2v_lite",
}

# Interpolation (first-frame + last-frame -> video) model keys.
INTERPOLATION_MODEL_MAP = {
    "omni_flash": "omni_flash",
    "lite":       "veo_3_1_i2v_lite",
    "fast":       "veo_3_1_i2v_fast",
    "quality":    "veo_3_1_i2v_quality",
    "lite_lp":    "veo_3_1_i2v_lite",
}


def interpolation_model_for(quality: str) -> str:
    """Model key for first+last-frame interpolation (Loop video)."""
    return INTERPOLATION_MODEL_MAP.get(quality, INTERPOLATION_MODEL_MAP["lite"])

# Allowed duration options (seconds) per model preset.
VIDEO_DURATIONS_BY_MODEL = {
    "omni_flash": [4, 6, 8, 10],
    "fast":       [4, 6, 8],
    "quality":    [8],
}

DEFAULT_VIDEO_DURATION = 8


def allowed_durations(quality: str) -> list[int]:
    """Return the duration options (seconds) allowed for a given model preset."""
    return VIDEO_DURATIONS_BY_MODEL.get(quality, [4, 6, 8])


def clamp_duration(quality: str, duration: int | None) -> int:
    """Clamp a requested duration to the closest allowed value for the model."""
    allowed = allowed_durations(quality)
    if not duration or duration not in allowed:
        return DEFAULT_VIDEO_DURATION if DEFAULT_VIDEO_DURATION in allowed else allowed[-1]
    return duration


# Backwards-compat alias (some old code still references VIDEO_MODEL_MAP)
VIDEO_MODEL_MAP = T2V_MODEL_MAP


def video_model_for(quality: str, mode: str = "t2v", duration: int = 8) -> str:
    """Pick the correct internal model key.

    - Omni Flash: duration is encoded in the key itself.
      Pattern: `abra_<t2v|i2v>_<duration>s` (eg. `abra_t2v_10s`).
    - Veo 3.1: static keys from T2V/I2V_MODEL_MAP. Duration is ignored
      because we haven't captured Veo's duration-variant keys yet.
    """
    m = (mode or "t2v").lower()
    if quality == "omni_flash":
        d = clamp_duration("omni_flash", duration)
        # CONFIRMED from labs.google HAR: `abra_t2v_<N>s` and `abra_i2v_<N>s`
        # are separate keys — I2V is NOT unified with T2V as previously assumed.
        # Duration is encoded in the key. Only abra_i2v_4s was captured
        # directly; 6s/8s/10s follow the same pattern as the T2V variants.
        return f"abra_i2v_{d}s" if m == "i2v" else f"abra_t2v_{d}s"
    table = I2V_MODEL_MAP if m == "i2v" else T2V_MODEL_MAP
    return table.get(quality, table["fast"])


_SLUG_INVALID_RE = None


def slugify_folder(name: str, fallback: str = "task") -> str:
    """Convert a free-text task name into a safe folder name.
    Keeps Vietnamese letters but strips path separators and special chars.
    """
    import re
    global _SLUG_INVALID_RE
    if _SLUG_INVALID_RE is None:
        # Reject path separators, control chars, and Windows reserved chars
        _SLUG_INVALID_RE = re.compile(r'[\\/<>:"|?*\x00-\x1f]+')
    if not name:
        return fallback
    s = _SLUG_INVALID_RE.sub(" ", str(name).strip())
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._ ")
    return s[:80] or fallback


def get_save_dir(
    kind: str,
    task_id: int | str,
    task_name: str | None = None,
) -> "Path":
    """Return where a generated file should be saved.

    Layout:
        outputs/<kind>/<YYYY-MM-DD>/<task_name|task_id>/

    If auto_save_outputs is False, the same layout is used under
    outputs/_pending/ (auto-cleaned after 1 week).

    Kind is "video" or "image".
    """
    from datetime import datetime
    from .database import db  # local import — avoid circular
    auto_save = db.get_setting("auto_save_outputs", True)
    if isinstance(auto_save, str):
        auto_save = auto_save.lower() not in ("false", "0", "no")
    base = OUTPUT_DIR if auto_save else (OUTPUT_DIR / "_pending")
    today = datetime.now().strftime("%Y-%m-%d")
    # Always unique per task: append the (numeric) task_id so two tasks that
    # share a name (e.g. duplicate time-based defaults) never write into the
    # same folder and overwrite each other's files.
    slug = slugify_folder(task_name or "", fallback="")
    folder = f"{slug}_{task_id}" if slug else f"task_{task_id}"
    d = base / kind / today / folder
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_item_stem(task: dict, item_id: int) -> str:
    """Filename stem `<project>_<stt>` for a generated item, where <stt> is the
    item's 1-based position WITHIN its task (prompt/creation order) — so files
    read like `chan_dung_3.png` instead of the global `item_457.png`. Falls back
    to the global item id only if the per-task position can't be resolved."""
    from .database import db  # local import — avoid circular
    slug = slugify_folder(task.get("name") or "", fallback=f"task_{task.get('id')}")
    seq = None
    try:
        for i, it in enumerate(db.get_task_items(task.get("id")), start=1):
            if it["id"] == item_id:
                seq = i
                break
    except Exception:
        pass
    return f"{slug}_{seq if seq is not None else item_id}"
