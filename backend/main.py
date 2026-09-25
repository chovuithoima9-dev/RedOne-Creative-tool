"""RedOne Creative — FastAPI entry point."""
from __future__ import annotations
import logging
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

# Unique build id per server start — used to bust ES module caches.
BUILD_ID = f"{int(time.time())}-{secrets.token_hex(4)}"

from .config import APP_NAME, APP_VERSION, OUTPUT_DIR, DATA_DIR, BASE_DIR, SERVER_PORT, EXT_DIR
from .database import db
from .ws_hub import hub
from .routers import (
    accounts, content, image as image_router, analyzer, long_video,
    media_tools, settings as settings_router, files as files_router,
    tasks as tasks_router, system as system_router,
    sync as sync_router, auth as auth_router,
    shakker_accounts as shakker_accounts_router,
    shakker as shakker_router,
    storyboard as storyboard_router,
    hub as hub_router,
    features as features_router,
    video_editor as video_editor_router,
    batch_color as batch_color_router,
    hgstock as hgstock_router,
    ps_genfill as ps_genfill_router,
    tracking as tracking_router,
)
from .queue_manager import queue as task_queue, shakker_queue

# ── Logging ──────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log_file = DATA_DIR / "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("redone")


class _AsyncioNoise(logging.Filter):
    """Silence benign Windows-proactor noise that doesn't affect downloads.

    `WinError 10054` (ConnectionResetError) fires from
    `_ProactorBasePipeTransport._call_connection_lost` AFTER our HTTP/Playwright
    response is already read — Google's CDN just closes the socket before our
    graceful shutdown. Harmless, but spams the log.
    """
    def filter(self, record):  # noqa: A003
        msg = record.getMessage()
        if "_ProactorBasePipeTransport._call_connection_lost" in msg:
            return False
        if "WinError 10054" in msg or "ConnectionResetError" in msg:
            return False
        return True


logging.getLogger("asyncio").addFilter(_AsyncioNoise())


def _silent_proactor_handler(loop, context):
    """Drop benign ProactorBasePipeTransport ConnectionResetError noise."""
    exc = context.get("exception")
    msg = context.get("message", "")
    if isinstance(exc, ConnectionResetError):
        return
    if "_ProactorBasePipeTransport._call_connection_lost" in msg:
        return
    if "_ProactorBaseWritePipeTransport._call_connection_lost" in msg:
        return
    # Anything else: default behavior
    loop.default_exception_handler(context)


