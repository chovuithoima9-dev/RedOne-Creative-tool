"""GitHub-based update checker + in-place auto-installer.

Two responsibilities:

  1. `check_for_update()` polls the releases API on demand (no background
     loop — frontend triggers it on app load). Compares the latest published
     tag against the bundled APP_VERSION.

  2. `download_update()` / `extract_update()` / `apply_update()` implement
     the in-app updater: stream-download the release zip → extract to a
     staging dir → write a Windows batch installer that swaps the install
     dir (EXE + _internal/) AFTER the running process dies, then relaunches.

User data (`data/`, `outputs/`, `console.log`) lives next to the EXE and is
NEVER touched by the batch — only the bundled `RedOne Creative.exe` and
`_internal/` directory are replaced. So accounts, generated videos, logs,
DB state all carry over cleanly.
"""
from __future__ import annotations
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from ..config import APP_VERSION, APP_NAME, GITHUB_REPO, IS_FROZEN, DATA_DIR

log = logging.getLogger("redone.updater")

_CACHE: dict = {"data": None, "ts": 0}
_CACHE_TTL_SECONDS = 300  # 5 min — avoid GitHub rate limits

# Single-flight: only one download/extract at a time across all clients
_UPDATE_LOCK = asyncio.Lock()
_UPDATE_STATE: dict = {
    "stage": "idle",       # idle | downloading | extracting | ready | installing | error
    "downloaded": 0,        # bytes
    "total": 0,             # bytes
    "percent": 0.0,
    "message": "",
    "version": None,
    "extracted_dir": None,   # str(Path) once extract done
    "error": None,
}

# Type alias: an async progress callback. Reused for both download and extract.
ProgressCB = Callable[[dict], Awaitable[None]]


def _parse_version(v: str) -> tuple[int, ...]:
    """Strip leading 'v', split on dots, return int tuple. Falls back to (0,)."""
    v = (v or "").lstrip("vV").strip()
    parts = re.split(r"[.\-+]", v)
    out: list[int] = []
    for p in parts:
        m = re.match(r"^(\d+)", p)
        if m:
            out.append(int(m.group(1)))
        else:
            break
    return tuple(out) or (0,)


def _is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


