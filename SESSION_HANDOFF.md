# RedOne Creative — Session Handoff

> **Mục đích**: Mở file này trong session AI mới để continue build mà không mất context.
> **Phiên bản hiện tại**: `v1.5.3` — **commit mới nhất trên `main`**.
> **Cập nhật**: 2026-07-17

---

## 1. Tool là gì

**RedOne Creative** — tool web local (FastAPI + vanilla JS) tạo ảnh/video AI qua:
- **Google Labs Flow** (Veo 3.1, Nano Banana, Imagen 4) — credit miễn phí via Chrome Extension Bridge.
- **Shakker.ai** (SD/FLUX.1) — trả bằng power.

Deployment: EXE (PyInstaller `--onedir`) chạy `FastAPI 127.0.0.1:8000`, user dùng qua browser.

### Repo & workspace
- **GitHub**: https://github.com/chovuithoima9-dev/RedOne-Creative-tool
- **Workspace**: `D:\RedOne Creative tool` (Windows)
- **Branch**: `main` only

---

## 2. Quy tắc BẮT BUỘC

1. **KHÔNG push vội**: chỉ commit + push khi user nói "OK push" (hoặc tương đương).
2. **Ko push các thứ liên quan G-labs**: `flow_client.py`, `flow_client_bridge.py` — giữ local khi thay đổi nhạy cảm.
3. **Không hardcode secrets**: dùng `private_config.py` (gitignored) hoặc env vars.
4. **`subprocess_no_window_kwargs()`** từ `ffmpeg_utils.py` cho mọi subprocess → tránh CMD flash trên windowed EXE.
5. **`async def` + `asyncio.to_thread()`** cho blocking I/O trong FastAPI routes.
6. **Hàm < 50 dòng, file < 800 dòng**. Dùng `logging`, không `print()`.

---

## 3. Workspace layout (v1.5.3)

