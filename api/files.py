import os
import json
import traceback
import logging
import threading
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
import aiofiles

from core.transcriber import transcribe
from core.settings import load_settings

router = APIRouter(prefix="/api/files", tags=["files"])
logger = logging.getLogger(__name__)

# Global semaphore — only one transcription job runs at a time across all sources
_transcribe_lock = threading.Semaphore(1)

# Tracks files for which a transcription background task has been submitted but not yet completed
_active_transcriptions: set = set()

UPLOAD_DIR = Path("data/uploads")
ARCHIVE_DIR = Path("data/archive")

AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def _cache_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(".json")


def _load_cache(audio_path: Path):
    p = _cache_path(audio_path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            p.unlink(missing_ok=True)
    return None


def _find_in_archive(filename: str) -> Path | None:
    if not ARCHIVE_DIR.exists():
        return None
    for p in ARCHIVE_DIR.rglob("*"):
        if p.is_file() and p.name == filename:
            return p
    return None


def _load_cache_with_fallback(audio_path: Path, filename: str):
    cached = _load_cache(audio_path)
    if cached is not None:
        return cached
    # File may have been archived — check original upload path cache
    original_path = UPLOAD_DIR / filename
    if audio_path != original_path:
        cached = _load_cache(original_path)
        if cached is not None:
            _cache_path(audio_path).write_text(
                json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return cached


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    allowed = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".mkv", ".avi", ".mov", ".webm"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"不支持的格式: {ext}")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / file.filename
    async with aiofiles.open(dest, "wb") as f:
        content = await file.read()
        await f.write(content)

    return {"name": file.filename, "path": str(dest)}


@router.get("/audio/{filename}")
def serve_audio(filename: str, request: Request):
    path = UPLOAD_DIR / filename
    if not path.exists():
        found = _find_in_archive(filename)
        if found:
            path = found
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    file_size = path.stat().st_size
    mime = AUDIO_MIME.get(path.suffix.lower(), "audio/mpeg")
    range_header = request.headers.get("range")

    if range_header:
        range_val = range_header.strip().replace("bytes=", "")
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def iter_file():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 65536
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )

    return StreamingResponse(
        open(path, "rb"),
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        },
    )


@router.delete("/delete/{filename}")
def delete_file(filename: str):
    path = UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    stem = Path(filename).stem
    path.unlink()
    for p in UPLOAD_DIR.glob(f"{stem}.*"):
        if p.suffix in (".json", ".error"):
            p.unlink(missing_ok=True)
    try:
        from api.library import remove_entry, RemoveRequest
        remove_entry(RemoveRequest(name=filename))
    except Exception:
        pass
    return {"status": "deleted"}


from pydantic import BaseModel

class TranscribeRequest(BaseModel):
    split_by_punctuation: bool = False


@router.delete("/transcribe/{filename}/clear")
def clear_transcribe_cache(filename: str):
    path = UPLOAD_DIR / filename
    cache = _cache_path(path)
    for p in [cache, cache.with_suffix(".error")]:
        p.unlink(missing_ok=True)
    return {"status": "cleared"}


@router.post("/transcribe/{filename}")
def transcribe_file(filename: str, background_tasks: BackgroundTasks, req: TranscribeRequest = TranscribeRequest()):
    path = UPLOAD_DIR / filename
    if not path.exists():
        found = _find_in_archive(filename)
        if found:
            path = found
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    cached = _load_cache_with_fallback(path, filename)
    if cached is not None:
        return {"status": "cached", "segments": cached}

    _active_transcriptions.add(str(path))
    background_tasks.add_task(_do_transcribe, path, req.split_by_punctuation)
    return {"status": "processing"}


@router.get("/transcribe/{filename}/status")
def transcribe_status(filename: str):
    path = UPLOAD_DIR / filename
    if not path.exists():
        found = _find_in_archive(filename)
        if found:
            path = found

    cache = _cache_path(path)
    error_path = cache.with_suffix(".error")

    cached = _load_cache_with_fallback(path, filename)
    if cached is not None:
        return {"status": "done", "segments": cached}
    if error_path.exists():
        with open(error_path, "r", encoding="utf-8") as f:
            return {"status": "error", "message": f.read()}
    if str(path) in _active_transcriptions:
        return {"status": "processing"}
    return {"status": "not_started"}


@router.post("/cache/cleanup")
def cleanup_cache():
    """Delete orphaned and corrupt cache files. Returns counts."""
    deleted_orphan = 0
    deleted_corrupt = 0

    # Collect all known audio file names (uploads + archive)
    audio_exts = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".mkv", ".avi", ".mov", ".webm"}
    known = set()
    for d in [UPLOAD_DIR] + (list(ARCHIVE_DIR.rglob("*")) if ARCHIVE_DIR.exists() else []):
        p = Path(d) if isinstance(d, str) else d
        if p.is_file() and p.suffix.lower() in audio_exts:
            known.add(p.stem)

    # Check same-dir JSON caches in uploads
    for f in UPLOAD_DIR.iterdir():
        if f.suffix == ".json":
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    json.load(fh)
                # Valid JSON — check if matching audio exists
                if f.stem not in known:
                    f.unlink()
                    deleted_orphan += 1
            except (json.JSONDecodeError, OSError):
                f.unlink(missing_ok=True)
                deleted_corrupt += 1
        elif f.suffix == ".error":
            if f.stem not in known:
                f.unlink(missing_ok=True)
                deleted_orphan += 1

    return {"deleted_orphan": deleted_orphan, "deleted_corrupt": deleted_corrupt}


def _do_transcribe(audio_path: Path, split_by_punctuation: bool = False):
    cache = _cache_path(audio_path)
    error_path = cache.with_suffix(".error")
    _transcribe_lock.acquire()
    try:
        settings = load_settings()
        segments = transcribe(
            str(audio_path),
            model_name=settings["whisper_model"],
            min_duration=settings["min_segment_duration"],
            max_duration=settings["max_segment_duration"],
            merge_threshold=settings["merge_threshold"],
            split_by_punctuation=split_by_punctuation,
            use_srt=settings.get("use_srt", False),
        )
        cache.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        if error_path.exists():
            error_path.unlink()
    except Exception:
        err = traceback.format_exc()
        logger.error(f"转录失败 {audio_path}:\n{err}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(err, encoding="utf-8")
    finally:
        _transcribe_lock.release()
        _active_transcriptions.discard(str(audio_path))