def recover_interrupted_tasks():
    """On startup the in-memory queue is empty, so any task left RUNNING/PENDING
    from a previous run is orphaned (crash / update / disconnect). Mark them
    PAUSED and reset half-done items to PENDING so the user can Resume instead
    of recreating the task."""
    try:
        from .database import db
        from .config import TaskStatus, ItemStatus
        interrupted_item = {
            ItemStatus.GENERATING.value, ItemStatus.UPLOADING.value,
            ItemStatus.DOWNLOADING.value,
        }
        n = 0
        for t in db.list_tasks(limit=1000):
            if t.get("status") not in (TaskStatus.RUNNING.value, TaskStatus.PENDING.value):
                continue
            done = 0
            for it in db.get_task_items(t["id"]):
                if it["status"] == ItemStatus.COMPLETED.value:
                    done += 1
                elif it["status"] in interrupted_item:
                    db.update_item(it["id"], status=ItemStatus.PENDING.value, error_message=None)
            db.update_task(t["id"], status=TaskStatus.PAUSED.value, done_count=done, finished_at=None)
            n += 1
        if n:
            log.info(f"Recovered {n} interrupted task(s) -> PAUSED (resumable)")
    except Exception as e:
        log.warning(f"recover_interrupted_tasks failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"=== {APP_NAME} v{APP_VERSION} starting ===")
    # Quiet down the Windows proactor shutdown noise
    import asyncio as _asyncio
    try:
        _asyncio.get_running_loop().set_exception_handler(_silent_proactor_handler)
    except Exception:
        pass
    try:
        files_router.cleanup_pending_folder(max_age_hours=168)  # giữ file _pending 1 tuần (trước: 24h)
    except Exception as e:
        log.warning(f"cleanup_pending_folder failed: {e}")
    task_queue.start()
    shakker_queue.start()   # independent lane → runs concurrently with Flow
    recover_interrupted_tasks()

    # ── Firebase session-time heartbeat ──────────────────────────────
    # Background task sends a heartbeat every 60s while the tool is open,
    # so Firestore accumulates accurate session-time per user.
    _heartbeat_task = None
    try:
        from .services import tracking as _tracking
        from .config import FIREBASE_HEARTBEAT_INTERVAL_S
        if _tracking.is_enabled():
            async def _heartbeat_loop():
                while True:
                    await _asyncio.sleep(FIREBASE_HEARTBEAT_INTERVAL_S)
                    try:
                        from .services.oauth_auth import load_session as _ls
                        sess = _ls()
                        if sess:
                            await _tracking.heartbeat(
                                sess.get("email", ""),
                                display_name=sess.get("name", ""),
                            )
                    except Exception:
                        pass
            _heartbeat_task = _asyncio.create_task(_heartbeat_loop())
            log.info("Firebase tracking heartbeat started (interval=%ds)",
                     FIREBASE_HEARTBEAT_INTERVAL_S)
    except Exception as e:
        log.debug("Firebase heartbeat setup skipped: %s", e)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    if _heartbeat_task:
        _heartbeat_task.cancel()
    task_queue.stop()
    shakker_queue.stop()
    log.info(f"=== {APP_NAME} shutting down ===")


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
# CORS locked to the tool's own localhost origin. The SPA is served same-origin
# (needs no CORS) and the Chrome extension bypasses CORS via host_permissions,
# so a strict allowlist breaks nothing — but it stops ANY other website the user
# visits from making credentialed reads against this local server, which would
# otherwise expose /sync/* (incl. the shared Google password) + authenticated
# /api/* responses. See security review C1.
_CORS_ORIGINS = [f"http://127.0.0.1:{SERVER_PORT}", f"http://localhost:{SERVER_PORT}"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def no_cache_for_static(request: Request, call_next):
    """Disable browser caching for /static and the root HTML — this is a local
    dev tool, so we want every change to be picked up on refresh without
    needing manual version bumps."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static") or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ── Auth gate ─────────────────────────────────────────────────────────
# Blocks /api/* requests + the SPA root from unauthenticated callers.
# Allowed without auth:
#   /auth/*       OAuth flow handlers
#   /login.html   the login page itself
#   /static/*     CSS/JS/images the login page needs to render
#   /sync/*       extension <-> bridge protocol (no user identity)
#   /favicon.ico
# Everything else 401s (for /api/*) or redirects to /login.html (for HTML).
# When OAuth is unconfigured (no private_config.py), the gate is BYPASSED
# so an admin can still reach the tool to read setup instructions.

_AUTH_ALLOW_PREFIXES = (
    "/auth/", "/static/", "/sync/", "/login.html",
    "/favicon.ico", "/css/", "/js/",
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a valid @{ALLOWED_DOMAIN} OAuth session for everything
    except auth endpoints + static assets. See module docstring."""
    from .services.oauth_auth import load_session, is_configured

    # Always allow auth flow + static assets + the bridge protocol
    path = request.url.path
    for prefix in _AUTH_ALLOW_PREFIXES:
        if path == prefix or path.startswith(prefix):
            return await call_next(request)

    # If admin hasn't set up OAuth yet (no private_config.py with creds),
    # let everything through so they can still read setup docs in the UI.
    # Once configured, the gate engages.
    if not is_configured():
        return await call_next(request)

    if load_session() is not None:
        return await call_next(request)

    # Unauthenticated. API calls get a clean 401; HTML navigation gets
    # a redirect to the login page.
    if path.startswith("/api/") or path.startswith("/ws"):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"error": "unauthorized", "message": "Cần đăng nhập trước khi dùng tool"},
            status_code=401,
        )
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/login.html", status_code=302)

