"""System info + update-check + auto-installer endpoints."""
from __future__ import annotations
import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..config import APP_NAME, APP_VERSION, GITHUB_REPO, IS_FROZEN, OUTPUT_DIR, DATA_DIR, FEEDBACK_FORM_URL
from ..services.updater import (
    check_for_update,
    download_update,
    extract_update,
    apply_update_and_exit,
    get_update_state,
    _UPDATE_LOCK,
)
from ..ws_hub import hub

log = logging.getLogger("redone.system")
router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
async def info():
    """Static app info."""
    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "frozen": IS_FROZEN,
        "github_repo": GITHUB_REPO,
        "github_url": f"https://github.com/{GITHUB_REPO}",
        "feedback_url": FEEDBACK_FORM_URL,
        "data_dir": str(DATA_DIR),
        "output_dir": str(OUTPUT_DIR),
        "can_auto_install": IS_FROZEN,
    }


@router.get("/check-update")
async def check_update(force: bool = False):
    """Check GitHub releases for a newer version. Cached 5 min."""
    return await check_for_update(force=force)


@router.get("/update-state")
async def update_state():
    """Snapshot of the in-progress (or last completed) update.

    Useful after a page reload mid-download: frontend can call this to
    reattach to an ongoing job and resume showing the progress bar without
    waiting for the next WS tick.
    """
    return get_update_state()


@router.get("/bridge-status")
async def bridge_status():
    """Detailed live status of the Chrome extension bridge and active Flow tab."""
    import time
    from ..services.browser_bridge import bridge
    return {
        "status": bridge._ext_last_status,
        "url": bridge._ext_last_url,
        "email": bridge.get_active_account_email(),
        "tier": bridge.get_active_account_tier(),
        "credits": bridge.get_active_account_credits(),
        "active_project_id": bridge.get_active_project_id(),
        "last_poll_age_s": time.time() - bridge._ext_last_poll,
        "is_live": bridge.is_extension_live(),
        "has_ready_tab": bridge._ext_last_status == "ready",
    }


@router.get("/debug-tab")
async def debug_tab():
    """Inspect the live DOM, title, user, and WIZ data of the active Flow tab."""
    from ..services.browser_bridge import bridge
    return await bridge.batch_execute("DEBUG_DOM", {})


@router.get("/test-debug-dom")
async def test_debug_dom():
    """Test DEBUG_DOM to verify extension version and tab state."""
    from ..services.browser_bridge import bridge
    return await bridge.batch_execute("DEBUG_DOM", {})


@router.get("/test-recaptcha")
async def test_recaptcha(action: str = "IMAGE_GENERATION"):
    """Test harvesting reCAPTCHA token from active tab."""
    from ..services.flow.captcha_worker import captcha_provider
    token = await captcha_provider.get_token(action=action)
    return {
        "action": action,
        "token_len": len(token) if token else 0,
        "token_prefix": token[:30] if token else None,
        "token_suffix": token[-30:] if token else None,
        "token": token,
    }


@router.get("/active-cookies")
async def active_cookies():
    """Retrieve full live cookie list from Chrome bridge."""
    from ..services.browser_bridge import bridge
    res = await bridge.get_cookies(domains=["flow.google.com", ".flow.google.com", ".google.com", "google.com"])
    return res


@router.get("/reload-extension")
async def reload_extension():
    """Instruct the Chrome extension to reload its background worker."""
    from ..services.browser_bridge import bridge
    bridge.push_session_command("reload_extension")
    return {"status": "command_pushed"}


@router.get("/reload-tab")
async def reload_tab():
    """Instruct the Chrome extension to reload the Flow tab."""
    from ..services.browser_bridge import bridge
    bridge.push_session_command("reload_tab")
    return {"status": "command_pushed"}



