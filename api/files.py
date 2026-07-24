import os
import json
import hashlib
import traceback
import logging
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
import aiofiles

from core.transcriber import transcribe
from core.settings import load_settings

router = APIRouter(prefix="/api/files", tags=["files"])
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("data/uploads")
LEGACY_CACHE_DIR = Path("data/cache")
ARCHIVE_DIR = Path("data/archive")

AUDIO_MIME = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
}


def _cache_path(audio_path: Path) -> Path:
    """Return <audio_path>.json (same directory as the audio file)."""
    return audio_path.with_suffix(".json")


def _legacy_cache_path(audio_path: Path) -> Path:
    h = hashlib.md5(str(audio_path).encode()).hexdigest()
    return LEGACY_CACHE_DIR / f"{h}.json"


def _load_cache(audio_path: Path):
    """Load cache, preferring same-dir JSON; fall back to old MD5 cache."""
    new = _cache_path(audio_path)
    if new.exists():
        try:
            with open(new, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            new.unlink(missing_ok=True)  # corrupt file, delete and re-transcribe
    old = _legacy_cache_path(audio_path)
    if old.exists():
        try:
            with open(old, "r", encoding="utf-8") as f:
                data = json.load(f)
            new.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        except (json.JSONDecodeError, OSError):
            old.unlink(missing_ok=True)
    return None


def _find_in_archive(filename: str) -> Path | None:
    """Find a file by exact name anywhere in the archive directory (glob-safe)."""
    if not ARCHIVE_DIR.exists():
        return None
    for p in ARCHIVE_DIR.rglob("*"):
        if p.is_file() and p.name == filename:
            return p
    return None


def _load_cache_with_fallback(audio_path: Path, filename: str):
    """Load cache for a file, also checking legacy cache keyed from original upload path."""
    cached = _load_cache(audio_path)
    if cached is not None:
        return cached
    # If the file was archived, the legacy MD5 cache was computed from the original upload path.
    # Try that path even though the audio file no longer lives there.
    original_path = UPLOAD_DIR / filename
    if audio_path != original_path:
        cached = _load_cache(original_path)
        if cached is not None:
            # migrate: write cache next to archived audio so future loads are fast
            _cache_path(audio_path).write_text(
                json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return cached
    files = []
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for f in UPLOAD_DIR.iterdir():
        if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac"):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "path": str(f),
                "transcribed": _cache_path(f).exists() or _legacy_cache_path(f).exists(),
            })
    return sorted(files, key=lambda x: x["name"])


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    allowed = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac"}
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
    path.unlink()
    for p in [_cache_path(path), _legacy_cache_path(path),
              _cache_path(path).with_suffix(".error"), _legacy_cache_path(path).with_suffix(".error")]:
        if p.exists():
            p.unlink()
    # Remove from library
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
    legacy = _legacy_cache_path(path)
    for p in [cache, legacy, cache.with_suffix(".error"), legacy.with_suffix(".error")]:
        if p.exists():
            p.unlink()
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
    return {"status": "processing"}


def _do_transcribe(audio_path: Path, split_by_punctuation: bool = False):
    cache = _cache_path(audio_path)
    error_path = cache.with_suffix(".error")
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
