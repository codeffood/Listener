import json
import hashlib
import tempfile
import traceback
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from core import webdav as wdav
from core.settings import load_settings
from core.transcriber import transcribe

router = APIRouter(prefix="/api/webdav", tags=["webdav"])
logger = logging.getLogger(__name__)

NAS_CACHE_DIR = Path("data/cache/nas")

# In-memory status for NAS transcription jobs: key = (nas_idx, remote_path)
_nas_jobs: dict = {}   # key -> {"status": "processing"|"done"|"error", "segments": [...], "message": ""}


def _nas_cache_path(nas_idx: int, remote_path: str) -> Path:
    key = f"{nas_idx}:{remote_path}"
    h = hashlib.md5(key.encode()).hexdigest()
    return NAS_CACHE_DIR / f"{h}.json"


class ConnectRequest(BaseModel):
    url: str
    username: str
    password: str
    path: str = "/"


@router.post("/list")
def list_webdav(req: ConnectRequest):
    try:
        items = wdav.list_files(req.url, req.username, req.password, req.path)
        return {"items": items}
    except Exception as e:
        raise HTTPException(400, f"连接失败: {str(e)}")


# kept for backward-compat (NAS tab no longer uses it, but old bookmarks may)
@router.post("/download")
def download_from_webdav(req: ConnectRequest):
    from pathlib import Path as _P
    UPLOAD_DIR = _P("data/uploads")
    filename = req.path.rstrip("/").split("/")[-1]
    local_path = UPLOAD_DIR / filename
    try:
        wdav.download_file(req.url, req.username, req.password, req.path, str(local_path))
        return {"name": filename, "path": str(local_path)}
    except Exception as e:
        raise HTTPException(400, f"下载失败: {str(e)}")


class TestRequest(BaseModel):
    url: str
    username: str
    password: str
    root: str = "/"


@router.post("/test")
def test_connection(req: TestRequest):
    if not req.url:
        raise HTTPException(400, "未填写 WebDAV 地址")
    try:
        items = wdav.list_files(req.url, req.username, req.password, req.root)
        return {"status": "ok", "items_count": len(items)}
    except Exception as e:
        raise HTTPException(400, f"连接失败: {str(e)}")


# ── NAS 直接播放 ─────────────────────────────────────────────────────────────

class NasFileRequest(BaseModel):
    nas_idx: int
    path: str
    split_by_punctuation: bool = False


def _get_nas(nas_idx: int) -> dict:
    settings = load_settings()
    nas_list = settings.get("nas_list", [])
    if nas_idx < 0 or nas_idx >= len(nas_list):
        raise HTTPException(404, "NAS 不存在")
    return nas_list[nas_idx]


@router.get("/pdf")
def stream_nas_pdf(nas_idx: int, path: str):
    """Proxy a PDF from NAS so the browser can open it directly."""
    nas = _get_nas(nas_idx)
    url, user, pw = nas["url"], nas["username"], nas["password"]
    try:
        data = wdav.download_bytes(url, user, pw, path)
    except Exception as e:
        raise HTTPException(502, f"NAS 读取失败: {str(e)}")
    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{path.split('/')[-1]}\""},
    )


@router.get("/stream")
def stream_nas_audio(nas_idx: int, path: str, request: Request):
    """Proxy-stream a NAS audio file to the browser without saving locally."""
    nas = _get_nas(nas_idx)
    url, user, pw = nas["url"], nas["username"], nas["password"]

    # Determine content-type from extension
    AUDIO_MIME = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac",
    }
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.split("/")[-1] else ""
    mime = AUDIO_MIME.get(ext, "audio/mpeg")

    try:
        data = wdav.download_bytes(url, user, pw, path)
    except Exception as e:
        raise HTTPException(502, f"NAS 读取失败: {str(e)}")

    total = len(data)
    range_header = request.headers.get("range")

    if range_header:
        range_val = range_header.strip().replace("bytes=", "")
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else total - 1
        end = min(end, total - 1)
        length = end - start + 1
        chunk = data[start:end + 1]
        return StreamingResponse(
            iter([chunk]),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )

    return StreamingResponse(
        iter([data]),
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(total),
        },
    )


