# RedOne Creative — Project Context

> **Mục đích file này**: tóm tắt toàn bộ kiến trúc + quyết định + lịch sử để mở session AI mới (hoặc onboard dev mới) mà không mất context. Đọc file này + xem code = hiểu hệ thống.
>
> **Cập nhật lần cuối**: 2026-07-18 · phiên bản hiện tại **v1.5.4**

---

## 0. TL;DR thay đổi lớn gần đây

- **v1.5.4**: Bỏ "Tách Nền" + "Xóa Logo Ảnh" (xử lý ảnh) + "Ghép Audio"; gộp nhóm sidebar "Xử lý ảnh" + "Xử lý video" → **"Xử lý media"** (alias nhóm cũ trong `renderFeatureNav` để tính năng Kho không mất tab); "Resize Hàng Loạt" hỗ trợ cả **video** (ffmpeg scale/pad/crop, xuất MP4) + làm gọn UI kết quả. **Lưu ý: bản build EXE v1.5.4 chưa bao gồm "HG Stock" (đang triển khai dở).**
- **v1.5.3**: Gallery lightbox nav (prev/next + phím mũi tên), fix video WM chớp giật (`_patchWmChipsInPlace`), prefer `wm_path` khi gửi sang upscale AI, nút hủy upscale video, fix `_cancelled_batches = set()` (không phải `{}`).
- **v1.5.2**: `flow_client.renew_token(bad_token=None)` mới, extension auth cải thiện (targetGoogleEmail sync).
- **v1.5.x**: Thêm **Storyboard**, **Ghép Audio nâng cao**, **HG Stock upload**, **Upscale Video AI** (realesrgan-ncnn-vulkan), **Photoshop GenFill plugin** (CEP + embedded bridge server port 8099).
- **v1.4.x**: Shakker content pipeline mở rộng, storyboard analyzer.
- **v1.3.x**: Multi-user OAuth, long video nâng cao, video watermark cải thiện.
- **v1.2.x**: Shakker.ai, Queue song song, gỡ Vertex AI/Playwright.

---

## 1. Bối cảnh dự án

### Workspace location (CANONICAL)
```
D:\RedOne Creative tool\        ← PROJECT CHÍNH
```
> ⚠️ Mọi chỉnh sửa đi vào **`D:\RedOne Creative tool`**.

### Repo GitHub
**https://github.com/chovuithoima9-dev/RedOne-Creative-tool** — user `chovuithoima9-dev`.

---

## 2. Kiến trúc tổng quát

### Mô hình deployment
```
GitHub Releases (zip)  →  RedOne Creative.exe (PyInstaller --onedir)
                              ↓
                       FastAPI 127.0.0.1:8000  ──(auth gate @redone.vn)──►  SPA UI
                              ↓
                  Chrome Extension "RedOne Auth Helper" (cài unpacked, v1.5.3)
                     ├─ tab labs.google   → token/cookies Flow  ──► /sync/*
                     └─ tab shakker.ai    → token Shakker        ──► /sync/shakker-account
```

### 2 lớp "tài khoản" — ĐỪNG NHẦM
| Lớp | Mục đích | Cơ chế |
|-----|----------|--------|
| **Đăng nhập tool** | Cổng bảo vệ — chỉ `@redone.vn` mới vào | OAuth Google, session lưu `data/auth_session.json` (30 ngày), per-máy |
| **Tài khoản tạo ảnh** | Account thực thi gen | Google (labs.google) cho Flow + tài khoản Shakker — lấy qua Chrome Extension |

### Tech stack
**Backend**: Python 3.12+ (dev 3.14) · FastAPI + uvicorn (8000) · WebSocket `/ws` · SQLite (`data/navtools.db`) · httpx · oss2 · google-genai (Gemini) · yt-dlp · realesrgan-ncnn-vulkan.
**Frontend**: Vanilla JS ES modules (không framework, không bundler) · CSS variables (light/dark).
**Extension**: Chrome MV3, XOR-enveloped bridge `/sync/*`. `BRIDGE_HOSTS = [8000, 8001, 8099, 8098]`.
**Packaging**: PyInstaller `--onedir` → `dist/RedOne Creative/` (~400-600MB).
**PS Plugin**: CEP extension (`com.redone.genfill`) + Node.js embedded server port 8099. **Phát hành riêng** qua `RedOne-GenFill-Plugin.zip`, độc lập với tool RedOne.