@router.get("/test-direct-gen")
async def test_direct_gen(project_id: Optional[str] = None):
    """Directly test ogiZ0b image generation with inline reCAPTCHA minting."""
    import uuid, random
    from ..services.browser_bridge import bridge

    pid = project_id or bridge.get_active_project_id() or "80f920a6-1a69-496c-94ab-ab3fddc2f621"
    if not pid:
        return {"error": "no active project in tab"}

    source_path = f"/project/{pid}"

    # Build payload with __MINT_RECAPTCHA__ placeholder — token will be
    #    minted INLINE by the extension inside the same executeScript call
    #    that sends the batchexecute request (prevents UNUSUAL_ACTIVITY).
    client_ctx = [
        None, 22, None, None, None,
        pid,
        None, None, None, None,
        ["__MINT_RECAPTCHA__", 1],
    ]
    prompt_arr = [[["a tranquil japanese garden with cherry blossoms, 8k"]]]

    # Google Flow UI always submits 4 candidates per generation batch
    candidates = []
    for _ in range(4):
        c_seed = random.randint(100000000, 2147483647)
        c_batch = str(uuid.uuid4()).upper()
        c_op = str(uuid.uuid4()).upper()
        candidates.append([
            None, None, None, c_seed, 3, "HARBOR_SEAL", None,
            client_ctx,
            prompt_arr,
            None, None, None,
            c_batch, c_op,
        ])

    inner_payload = [
        None,
        candidates,
        1,
        client_ctx,
        [str(uuid.uuid4()).upper()],
    ]

    res = await bridge.batch_execute(
        rpc_id="ogiZ0b",
        inner_payload=inner_payload,
        source_path=source_path,
        timeout_ms=120000,
        recaptcha_action="IMAGE_GENERATION",
    )
    return {
        "project_id": pid,
        "res_status": res.get("status"),
        "res_error": res.get("error"),
        "has_rpc_result": res.get("rpc_result") is not None,
        "rpc_result_preview": str(res.get("rpc_result"))[:300] if res.get("rpc_result") else None,
    }


@router.get("/test-direct-video-t2v")
async def test_direct_video_t2v(project_id: Optional[str] = None):
    """Directly test YhhmEf video generation with inline reCAPTCHA minting."""
    import uuid
    from ..services.browser_bridge import bridge

    pid = project_id or bridge.get_active_project_id() or "80f920a6-1a69-496c-94ab-ab3fddc2f621"
    client_ctx = [
        None, 22, None, None, None,
        pid,
        None, None, None, None,
        ["__MINT_RECAPTCHA__", 1],
    ]
    # 1. First call nzlxg (get credits / session warm)
    res_credits = await bridge.batch_execute(
        rpc_id="nzlxg",
        inner_payload=[],
        source_path=f"/project/{pid}",
        timeout_ms=30000,
    )

    # 2. Then call YhhmEf with veo_3_1_t2v_lite (exact match to update3.har)
    prompt_item = [None, None, [[["a tranquil drone shot of misty mountains, cinematic, 4k"]]]]
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()
    batch_uuid = str(uuid.uuid4()).upper()
    candidate = [
        prompt_item,
        "veo_3_1_t2v_lite",
        2,  # 16:9
        None,
        [None, None, None, None, uuid_a, uuid_b],
    ]
    inner_payload = [
        [candidate],
        client_ctx,
        [batch_uuid, 2],
    ]
    res = await bridge.batch_execute(
        rpc_id="YhhmEf",
        inner_payload=inner_payload,
        source_path=f"/project/{pid}",
        timeout_ms=120000,
        recaptcha_action="VIDEO_GENERATION",
    )
    return {
        "project_id": pid,
        "res_credits": res_credits.get("rpc_result"),
        "raw_res": res,
    }