@router.post("/nas/transcribe/clear")
def nas_transcribe_clear(req: NasFileRequest):
    job_key = (req.nas_idx, req.path)
    cache = _nas_cache_path(req.nas_idx, req.path)
    if cache.exists():
        cache.unlink()
    _nas_jobs.pop(job_key, None)
    return {"status": "cleared"}


@router.post("/nas/transcribe")
def nas_transcribe(req: NasFileRequest, background_tasks: BackgroundTasks):
    """Start transcription for a NAS audio file (result cached locally)."""
    nas = _get_nas(req.nas_idx)
    job_key = (req.nas_idx, req.path)
    cache = _nas_cache_path(req.nas_idx, req.path)

    # Local cache hit
    if cache.exists():
        try:
            with open(cache, "r", encoding="utf-8") as f:
                return {"status": "cached", "segments": json.load(f)}
        except (json.JSONDecodeError, OSError):
            cache.unlink(missing_ok=True)

    # Already running?
    if job_key in _nas_jobs and _nas_jobs[job_key]["status"] == "processing":
        return {"status": "processing"}

    _nas_jobs[job_key] = {"status": "processing", "segments": [], "message": ""}
    background_tasks.add_task(_do_nas_transcribe, nas, req.path, cache, job_key, req.split_by_punctuation)
    return {"status": "processing"}


@router.post("/nas/transcribe/status")
def nas_transcribe_status(req: NasFileRequest):
    job_key = (req.nas_idx, req.path)
    cache = _nas_cache_path(req.nas_idx, req.path)

    if cache.exists():
        try:
            with open(cache, "r", encoding="utf-8") as f:
                return {"status": "done", "segments": json.load(f)}
        except (json.JSONDecodeError, OSError):
            cache.unlink(missing_ok=True)
    if job_key not in _nas_jobs:
        return {"status": "not_started"}
    job = _nas_jobs[job_key]
    if job["status"] == "done":
        return {"status": "done", "segments": job["segments"]}
    if job["status"] == "error":
        return {"status": "error", "message": job["message"]}
    return {"status": "processing"}


def _do_nas_transcribe(nas: dict, remote_path: str, cache: Path, job_key: tuple, split_by_punctuation: bool = False):
    url, user, pw = nas["url"], nas["username"], nas["password"]
    tmp_path = None
    tmp_srt = None
    logger.info(f"NAS 转录开始: {remote_path}")
    try:
        audio_bytes = wdav.download_bytes(url, user, pw, remote_path)
        ext = Path(remote_path).suffix
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        # Try to fetch a matching SRT from NAS (case-insensitive: .srt / .SRT)
        # Use string split instead of Path.with_suffix to preserve forward slashes on Windows
        srt_path_local = None
        remote_base = remote_path.rsplit(".", 1)[0] if "." in remote_path.split("/")[-1] else remote_path
        for srt_ext in [".srt", ".SRT"]:
            srt_remote = remote_base + srt_ext
            try:
                if wdav.check_exists(url, user, pw, srt_remote):
                    srt_bytes = wdav.download_bytes(url, user, pw, srt_remote)
                    with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="wb") as sf:
                        sf.write(srt_bytes)
                        tmp_srt = sf.name
                    srt_path_local = tmp_srt
                    logger.info(f"SRT found on NAS: {srt_remote}")
                    break
            except Exception:
                pass

        settings = load_settings()
        segments = transcribe(
            tmp_path,
            model_name=settings["whisper_model"],
            min_duration=settings["min_segment_duration"],
            max_duration=settings["max_segment_duration"],
            merge_threshold=settings["merge_threshold"],
            srt_path=srt_path_local,
            split_by_punctuation=split_by_punctuation,
            use_srt=settings.get("use_srt", False),
        )

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

        _nas_jobs[job_key] = {"status": "done", "segments": segments, "message": ""}
    except Exception:
        err = traceback.format_exc()
        logger.error(f"NAS 转录失败 {remote_path}:\n{err}")
        _nas_jobs[job_key] = {"status": "error", "segments": [], "message": err}
    finally:
        for p in [tmp_path, tmp_srt]:
            if p:
                try:
                    import os; os.unlink(p)
                except Exception:
                    pass