---

## 3. Cấu trúc thư mục (v1.5.3)

```
RedOne Creative tool/
├── backend/
│   ├── main.py              ← FastAPI app, lifespan, AUTH GATE middleware
│   ├── config.py            ← APP_VERSION ("1.5.3"), paths, model maps, SERVER_PORT=8000
│   ├── database.py          ← SQLite ORM
│   ├── ws_hub.py            ← WebSocket broadcast hub
│   ├── private_config.py    ← (GITIGNORE) OAuth client id/secret, ALLOWED_EMAIL_DOMAIN
│   ├── routers/
│   │   ├── auth.py          ← /auth/login, /callback, /me, /logout
│   │   ├── accounts.py      ← /api/accounts/*
│   │   ├── content.py       ← /api/content/* (T2V + I2V + Upscale Video AI + upscale-cancel)
│   │   ├── image.py         ← /api/image/*
│   │   ├── long_video.py    ← /api/long-video/*
│   │   ├── shakker.py       ← /api/shakker/*
│   │   ├── shakker_accounts.py ← /api/shakker-accounts/*
│   │   ├── hgstock.py       ← /api/hgstock/* (HG Stock upload)
│   │   ├── video_editor.py  ← /api/video-editor/*
│   │   ├── sync.py          ← /sync/* (extension bridge; XOR envelope)
│   │   └── analyzer.py, media_tools.py, tasks.py, files.py, settings.py, system.py
│   └── services/
│       ├── flow_client.py       ← Google Flow API client (ACTIVE; renew_token(bad_token=None))
│       ├── flow_client_bridge.py ← BridgeFlowClient (Extension Bridge)
│       ├── upscaler.py          ← realesrgan-ncnn-vulkan subprocess
│       ├── ffmpeg_utils.py      ← ffmpeg + subprocess_no_window_kwargs()
│       ├── watermark_video.py   ← Video WM removal pipeline
│       ├── feature_installer.py ← Feature store (download + install addons)
│       ├── updater.py, oauth_auth.py, error_messages.py
│       └── browser_manager.py, gemini.py, image_utils.py, lama_inpaint.py, setup_wizard.py
│
├── extension/               ← Chrome MV3 "RedOne Auth Helper" (v1.5.3)
│   ├── manifest.json        ← host_permissions: labs.google + shakker.ai + accounts.google.com
│   ├── background.js        ← _pollLoop, heartbeat, XOR codec, SHAKKER_SYNC, targetGoogleEmail
│   ├── content.js, content_accounts.js, content_shakker.js
│   └── popup.html / popup.js
│
├── chrome-ext/
│   └── update.xml           ← CRX auto-update feed (v1.5.3)
│
├── photoshop-plugin/        ← CEP Extension "RedOne GenFill" (STANDALONE — độc lập tool)
│   ├── index.html, CSXS/manifest.xml (ExtensionBundleId: com.redone.genfill)
│   └── js/
│       ├── main.js          ← Init, embedded server, gen flow
│       ├── embedded_server.js ← Node.js HTTP port 8099, protocol /sync/*
│       ├── flow_api.js      ← Google Flow calls qua extension
│       └── CSInterface.js
│
├── frontend/
│   ├── index.html           ← SPA shell: sidebar + topbar
│   └── js/
│       ├── app.js, api.js, ui.js (lightbox nav), ws.js, tasks_store.js
│       ├── gallery_actions.js ← makeSelectionToolbar (wm, I2V, upscale-video, regen)
│       └── pages/
│           ├── content.js       ← Tạo Video + WM (_patchWmChipsInPlace anti-flicker)
│           ├── image.js         ← Tạo Ảnh (sendImagesToI2V ưu tiên upscale_path)
│           ├── video_upscale.js ← Upscale Video AI (nút hủy per-batch)
│           ├── storyboard.js, video_watermark.js, hgstock.js
│           └── accounts.js, settings.js, tasks_manager.js, ...
│
├── features/index.json      ← Remote feature catalog
├── addons/                  ← (gitignore) Downloaded feature bundles
├── data/                    ← (gitignore) db, auth_session, logs
├── outputs/                 ← (gitignore) Generated files
├── install_ps_plugin.bat    ← Cài CEP plugin (xcopy, run as Admin)
└── PROJECT_CONTEXT.md · SESSION_HANDOFF.md · BUILD_RELEASE.md · README.md
```