@router.get("/test-glabs-gen")
async def test_glabs_gen():
    """Test G-Labs Studio architecture: Python direct POST with harvested reCAPTCHA token."""
    import uuid, random, json
    import httpx
    from ..services.browser_bridge import bridge

    pid = bridge.get_active_project_id()
    if not pid:
        return {"error": "no active project in tab"}

    import datetime
    now_str = datetime.datetime.now().strftime("%b %d - %H:%M")
    inner_create = ["projects/*", [None, [f"Flow Project ({now_str})"]], [None, 22]]
    
    email = bridge.get_active_account_email() or "user@gmail.com"
    from ..services.flow_session import FlowSession
    session = FlowSession(account_email=email)
    c_res1 = await bridge.get_cookies(domains=[".flow.google.com", "flow.google.com", ".google.com", "google.com"])
    cookies_list = c_res1.get("cookies", [])
    session.update_cookies_from_list(cookies_list)
    await session.bootstrap(force=True)

    create_res = await session.execute("jHPbke", inner_create, source_path="/")
    log.info(f"jHPbke create_res: {create_res}")
    rpc_res = create_res.get("rpc_result")
    if rpc_res and isinstance(rpc_res, list) and len(rpc_res) > 0 and isinstance(rpc_res[0], str):
        pid = rpc_res[0]
        log.info(f"Created fresh project via jHPbke: {pid}")
    else:
        pid = "d9ec99ab-368b-49e4-b641-e74719742745"
        log.info(f"Fallback to G-Labs project: {pid}")

    # 3. Harvest reCAPTCHA token from extension
    try:
        rc_token = await bridge.harvest_recaptcha(
            site_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
            action="IMAGE_GENERATION",
        )
    except Exception as e:
        return {"error": f"harvest_recaptcha failed: {e}"}

    if not rc_token:
        return {"error": "harvest_recaptcha returned empty token"}

    # 4. Build payload with 1 candidate matching G-Labs Studio
    client_ctx = [
        None, 22, None, None, None,
        pid,
        None, None, None, None,
        [rc_token, 1],
    ]
    prompt_arr = [[["con mèo"]]]

    batch_uuid = str(uuid.uuid4()).upper()
    op_uuid = str(uuid.uuid4()).upper()
    c_seed = random.randint(100000, 999999)
    candidates = [[
        None, None, None, c_seed, 3, "HARBOR_SEAL", None,
        client_ctx,
        prompt_arr,
        None, None, None,
        batch_uuid, op_uuid,
    ]]

    inner_payload = [
        None,
        candidates,
        1,
        client_ctx,
        [str(uuid.uuid4()).upper()],
    ]

    res = await session.execute("ogiZ0b", inner_payload, source_path="/")
    return {
        "project_id": pid,
        "res_status": res.get("status"),
        "res_error": res.get("error"),
        "has_rpc_result": res.get("rpc_result") is not None,
        "rpc_result_preview": str(res.get("rpc_result"))[:300] if res.get("rpc_result") else None,
        "chunks_count": len(res.get("chunks", [])),
    }