app.include_router(accounts.router)
app.include_router(shakker_accounts_router.router)
app.include_router(shakker_router.router)
app.include_router(content.router)
app.include_router(image_router.router)
app.include_router(analyzer.router)
app.include_router(storyboard_router.router)
app.include_router(long_video.router)
app.include_router(media_tools.router)
app.include_router(settings_router.router)
app.include_router(files_router.router)
app.include_router(tasks_router.router)
app.include_router(system_router.router)
app.include_router(sync_router.router)
app.include_router(auth_router.router)
app.include_router(hub_router.router)
app.include_router(features_router.router)
app.include_router(video_editor_router.router)
app.include_router(batch_color_router.router)
app.include_router(hgstock_router.router)
app.include_router(ps_genfill_router.router)
app.include_router(tracking_router.router)


@app.get("/api/health")
async def health():
    return {"ok": True, "app": APP_NAME, "version": APP_VERSION}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


# Static file serving for generated outputs
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")

# Kho tính năng: downloaded "frontend" feature bundles live under EXT_DIR and
# are imported by the SPA as /addons/<id>/<entry>.js. Mounted BEFORE the
# catch-all SPA route so it takes precedence.
app.mount("/addons", StaticFiles(directory=str(EXT_DIR)), name="addons")

# Frontend
FRONTEND_DIR = BASE_DIR / "frontend"
if FRONTEND_DIR.exists():
    import re as _re

    # Regex to rewrite import statements in served JS files. Captures both:
    #   - static:  import { x } from './foo.js';   import './bar.js';
    #   - dynamic: import('./baz.js')
    # The path must be relative (starts with ./ or ../). We append ?b=BUILD_ID
    # so the browser sees a fresh URL when the server restarts. All imports
    # use the SAME bust value → all modules resolve to the SAME registry entry
    # (single instance), critical for shared state like tasks_store.
    _IMPORT_RE = _re.compile(
        r"""(\bfrom\s+|\bimport\s*\(\s*|\bimport\s+)        # leading keyword
            (['"])                                           # opening quote
            (\.{1,2}/[^'"?]+\.js)                            # relative .js path
            (\2)                                             # closing quote
        """,
        _re.VERBOSE,
    )

    def _rewrite_js(text: str) -> str:
        def repl(m):
            return f"{m.group(1)}{m.group(2)}{m.group(3)}?b={BUILD_ID}{m.group(4)}"
        return _IMPORT_RE.sub(repl, text)

    def _serve_js(target: Path) -> Response:
        try:
            text = target.read_text(encoding="utf-8")
        except Exception:
            return FileResponse(target)
        rewritten = _rewrite_js(text)
        return Response(rewritten, media_type="application/javascript")

    def _render_index() -> HTMLResponse:
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__BUILD_ID__", BUILD_ID)
        return HTMLResponse(html)

    @app.get("/")
    async def root():
        return _render_index()

    @app.get("/static/{path:path}")
    async def serve_static(path: str):
        target = FRONTEND_DIR / path
        if not target.is_file():
            return HTMLResponse("Not found", status_code=404)
        if target.suffix == ".js":
            return _serve_js(target)
        return FileResponse(target)

    @app.get("/{path:path}")
    async def spa(path: str):
        # Don't swallow /api/* — let FastAPI return a proper 404 instead of
        # silently matching the SPA fallback (which also causes 405s when
        # the user POSTs to a not-yet-loaded route — old server, no auto-
        # reload — because the GET fallback "claims" the path).
        if path.startswith("api/") or path == "api":
            return HTMLResponse("Not found", status_code=404)
        target = FRONTEND_DIR / path
        if target.is_file():
            if target.suffix == ".js":
                return _serve_js(target)
            return FileResponse(target)
        return _render_index()


def run():
    import uvicorn
    import sys as _sys
    if getattr(_sys, "frozen", False):
        # PyInstaller --windowed: sys.stdout/stderr are None or stub.
        # Disable uvicorn's ColourizedFormatter (calls stdout.isatty()) by
        # passing log_config=None so it inherits our basicConfig() above.
        # Also pass `app` object directly — string imports re-import which
        # may break in the bundled environment.
        uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT,
                    log_config=None, access_log=False)
    else:
        uvicorn.run("backend.main:app", host="127.0.0.1", port=SERVER_PORT,
                    reload=False, log_level="info")


if __name__ == "__main__":
    run()