---

## 4. Sidebar nav (v1.5.x)

```
SÁNG TẠO:   Tạo Video · Tạo Ảnh · Ảnh Shakker · Video Dài · Storyboard
PHÂN TÍCH:  YouTube→Prompt · Ý Tưởng→Video · Ảnh→Prompt
XỬ LÝ MEDIA: Upscale Ảnh · Resize Hàng Loạt · Chỉnh Màu Hàng Loạt · Xóa Watermark Video · Upscale Video (AI) · Phụ Đề
HỆ THỐNG:   Quản lý Task · Tài Khoản · HG Stock · Cài Đặt
```

---

## 5. Auth / OAuth gate

- Middleware `main.py` chặn `/api/*` + SPA root nếu chưa có session hợp lệ.
- Cho qua không cần auth: `/auth/*`, `/static/*`, `/sync/*`, `/login.html`, `/favicon.ico`.
- `private_config.py` (gitignored): OAuth client id/secret, ALLOWED_EMAIL_DOMAIN.
- Nếu chưa setup → gate BỎ QUA (admin vào đọc hướng dẫn được).
- 1 mail @redone.vn login được nhiều máy đồng thời (mỗi máy 1 session file).

---

## 6. Generation: CHỈ Chrome Extension Bridge

- `BridgeFlowClient` (`flow_client_bridge.py`) gọi Flow qua Chrome extension thật.
- `flow_client.py` cũng ACTIVE — refactor `renew_token(bad_token=None)`: chỉ renew khi token HIỆN TẠI khớp bad_token (tránh renew không cần thiết).
- Extension poll `BRIDGE_HOSTS = [8000, 8001, 8099, 8098]` — lấy host đầu tiên phản hồi rồi `break`.
  - **Khi RedOne tool (8000) đang chạy**: extension KHÔNG poll 8099 (PS GenFill).
  - **Test PS GenFill độc lập**: tắt tool (port 8000) trước.

---

## 7. Upscale Video AI

- **Endpoint**: `POST /api/content/upscale-start`, `GET /api/content/upscale-status/{batch_id}`, `POST /api/content/upscale-cancel/{batch_id}`.
- **State** (trong `content.py`):
  ```python
  _cancelled_batches: set[str] = set()   # PHẢI là set(), không phải {}
  _active_upscale_procs: dict[str, asyncio.subprocess.Process] = {}
  _upscale_progress: dict[str, dict] = {}
  ```
- **Cancel flow**: POST cancel → `_cancelled_batches.add(batch_id)` + `proc.kill()` → broadcast `video_upscale_batch_done` với `cancelled: true`.
- **Frontend** (`video_upscale.js`): batch header có nút "Hủy" khi `!task.completed && task.stage !== 'cancelled'`. `onBatchDone` xử lý `d.cancelled` riêng.
- **gallery_actions.js**: `onUpscaleVideo` ưu tiên `wm_path` (bản đã xóa WM) thay vì `output_path`.

---

## 8. Video Watermark Removal

- Pipeline: ffmpeg → lama/opencv inpaint → ffmpeg merge.
- `content.js` dùng `_patchWmChipsInPlace` (cập nhật chip in-place, KHÔNG re-render DOM) → hết chớp giật.

---

## 9. Gallery Lightbox Navigation (v1.5.3)

- `ui.js`: click ảnh/video → lightbox. Có nút **‹ / ›** cố định 2 bên viewport (glassmorphism).
- Phím ← → để điều hướng qua ảnh/video trong cùng task.
- Hoạt động ở: Tạo Ảnh, Tạo Video, Storyboard, Shakker.

---

## 10. Photoshop GenFill Plugin (STANDALONE)