@router.get("/test-bootstrap-session")
async def test_bootstrap_session():
    """Test full G-Labs FlowSession: bootstrap CSRF from flow.google.com/about + execute ogiZ0b."""
    import uuid, random, json
    from ..services.browser_bridge import bridge
    from ..services.flow_session import FlowSession

    pid = bridge.get_active_project_id()
    if not pid:
        return {"error": "no active project in tab"}

    # 1. Harvest fresh reCAPTCHA token from tab
    rc_token = await bridge.harvest_recaptcha(
        site_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
        action="IMAGE_GENERATION",
    )
    if not rc_token:
        return {"error": "harvest_recaptcha returned empty"}

    # 2. Get cookies from browser
    c_res = await bridge.get_cookies(domains=["flow.google.com", ".flow.google.com", ".google.com", "google.com"])
    cookies = c_res.get("cookies", [])

    # 3. Create FlowSession and bootstrap
    email = bridge.get_active_account_email() or "user@gmail.com"
    session = FlowSession(account_email=email)
    session.update_cookies_from_list(cookies)
    try:
        await session.bootstrap(force=True)
    except Exception as e:
        return {"error": f"bootstrap failed: {e}"}

    # 4. Build payload with 4 candidates
    client_ctx = [
        None, 22, None, None, None,
        pid,
        None, None, None, None,
        [rc_token, 1],
    ]
    prompt_arr = [[["a tranquil japanese garden with cherry blossoms, 8k"]]]
    candidates = []
    batch_uuid = str(uuid.uuid4()).upper()
    op_uuid = str(uuid.uuid4()).upper()
    for i in range(4):
        c_seed = random.randint(100000000, 2147483647)
        c_batch = batch_uuid if i == 0 else str(uuid.uuid4()).upper()
        c_op = op_uuid if i == 0 else str(uuid.uuid4()).upper()
        candidates.append([
            None, None, None, c_seed, 3, "HARBOR_SEAL", None,
            client_ctx,
            prompt_arr,
            None, None, None,
            c_batch, c_op,
        ])

    inner_payload = [
        None,
        candidates,
        1,
        client_ctx,
        [str(uuid.uuid4()).upper()],
    ]

    source_path = f"/project/{pid}"
    res = await session.execute(
        rpc_id="ogiZ0b",
        inner_payload=inner_payload,
        source_path=source_path,
        timeout_ms=60000,
    )
    return {
        "bootstrap": {
            "auth_index": session.auth_index,
            "at": session.at_token[:20] + "..." if session.at_token else None,
            "bl": session.build_label,
            "sid": session.session_id,
        },
        "rc_token_len": len(rc_token),
        "result_status": res.get("status"),
        "result_error": res.get("error"),
        "has_rpc_result": res.get("rpc_result") is not None,
        "rpc_result_preview": str(res.get("rpc_result"))[:300] if res.get("rpc_result") else None,
    }


@router.get("/test-direct-video")
async def test_direct_video():
    """Directly test YhhmEf video generation."""
    import uuid
    from ..services.browser_bridge import bridge

    pid = bridge.get_active_project_id()
    if not pid:
        return {"error": "no active project in tab"}

    source_path = f"/project/{pid}"

    # 1. Harvest fresh reCAPTCHA token for VIDEO_GENERATION
    site_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    recaptcha_token = await bridge.harvest_recaptcha(site_key=site_key, action="VIDEO_GENERATION")
    if not recaptcha_token:
        return {"error": "failed to harvest recaptcha token"}

    client_ctx = [
        None, 22, None, None, None,
        pid,
        None, None, None, None,
        [recaptcha_token, 1],
    ]

    prompt_item = [None, None, [[["a playful kitten exploring a flower field, cinematic lighting"]]]]
    model_name = "veo_3_1_t2v_lite"
    uuid_a = str(uuid.uuid4()).upper()
    uuid_b = str(uuid.uuid4()).upper()
    candidate = [
        prompt_item,
        model_name,
        2,
        None,
        [None, None, None, None, uuid_a, uuid_b],
    ]

    batch_uuid = str(uuid.uuid4()).upper()
    inner_payload = [
        [candidate],
        client_ctx,
        [batch_uuid, 2],
    ]

    res = await bridge.batch_execute(
        rpc_id="YhhmEf",
        inner_payload=inner_payload,
        source_path=source_path,
        timeout_ms=120000,
    )
    return {
        "project_id": pid,
        "token_len": len(recaptcha_token),
        "res_status": res.get("status"),
        "res_error": res.get("error"),
        "has_rpc_result": res.get("rpc_result") is not None,
        "rpc_result_preview": str(res.get("rpc_result"))[:300] if res.get("rpc_result") else None,
    }


# ─── In-app updater ────────────────────────────────────────────────