async def check_for_update(force: bool = False) -> dict:
    """Return {update_available, current, latest, release_url, download_url}."""
    import time
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"] < _CACHE_TTL_SECONDS):
        return _CACHE["data"]

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RedOne-Creative-Updater"}
    result = {
        "update_available": False,
        "current": APP_VERSION,
        "latest": None,
        "release_url": None,
        "download_url": None,
        "asset_name": None,
        "asset_size": None,
        "release_notes": None,
        "published_at": None,
        "error": None,
        "can_auto_install": IS_FROZEN,   # only frozen EXE can self-replace
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                result["error"] = "Chưa có release nào trên GitHub repo"
                _CACHE["data"] = result
                _CACHE["ts"] = now
                return result
            r.raise_for_status()
            data = r.json()

        tag = data.get("tag_name") or ""
        result["latest"] = tag.lstrip("vV")
        result["release_url"] = data.get("html_url")
        result["release_notes"] = (data.get("body") or "").strip()[:1500]
        result["published_at"] = data.get("published_at")

        # Prefer a .zip asset (auto-updater format). Falls back to .exe if zip
        # is missing — old releases. Asset naming convention for auto-update:
        #   RedOne-Creative-vX.X.X-win64.zip
        # The zip contains: RedOne Creative.exe + _internal/
        all_assets = data.get("assets", []) or []
        for asset in all_assets:
            name = (asset.get("name") or "").lower()
            if name.endswith(".zip"):
                result["download_url"] = asset.get("browser_download_url")
                result["asset_name"] = asset.get("name")
                result["asset_size"] = asset.get("size")
                break
        if not result["download_url"]:
            # Fall back to first .exe asset (manual install only)
            for asset in all_assets:
                if (asset.get("name") or "").lower().endswith(".exe"):
                    result["download_url"] = asset.get("browser_download_url")
                    result["asset_name"] = asset.get("name")
                    result["asset_size"] = asset.get("size")
                    break

        result["update_available"] = _is_newer(result["latest"], APP_VERSION)
        # Log + surface a warning if we found assets but none was a usable
        # format. Common cause: dev uploaded .rar instead of .zip — the
        # auto-updater can't extract .rar (no rarfile + unrar.exe bundled).
        if (
            result["update_available"]
            and not result["download_url"]
            and all_assets
        ):
            asset_names = [a.get("name", "?") for a in all_assets]
            log.warning(
                f"Update v{result['latest']} has assets but no usable "
                f"format. Need .zip (or .exe fallback). Found: {asset_names}. "
                f"Re-upload as .zip via PowerShell Compress-Archive."
            )
            result["error"] = (
                f"Release v{result['latest']} có asset nhưng không phải .zip "
                f"(tìm thấy: {', '.join(asset_names)}). "
                f"Auto-update chỉ nhận .zip — vào GitHub Release edit + "
                f"re-upload zip."
            )
        log.info(
            f"Update check: current={APP_VERSION} latest={result['latest']} "
            f"available={result['update_available']} asset={result['asset_name']}"
        )
    except Exception as e:
        log.warning(f"Update check failed: {e}")
        result["error"] = str(e)

    _CACHE["data"] = result
    _CACHE["ts"] = now
    return result


def get_update_state() -> dict:
    """Snapshot of the current in-progress (or last completed) update."""
    return dict(_UPDATE_STATE)


def _reset_state():
    _UPDATE_STATE.update({
        "stage": "idle", "downloaded": 0, "total": 0, "percent": 0.0,
        "message": "", "version": None, "extracted_dir": None, "error": None,
    })


def _updates_dir() -> Path:
    """Where to stage downloads/extracts. Lives under data/ so it persists
    across runs (in case the user closes the app mid-download)."""
    d = DATA_DIR / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _emit(cb: Optional[ProgressCB]) -> None:
    if cb:
        try:
            await cb(dict(_UPDATE_STATE))
        except Exception as e:
            log.warning(f"progress callback raised: {e}")


async def download_update(
    download_url: str,
    asset_name: Optional[str] = None,
    expected_size: Optional[int] = None,
    on_progress: Optional[ProgressCB] = None,
) -> Path:
    """Stream-download the release zip to data/updates/<asset_name>.

    Emits state updates roughly every 256KB so the WS isn't flooded.
    """
    fname = asset_name or "update.zip"
    target = _updates_dir() / fname
    tmp = target.with_suffix(target.suffix + ".part")

    _UPDATE_STATE.update({
        "stage": "downloading", "downloaded": 0,
        "total": expected_size or 0, "percent": 0.0,
        "message": f"Đang tải {fname}…",
    })
    await _emit(on_progress)

    chunk_size = 1 << 15        # 32KB
    emit_every = 1 << 18        # 256KB
    bytes_since_emit = 0

    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", download_url) as r:
            r.raise_for_status()
            total = expected_size or int(r.headers.get("Content-Length") or 0)
            _UPDATE_STATE["total"] = total
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes(chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    _UPDATE_STATE["downloaded"] += len(chunk)
                    bytes_since_emit += len(chunk)
                    if total > 0:
                        _UPDATE_STATE["percent"] = round(
                            _UPDATE_STATE["downloaded"] / total * 100, 1
                        )
                    if bytes_since_emit >= emit_every:
                        await _emit(on_progress)
                        bytes_since_emit = 0

    # Atomic-ish: rename .part → final
    if target.exists():
        target.unlink()
    tmp.rename(target)
    log.info(f"Download complete: {target} ({_UPDATE_STATE['downloaded']} bytes)")
    return target


async def extract_update(
    zip_path: Path,
    version: str,
    on_progress: Optional[ProgressCB] = None,
) -> Path:
    """Extract release zip → data/updates/<version>/extracted/.

    Returns the extracted directory. The contents should be:
        <extract_dir>/RedOne Creative.exe
        <extract_dir>/_internal/...
    OR
        <extract_dir>/RedOne Creative/RedOne Creative.exe   (if zip has
                                                            an outer folder)
    We auto-detect and return the dir that actually holds the .exe.
    """
    _UPDATE_STATE.update({
        "stage": "extracting", "percent": 0.0,
        "message": "Đang giải nén…",
    })
    await _emit(on_progress)

    out = _updates_dir() / version / "extracted"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    def _do_extract():
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            total = len(members) or 1
            for i, m in enumerate(members):
                zf.extract(m, out)
                # Update progress (not awaiting — sync function inside to_thread)
                _UPDATE_STATE["percent"] = round((i + 1) / total * 100, 1)
                _UPDATE_STATE["message"] = f"Giải nén {i + 1}/{total}"

    # Run sync extraction in a thread so we don't block the event loop
    await asyncio.to_thread(_do_extract)

    # Detect the inner folder layout
    exe_name = f"{APP_NAME}.exe"
    if (out / exe_name).exists():
        root = out
    else:
        # Look one level deep for the exe
        for sub in out.iterdir():
            if sub.is_dir() and (sub / exe_name).exists():
                root = sub
                break
        else:
            raise RuntimeError(
                f"Không thấy {exe_name} trong file zip. Layout không hợp lệ — "
                f"zip phải chứa RedOne Creative.exe + _internal/ ở root."
            )

    _UPDATE_STATE.update({
        "stage": "ready", "percent": 100.0,
        "message": "Sẵn sàng cài đặt",
        "extracted_dir": str(root),
    })
    await _emit(on_progress)
    log.info(f"Extracted to: {root}")
    return root


# ── Install batch script (Windows) ────────────────────────────────

_INSTALL_BAT_TEMPLATE = r"""@echo off
REM ─────────────────────────────────────────────────────────────
REM   RedOne Creative — in-place updater
REM   Generated by backend/services/updater.py — do not edit.
REM
REM   What this does:
REM     1. Wait for the running EXE to die (it's mid-exit)
REM     2. Replace RedOne Creative.exe and _internal\ in-place
REM     3. Leave data\ and outputs\ alone
REM     4. Relaunch the new EXE
REM     5. Self-delete
REM ─────────────────────────────────────────────────────────────
setlocal enableextensions
set "INSTALL_DIR={install_dir}"
set "EXTRACT_DIR={extract_dir}"
set "EXE_NAME={exe_name}"
set "LOG_FILE=%INSTALL_DIR%\data\update.log"

call :log "─── Update install started @ %DATE% %TIME% ───"

REM Wait for parent process to release file locks
timeout /t 3 /nobreak >nul

REM Up to ~30s of retries: file lock can linger on slow machines
set RETRY=0
:wait_loop
del "%INSTALL_DIR%\%EXE_NAME%" 2>nul
if exist "%INSTALL_DIR%\%EXE_NAME%" (
    set /a RETRY+=1
    if %RETRY% GEQ 15 (
        call :log "[FAIL] EXE still locked after 30s — abort"
        goto :error
    )
    timeout /t 2 /nobreak >nul
    goto :wait_loop
)

REM Drop old _internal — rebuilt fresh from extract
if exist "%INSTALL_DIR%\_internal" rmdir /S /Q "%INSTALL_DIR%\_internal"
if exist "%INSTALL_DIR%\_internal" (
    call :log "[FAIL] could not remove _internal"
    goto :error
)

REM Copy new bundle in (xcopy /E /Y /Q — recursive, overwrite, quiet)
call :log "Copy %EXTRACT_DIR% -> %INSTALL_DIR%"
xcopy "%EXTRACT_DIR%\*" "%INSTALL_DIR%\" /E /Y /Q >nul
if errorlevel 1 (
    call :log "[FAIL] xcopy returned %errorlevel%"
    goto :error
)

REM Sanity check: EXE present?
if not exist "%INSTALL_DIR%\%EXE_NAME%" (
    call :log "[FAIL] new EXE missing after copy"
    goto :error
)

call :log "[OK] update applied — relaunching"

REM Relaunch (detached so this cmd window can close)
start "" "%INSTALL_DIR%\%EXE_NAME%"

REM Cleanup: this script + the staging dir
REM (rmdir on staging is fine, .bat self-deletes via `del "%~f0"`)
rmdir /S /Q "%EXTRACT_DIR%\.." 2>nul
(goto) 2>nul & del "%~f0"
exit /b 0

:error
call :log "[ABORT] update failed — opening install folder so you can re-extract manually"
start "" "%INSTALL_DIR%"
pause
exit /b 1

:log
echo %~1 >> "%LOG_FILE%"
goto :eof
"""


def write_install_batch(extracted_dir: Path, install_dir: Path) -> Path:
    """Write the install script to a temp dir OUTSIDE the install folder.

    Keeping it outside is important — the script deletes _internal/ which
    could conflict if it lived inside.
    """
    exe_name = f"{APP_NAME}.exe"
    bat_path = DATA_DIR / "updates" / "_install.bat"
    bat_path.parent.mkdir(parents=True, exist_ok=True)
    content = _INSTALL_BAT_TEMPLATE.format(
        install_dir=str(install_dir).rstrip("\\/"),
        extract_dir=str(extracted_dir).rstrip("\\/"),
        exe_name=exe_name,
    )
    # Windows batch needs CRLF
    bat_path.write_text(content, encoding="utf-8", newline="\r\n")
    log.info(f"Wrote install batch: {bat_path}")
    return bat_path


def apply_update_and_exit(extracted_dir: Path) -> None:
    """Launch the install batch (detached) then kill THIS process so the
    batch can swap files. Returns NEVER — calls os._exit(0).

    Refuses to run in dev (non-frozen) mode because there's no exe to swap.
    """
    if not IS_FROZEN:
        raise RuntimeError(
            "Tool đang chạy ở dev mode (python launch.py) — không thể "
            "tự cài. Build EXE rồi mới dùng tính năng update tự động."
        )
    if sys.platform != "win32":
        raise RuntimeError("Auto-update chỉ hỗ trợ Windows ở thời điểm này.")

    from ..config import EXE_DIR
    install_dir = EXE_DIR  # parent of running .exe

    # ── Strip data/ from staged bundle — prevent dev session leaking ──
    # If the release zip accidentally contains a data/ folder (e.g. the
    # dev forgot to clean dist/ before zipping), it would get xcopy'd
    # into the user's install dir, OVERWRITING their auth_session.json,
    # navtools.db, cookies, etc. with the dev's personal data. Symptom:
    # user updates → suddenly logged in as the developer's email.
    #
    # Fix: always nuke data/ from the staged extract before the batch
    # runs. The user's existing data/ is OUTSIDE the staged bundle and
    # is never touched by the batch (xcopy only adds, doesn't delete
    # pre-existing files). By removing staged data/, xcopy simply
    # leaves the user's data/ intact.
    try:
        data_staged = extracted_dir / "data"
        if data_staged.exists():
            shutil.rmtree(data_staged, ignore_errors=True)
            log.info(
                "Auto-update: stripped data/ from staged bundle "
                "(prevents dev session from leaking into user's install)"
            )
    except Exception as e:
        log.warning(f"Could not prune staged data/: {e}")

    bat = write_install_batch(extracted_dir, install_dir)

    _UPDATE_STATE.update({
        "stage": "installing",
        "message": "Đang cài — tool sẽ tự khởi động lại trong ~5s",
    })

    # Launch detached. CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS so it
    # survives our death. shell=False since we hand the .bat to cmd.exe.
    log.info(f"Launching installer: {bat}")
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        cwd=str(install_dir),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )

    # Give the batch a beat to spawn cleanly before we vanish
    import time
    time.sleep(0.5)

    # Hard exit — no atexit, no cleanup. The batch needs us GONE.
    os._exit(0)