```
RedOne Creative tool/
├── backend/
│   ├── config.py            ← APP_VERSION="1.5.3", paths, model keys, SERVER_PORT=8000
│   ├── main.py              ← FastAPI app + Auth gate middleware + router includes
│   ├── database.py          ← SQLite (accounts, tasks, task_items, settings, shakker_accounts)
│   ├── ws_hub.py            ← WebSocket broadcast hub
│   ├── private_config.py    ← (GITIGNORE) OAuth client id/secret, ALLOWED_EMAIL_DOMAIN
│   ├── routers/
│   │   ├── auth.py          ← /auth/login /callback /me /logout
│   │   ├── accounts.py      ← /api/accounts/*
│   │   ├── content.py       ← /api/content/* (T2V, I2V, upscale-video-AI, upscale-cancel)
│   │   ├── image.py         ← /api/image/* (gen + upscale ảnh)
│   │   ├── long_video.py    ← /api/long-video/*
│   │   ├── shakker.py       ← /api/shakker/*
│   │   ├── shakker_accounts.py ← /api/shakker-accounts/*
│   │   ├── hgstock.py       ← /api/hgstock/* (upload lên HG Stock)
│   │   ├── video_editor.py  ← /api/video-editor/* (ghép audio, etc.)
│   │   ├── sync.py          ← /sync/* (extension bridge; XOR envelope)
│   │   └── analyzer.py, media_tools.py, tasks.py, files.py, settings.py, system.py
│   └── services/
│       ├── flow_client.py       ← Google Flow API client (~1700 LOC, ACTIVE)
│       │                           renew_token(bad_token=None) — chỉ renew khi token khớp bad_token
│       ├── flow_client_bridge.py ← BridgeFlowClient (Extension Bridge, ACTIVE)
│       ├── upscaler.py          ← realesrgan-ncnn-vulkan subprocess
│       │                           _run() check _cancelled_batches giữa mỗi video
│       ├── watermark_video.py   ← Video WM removal (ffmpeg + lama/opencv)
│       ├── ffmpeg_utils.py      ← ffmpeg detection + subprocess_no_window_kwargs()
│       ├── feature_installer.py ← Feature store (catalog, download, extract addons/)
│       ├── updater.py           ← Auto-updater (GitHub Release → zip → install.bat)
│       ├── oauth_auth.py        ← Session file, domain check
│       ├── error_messages.py    ← friendly_error() [Flow] + friendly_shakker_error() [Shakker]
│       └── browser_manager.py, gemini.py, image_utils.py, lama_inpaint.py, setup_wizard.py
│
├── extension/               ← Chrome MV3 "RedOne Auth Helper" (v1.5.3)
│   ├── manifest.json        ← host_permissions: labs.google, shakker.ai, accounts.google.com
│   │                           version: "1.5.3"
│   ├── background.js        ← _pollLoop (poll BRIDGE_HOSTS tuần tự, break at first OK host)
│   │                           BRIDGE_HOSTS = [8000, 8001, 8099, 8098]
│   │                           targetGoogleEmail sync (mới v1.5.2)
│   ├── content.js           ← labs.google content script
│   ├── content_accounts.js  ← accounts.google.com (auto-login helper)
│   └── content_shakker.js   ← shakker.ai token → /sync/shakker-account
│
├── chrome-ext/
│   └── update.xml           ← CRX update feed (v1.5.3)
│
├── photoshop-plugin/        ← CEP Extension "RedOne GenFill" (STANDALONE, độc lập tool)
│   ├── CSXS/manifest.xml    ← ExtensionBundleId: com.redone.genfill, version: 1.0.0
│   ├── js/
│   │   ├── main.js          ← serverMode: 'embedded'|'external'|'none'
│   │   ├── embedded_server.js ← Node.js HTTP port 8099 (DEFAULT_PORT=8099)
│   │   │                       Routes: /sync/status, /sync/next-task, /sync/task-result
│   │   ├── flow_api.js      ← Google Flow calls qua extension bridge
│   │   └── CSInterface.js
│   └── jsx/photoshop.jsx    ← ExtendScript PS actions
│
├── frontend/
│   ├── index.html           ← SPA shell: sidebar + topbar
│   ├── css/
│   └── js/
│       ├── app.js           ← router, WS, theme, OAuth chip, autoScan
│       ├── api.js           ← tất cả fetch wrappers
│       ├── ui.js            ← el(), toast(), icon(), modal(), lightbox (prev/next nav)
│       ├── ws.js            ← WebSocket client + event dispatcher
│       ├── tasks_store.js   ← Global tasks state (Map + localStorage backup)
│       ├── gallery_actions.js ← makeSelectionToolbar
│       │                       onUpscaleVideo → ưu tiên wm_path thay vì output_path
│       └── pages/
│           ├── content.js       ← Tạo Video T2V/I2V
│           │                       _patchWmChipsInPlace() — anti-flicker WM removal
│           ├── image.js         ← Tạo Ảnh
│           │                       sendImagesToI2V: upscale_path > output_path
│           ├── video_upscale.js ← Upscale Video AI
│           │                       Nút "Hủy" per-batch, onBatchDone xử lý d.cancelled
│           ├── storyboard.js    ← Storyboard builder
│           ├── video_watermark.js, audio_merge.js, hgstock.js
│           └── accounts.js, settings.js, tasks_manager.js, ...
│
├── features/index.json      ← Remote feature catalog (GitHub)
├── addons/                  ← (gitignore) Downloaded feature bundles
├── data/                    ← (gitignore) db, auth_session, logs, updates/
├── outputs/                 ← (gitignore) Generated files
├── docs/HUONG_DAN_SU_DUNG.md
├── install_ps_plugin.bat    ← Cài CEP plugin (xcopy, NOT symlink, run as Admin)
└── BUILD_RELEASE.md · PROJECT_CONTEXT.md · README.md
```

---

## 4. Upscale Video AI — chi tiết kỹ thuật

### Backend state trong `content.py`
```python
_upscale_progress: dict[str, dict] = {}
_cancelled_batches: set[str] = set()   # ← PHẢI là set(), KHÔNG phải {}
_active_upscale_procs: dict[str, asyncio.subprocess.Process] = {}
```

### Cancel flow
1. `POST /api/content/upscale-cancel/{batch_id}`
2. `_cancelled_batches.add(batch_id)` + `proc.kill()`
3. Broadcast `video_upscale_batch_done` với `{"cancelled": true}`

### Frontend (`video_upscale.js`)
- Batch header: nút "Hủy" (đỏ, icon x) khi `!task.completed && task.stage !== 'cancelled'`.
- `onBatchDone`: nếu `d.cancelled` → stage='cancelled', không mark video chưa xong là done.
- Polling: `_activePolls` map, `clearInterval` khi cancel hoặc done.

---

## 5. Kết nối Extension ↔ Tool ↔ PS Plugin

```
Chrome Extension
  └─ _pollLoop → BRIDGE_HOSTS = [8000, 8001, 8099, 8098]
      → lấy host đầu tiên phản hồi, break

Nếu RedOne Tool đang chạy (port 8000):
  → Extension chỉ poll 8000 (Tool), KHÔNG bao giờ poll 8099 (PS GenFill)

Để test PS GenFill:
  → Tắt Tool (port 8000 phải đóng) → Extension tự fallback sang 8099
```

---

## 6. Shakker.ai