async def _run_download_pipeline(download_url: str, asset_name: str | None,
                                 asset_size: int | None, version: str | None):
    """Background task: download → extract → broadcast progress.

    Holds the global _UPDATE_LOCK so only one update can run at a time.
    Broadcasts a single WS event type, `update_progress`, with the full
    state snapshot — frontend only needs one subscription.
    """
    async def _emit(state: dict) -> None:
        await hub.broadcast("update_progress", state)

    async with _UPDATE_LOCK:
        try:
            zip_path = await download_update(
                download_url, asset_name=asset_name,
                expected_size=asset_size, on_progress=_emit,
            )
            await extract_update(zip_path, version=version or "pending",
                                 on_progress=_emit)
            # Final emit so frontend's stage = "ready" reaches the WS even
            # without further activity.
            await _emit(get_update_state())
        except Exception as e:
            log.exception("Update pipeline crashed")
            from ..services.updater import _UPDATE_STATE
            _UPDATE_STATE["stage"] = "error"
            _UPDATE_STATE["error"] = str(e)
            _UPDATE_STATE["message"] = f"Lỗi: {e}"
            await _emit(get_update_state())


@router.post("/start-update")
async def start_update():
    """Kick off background download + extract. Returns immediately.

    The frontend listens to WS `update_progress` events for live progress.
    Refuses if (a) not running as a frozen EXE — dev mode can't self-replace,
    or (b) GitHub didn't expose a .zip asset on the release.
    """
    if not IS_FROZEN:
        raise HTTPException(
            400,
            "Tool đang chạy ở dev mode — auto-update chỉ áp dụng cho bản EXE "
            "đã đóng gói. Cập nhật bằng `git pull` thủ công.",
        )

    info = await check_for_update(force=True)
    if not info.get("update_available"):
        raise HTTPException(400, "Đang ở phiên bản mới nhất rồi")
    url = info.get("download_url")
    if not url:
        raise HTTPException(
            400,
            "Release mới không có file .zip — yêu cầu maintainer upload "
            "asset RedOne-Creative-vX.X.X-win64.zip vào release.",
        )

    # Lock-check: if a download is already running, just acknowledge so
    # the user can still see live progress instead of getting an error.
    state = get_update_state()
    if state["stage"] in ("downloading", "extracting"):
        return {
            "ok": True, "already_running": True, "state": state,
        }

    # Spawn background coroutine — don't await it. Frontend polls via WS.
    asyncio.create_task(_run_download_pipeline(
        url, info.get("asset_name"), info.get("asset_size"), info.get("latest"),
    ))
    return {"ok": True, "started": True, "version": info["latest"]}


# ─── First-run setup wizard ────────────────────────────────────────

async def _run_setup_pipeline():
    """Background task: run the first-run wizard, broadcasting progress
    via WS. Exists at module level so asyncio.create_task() in /setup-run
    doesn't tie its lifetime to the HTTP request handler."""
    from ..services.setup_wizard import run_setup

    async def _emit(state: dict) -> None:
        await hub.broadcast("setup_progress", state)

    try:
        await run_setup(on_progress=_emit)
    except Exception as e:
        log.warning(f"Setup pipeline crashed: {e}")
        # run_setup already updated state.error before re-raising


@router.get("/setup-status")
async def setup_status():
    """Return what the wizard needs to do + whether it's already done
    for this version. Frontend hits this on app load — if `all_ready` is
    False and the version marker doesn't match, show the wizard modal."""
    from ..services.setup_wizard import compute_needs, is_setup_complete_for_current_version
    needs = await compute_needs()
    needs["setup_complete_for_current_version"] = is_setup_complete_for_current_version()
    return needs


@router.get("/setup-state")
async def setup_state():
    """Live state snapshot — frontend reattaches to an in-progress
    wizard after a tab reload by reading this."""
    from ..services.setup_wizard import get_setup_state
    return get_setup_state()


@router.post("/setup-run")
async def setup_run():
    """Kick off the wizard pipeline. Rejects if one is already running."""
    from ..services.setup_wizard import get_setup_state
    state = get_setup_state()
    if state["stage"] == "running":
        raise HTTPException(409, "Setup đang chạy rồi")
    asyncio.create_task(_run_setup_pipeline())
    return {"ok": True, "started": True}