- **CEP extension** `com.redone.genfill` — cài vào `C:\Program Files (x86)\Common Files\Adobe\CEP\extensions\`.
- `install_ps_plugin.bat`: xcopy (không symlink) → độc lập sau cài. Cần chạy As Administrator.
- **Embedded bridge server** (`embedded_server.js`): Node.js, port 8099, protocol `/sync/*` giống extension.
- **Phát hành**: `RedOne-GenFill-Plugin.zip` gồm `install_ps_plugin.bat` + `photoshop-plugin/` + `extension/`.
- **Lỗi `ERR_FILE_NOT_FOUND`**: thường do cài symlink không đúng → cài lại bằng script mới (xcopy).

---

## 11. Shakker.ai

- Engine SD/FLUX.1, tính bằng **power**. Auth: cookie `usertoken` (44-char hex).
- `ShakkerClient`: list_models/loras (param `query`), submit_generate (retry transient), upload_reference (oss2 → Alibaba OSS `models-online-persist-us`).
- LoRA: `additionalNetwork.append({modelId: versionId, weight, type: 0})`.

---

## 12. Bảo mật (RÀNG BUỘC)

- **KHÔNG commit secret**. Gitignored: `backend/private_config.py`, `docs/shakker_capture/`, `*.har`, `data/`, `outputs/`, `*.pem`, `*.crx`, `addons/`.
- **KHÔNG push vội** — chỉ commit+push khi user nói rõ "OK push".
- **Ko push các thứ liên quan G-labs** — `flow_client.py`, `flow_client_bridge.py` giữ local khi có thay đổi nhạy cảm.
- Download URLs restricted to GitHub allowlist (`FEATURES_ALLOWED_HOSTS`).
- Zip extraction guards against path traversal. 600MB size cap.

---

## 13. WebSocket events

| Event | Purpose |
|---|---|
| `item_completed` | Per-item done (output_path, upscale_path, upscale_url) |
| `video_upscale_progress` | Batch upscale tiến trình % |
| `video_upscale_batch_done` | Batch done (có `cancelled: true` nếu bị hủy) |
| `watermark_progress/_completed/_error` | Video WM removal |
| `account_updated` | Credit/status thay đổi |
| `queue_updated` | Queue snapshot |
| `update_progress` | Auto-updater |

---

## 14. Gửi ảnh sang I2V

`sendImagesToI2V` (`image.js` dòng 88):
- `upscale_status === 'done'` → gửi `upscale_path`/`upscale_url`.
- Ngược lại → gửi `output_path` (gốc).
- Toast ghi rõ số ảnh upscale được dùng.

---

## 15. Version history

| Version | Tóm tắt |
|---|---|
| v1.0.x | Web app cơ bản: Veo 3.1 + Nano Banana, Playwright |
| v1.1.x | Chrome Extension Bridge, OAuth gate @redone.vn |
| v1.2.x | Shakker.ai, Queue song song, gỡ Vertex AI/Playwright |
| v1.3.x | Multi-user, long video nâng cao |
| v1.4.x | Storyboard, video editor, shakker mở rộng |
| v1.5.0 | Upscale Video AI, HG Stock, PS GenFill plugin |
| v1.5.1 | Audio merge nâng cao, gallery improvements |
| v1.5.2 | `renew_token(bad_token)`, extension targetGoogleEmail sync |
| **v1.5.3** | Lightbox nav, fix WM flicker, prefer wm_path, cancel upscale, fix `set()` |

---

## 16. Commands cheatsheet

```bash
cd "D:\RedOne Creative tool"
run.bat                      # dev run (uvicorn 127.0.0.1:8000)
build.bat                    # build EXE
curl http://127.0.0.1:8000/api/health
git status && git log --oneline -10
```

---

## 17. Conventions

- Tiếng Việt cho UI text; English cho code comments/identifiers.
- Per-page module: `export function renderXxx(root)` + cleanup qua MutationObserver.
- Backend đổi Python → phải restart `run.bat`. JS/HTML → hard-refresh.
- Hàm < 50 dòng, file < 800 dòng. Dùng `logging`, không `print()`.
- `async def` cho FastAPI I/O endpoints. `asyncio.to_thread()` cho blocking ops.

---

## 18. Khi mở session mới — đọc theo thứ tự

1. `git status && git log --oneline -5` → tình trạng repo.
2. File này — section 0 + 6/7.
3. `backend/config.py` (APP_VERSION, constants).
4. `backend/main.py` (auth gate, router includes).
5. File liên quan tính năng đang làm.

> ⚠️ `flow_client.py` / `flow_client_bridge.py` đang chạy ổn — không refactor lớn.

---

**END OF CONTEXT** — update khi có thay đổi lớn về kiến trúc hoặc version bump.