- Auth: `usertoken` cookie (44-char hex) từ shakker.ai → extension push về `/sync/shakker-account`.
- Upload ref image: oss2 → Alibaba OSS bucket `models-online-persist-us`.
- LoRA: `additionalNetwork: [{modelId: versionId, weight: w, type: 0}]`.
- Error 429 từ Google = **quota hết ngày** (HTTP 429 RESOURCE_EXHAUSTED, per_model_daily_quota). Khác với reCAPTCHA throttle (tạm thời).

---

## 7. Gallery Lightbox Navigation (v1.5.3)

- `ui.js`: `openLightbox(items, index)` → modal fullscreen.
- Nút **‹** / **›** cố định hai bên viewport, glassmorphism style.
- Phím ← → navigate. Phím Escape đóng.
- Tất cả pages có lightbox: Tạo Ảnh, Tạo Video, Storyboard, Shakker.

---

## 8. Database schema (SQLite)

```sql
accounts:          id, email, enabled, credit, tier, proxy, created_at
tasks:             id, project_id, name, mode, quality, image_model,
                   aspect_ratio, resolution, duration, concurrent,
                   total_count, done_count, error_count,
                   character_images_json (JSON config), status, *_at
task_items:        id, task_id, prompt, status, output_path,
                   credit_cost, error_message, extra_json, *_at
settings:          key, value (JSON)
shakker_accounts:  id, user_uuid(UNIQUE), email, token, webid, tier,
                   total_power, used_power, usable_power, concurrent,
                   expiry, enabled, status, last_check_at, created_at
```
Migration idempotent: `_add_column_if_missing(...)` mỗi boot.

---

## 9. WebSocket events (key ones)

| Event | Payload quan trọng | Purpose |
|---|---|---|
| `item_completed` | `output_path, upscale_path, upscale_url` | Per-item done |
| `video_upscale_progress` | `batch_id, percent, current, total` | Upscale tiến trình |
| `video_upscale_batch_done` | `batch_id, results, cancelled?` | Batch xong/hủy |
| `watermark_progress` | `job_id, percent` | WM removal progress |
| `account_updated` | `id, credit, alive` | Credit thay đổi |
| `queue_updated` | `{...}` | Queue snapshot |
| `update_progress` | `stage, percent, message` | Auto-updater |

---

## 10. Bảo mật

Gitignored (KHÔNG commit):
- `backend/private_config.py` — OAuth secrets
- `docs/shakker_capture/`, `*.har` — token thật, HAR dumps
- `data/`, `outputs/` — user data
- `*.pem`, `*.crx`, `/extension.crx` — extension signing
- `addons/` — downloaded feature bundles

Build artifacts KHÔNG push GitHub:
- `*.zip` (extension zip, plugin zip, dist zip)
- Model binaries (`.pth`, `.onnx`, `.onnx.data`)

---

## 11. Version history

| Version | Highlights |
|---|---|
| v1.0.x | Web app cơ bản: T2V/I2V/Image, Playwright |
| v1.1.x | Chrome Extension Bridge, OAuth gate |
| v1.2.x | Shakker.ai, Queue song song, gỡ Playwright |
| v1.3.x | Multi-user, long video nâng cao |
| v1.4.x | Storyboard, video editor |
| v1.5.0 | Upscale Video AI (realesrgan), HG Stock, PS GenFill plugin |
| v1.5.1 | Audio merge nâng cao |
| v1.5.2 | `renew_token(bad_token)`, extension targetGoogleEmail |
| **v1.5.3** | Lightbox nav (prev/next+keys), WM anti-flicker, wm_path upscale, cancel upscale UI+backend, fix `set()` |

---

## 12. Files đọc đầu khi mở session mới

Theo thứ tự ưu tiên:
1. **`SESSION_HANDOFF.md`** (file này)
2. **`backend/config.py`** — APP_VERSION, paths, constants
3. **`backend/main.py`** — auth gate, router includes
4. **`backend/routers/content.py`** — upscale video, cancel logic
5. **`frontend/js/pages/video_upscale.js`** — upscale UI

Sau đó dig sâu theo feature đang làm.

---

## 13. Quick debug

```powershell
# Check ports
$ports = @(8000, 8099); foreach ($p in $ports) {
  $tcp = New-Object System.Net.Sockets.TcpClient
  try { $tcp.Connect("127.0.0.1", $p); "Port $p: OPEN" } catch { "Port $p: CLOSED" }
}

# Test embedded PS server state
Invoke-WebRequest "http://127.0.0.1:8099/sync/state" -UseBasicParsing | Select -Exp Content

# Tail log
Get-Content data/app.log -Tail 50

# Git status nhanh
git status --short && git log --oneline -5
```

---

**End of handoff. Đọc file này đầu mọi session mới về RedOne Creative.**