# ─── LaMa AI upgrade installer ─────────────────────────────────────

async def _run_lama_install_pipeline():
    """Background task: install LaMa deps + download model, broadcasting
    progress via WS. Lives at module level so the asyncio.create_task() in
    /lama-install doesn't tie its lifetime to the request handler."""
    from ..services.lama_installer import install_lama

    async def _emit(state: dict) -> None:
        await hub.broadcast("lama_install_progress", state)

    try:
        await install_lama(on_progress=_emit)
    except Exception as e:
        log.warning(f"LaMa install pipeline crashed: {e}")
        # _emit already fired by install_lama() before re-raising


@router.get("/lama-install-state")
async def lama_install_state():
    """Snapshot of the in-progress (or last completed) LaMa install.

    Frontend uses this on page mount to reattach to an ongoing install
    after a refresh — same pattern as /update-state for the auto-updater.
    """
    from ..services.lama_installer import get_install_state
    return get_install_state()


@router.post("/lama-install")
async def lama_install():
    """Kick off the LaMa upgrade pipeline as a background task.

    Returns immediately; frontend subscribes to WS `lama_install_progress`
    events for live progress. Rejects with 409 if an install is already
    running so the user doesn't accidentally start two concurrent pip
    invocations (they'd race on the same cache).
    """
    from ..services.lama_installer import get_install_state
    state = get_install_state()
    if state["stage"] in ("detecting", "installing_pip", "downloading_model"):
        raise HTTPException(409, "Install đang chạy rồi")
    asyncio.create_task(_run_lama_install_pipeline())
    return {"ok": True, "started": True}


@router.post("/shutdown")
async def shutdown():
    """Kill the server process. Called from Settings → "Tắt tool".

    Browser tabs only disconnect — they don't kill the backend. Without this
    endpoint, the EXE keeps running in the background after the user closes
    the last tab, hogging port 8000 + RAM. Calls os._exit(0) after a tiny
    delay so the HTTP response flushes first.
    """
    import os
    import asyncio
    log.info("Shutdown requested via /api/system/shutdown — exiting in 0.5s")
    await hub.broadcast("server_shutting_down", {})

    async def _bye():
        await asyncio.sleep(0.5)   # let response flush
        os._exit(0)
    asyncio.create_task(_bye())
    return {"ok": True, "message": "Đang tắt tool..."}


@router.post("/apply-update")
async def apply_update():
    """Swap to the staged build and restart. THIS PROCESS DIES.

    Must be called after a successful start-update + extract. Frontend
    detects stage="ready" before showing the "Install & restart" button.
    """
    state = get_update_state()
    if state["stage"] != "ready" or not state.get("extracted_dir"):
        raise HTTPException(
            400,
            f"Chưa có bản update sẵn sàng (stage={state['stage']}). "
            "Bấm 'Tải xuống' trước đã.",
        )

    extracted = Path(state["extracted_dir"])
    if not extracted.exists():
        raise HTTPException(500, f"Thư mục extract đã biến mất: {extracted}")

    # Broadcast one last event so the frontend can show "Đang cài…"
    await hub.broadcast("update_progress", {
        **state, "stage": "installing", "message": "Đang khởi động installer…",
    })

    # Schedule the death after returning the HTTP response. We can't call
    # apply_update_and_exit() synchronously here — uvicorn would never
    # finish sending the response. Use an asyncio task with a tiny delay.
    async def _delayed_exit():
        await asyncio.sleep(0.5)   # let response flush
        try:
            apply_update_and_exit(extracted)
        except Exception as e:
            log.exception("apply_update_and_exit failed")
            await hub.broadcast("update_progress", {
                "stage": "error", "message": str(e),
                "error": str(e), "percent": 0.0,
            })
    asyncio.create_task(_delayed_exit())

    return {"ok": True, "scheduled": True}

