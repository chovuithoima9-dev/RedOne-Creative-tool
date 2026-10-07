# RedOne Creative

Local desktop AI tool (nội bộ RedOne) — tạo ảnh / video bằng **Google Veo 3.1 + Nano Banana**.
Đăng nhập bằng email **@redone.vn**, mặc định chạy **Vertex AI** (credentials nhúng sẵn — dùng ngay, không cần setup); tùy chọn **Chrome Extension Bridge** để xài quota miễn phí Google Labs Flow.

## 📚 Tài liệu

- **🚀 [Hướng dẫn sử dụng đầy đủ](docs/HUONG_DAN_SU_DUNG.md)** — cho người dùng cuối: cài đặt → đăng nhập @redone.vn → tạo ảnh/video → gen lại lỗi → quản lý task
- **🔨 [Build & Release guide](BUILD_RELEASE.md)** — cho dev: PyInstaller, GitHub Release workflow
- **📥 [Tải bản EXE mới nhất](https://github.com/chovuithoima9-dev/RedOne-Creative-tool/releases)** — Windows 10/11

## Cài đặt nhanh

```bash
# 1. Cài Python ≥ 3.10
# 2. Cài deps
pip install -r requirements.txt

# 3. Cài Chromium cho Playwright (chỉ cần 1 lần)
playwright install chromium

# 4. Chạy
python -m backend.main
# hoặc double-click run.bat trên Windows
```

Mở trình duyệt: <http://127.0.0.1:8000>

## Cấu trúc

```
RedOne Creative tool/
├── backend/
│   ├── main.py              FastAPI app + /ws WebSocket
│   ├── config.py            Constants, model maps
│   ├── database.py          SQLite ORM
│   ├── ws_hub.py            Broadcast hub
│   ├── routers/
│   │   ├── accounts.py      Google account CRUD + check + login
│   │   ├── content.py       T2V / I2V batch render
│   │   ├── long_video.py    Multi-scene + extend + concat
│   │   ├── analyzer.py      YouTube + Script + Image → Prompt
│   │   ├── media_tools.py   BG remove / watermark / upscale / audio / subtitle / resize
│   │   └── settings.py      API keys, output, logs
│   └── services/
│       ├── flow_client.py       Google Veo API client
│       ├── browser_manager.py   Playwright + real Chrome
│       ├── google_auth.py       Login flow
│       ├── gemini.py            Gemini API + fallback chain
│       ├── ffmpeg_utils.py      Concat, audio merge, subtitle burn
│       └── image_utils.py       Pillow resize, bg fill
├── frontend/
│   ├── index.html           SPA shell với sidebar
│   ├── css/
│   │   ├── theme.css        Design tokens (dark, glassmorphism)
│   │   ├── layout.css       Sidebar + topbar + grid
│   │   ├── components.css   Buttons, cards, inputs, toasts, modals
│   │   └── pages.css        Page-specific styles
│   └── js/
│       ├── app.js           Router + state + WS wiring
│       ├── api.js           Fetch wrapper
│       ├── ws.js            WebSocket client with reconnect
│       ├── ui.js            el(), toast(), modal(), icon(), etc.
│       └── pages/           13 page modules (1 per nav item)
├── data/                    SQLite + cookies + logs
├── outputs/                 Generated videos, images, SRT
├── requirements.txt
├── run.bat
└── README.md
```

## Tính năng

| Trang | Endpoint | Backend |
|---|---|---|
| **Tạo Video** (T2V / I2V) | POST /api/content/start | Google Veo qua Playwright |
| **Video Dài** (multi-scene) | POST /api/long-video/start | Veo extend chain + FFmpeg concat |
| **YouTube → Prompt** | POST /api/analyzer/youtube | yt-dlp + Gemini Vision |
| **Ý Tưởng → Video** | POST /api/analyzer/script | Gemini one-shot storyboard |
| **Ảnh → Prompt** | POST /api/analyzer/image-to-prompt | Gemini Vision |
| **Upscale** | POST /api/media/upscale | PIL LANCZOS (Real-ESRGAN optional) |
| **Resize Hàng Loạt** | POST /api/media/batch-resize | Pillow + presets platform |
| **Phụ Đề** | POST /api/media/subtitle | OpenAI Whisper (offline) |
| **Tài Khoản** | /api/accounts/* | SQLite + Playwright login |
| **Cài Đặt** | /api/settings | KV store + logs viewer |

## Phụ thuộc tùy chọn (cài thêm khi cần)

Tính năng AI nặng không bắt buộc — chỉ install khi muốn dùng:

```bash
pip install openai-whisper     # Sinh phụ đề
pip install opencv-python      # Xóa watermark (basic)
pip install torch spandrel     # Xóa watermark bằng LaMa (chất lượng cao)
```

## Kiến trúc & ghi chú

- **Backend**: FastAPI + uvicorn, port mặc định 8000.
- **WebSocket** `/ws`: broadcast realtime cho task progress (item_completed, scene_done, task_error...).
- **Frontend**: SPA thuần ES Modules, không bundler/build step. F5 vẫn giữ tab nhờ hash routing.
- **Theme**: Dark glassmorphism với gradient cyan/purple, sidebar fixed 280px.
- **Account model**: 1 SQLite row mỗi account, cookies lưu ở `data/cookies/{id}_cookies.json` (Cookie-Editor JSON format).
- **Generated files**: `outputs/video/`, `outputs/image/`, `outputs/subtitle/`.

## Roadmap chưa làm

- TTS giọng Việt (cần cài thư viện voice clone riêng)
- Voice activity → TTS pipeline cho YouTube/Script narration
- Real-ESRGAN AI upscaling (đã có hook, cần model + spandrel)
- Per-account browser context pool cho tải song song nhiều account
- LaMa watermark removal (hook sẵn, cần download big-lama.pt)
