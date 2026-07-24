import json
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/archive", tags=["archive"])

UPLOAD_DIR   = Path("data/uploads")
ARCHIVE_DIR  = Path("data/archive")
NAS_CACHE_DIR = Path("data/cache/nas")
LIBRARY_PATH = Path("data/library.json")


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_library() -> list:
    if LIBRARY_PATH.exists():
        with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_library(entries: list):
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _nas_cache_path(nas_idx: int, remote_path: str) -> Path:
    stem = Path(remote_path).stem
    return NAS_CACHE_DIR / str(nas_idx) / f"{stem}.json"


def _local_cache_path(name: str) -> Path:
    return UPLOAD_DIR / (Path(name).stem + ".json")


def _archived_audio_path(folder: str, name: str) -> Path:
    return ARCHIVE_DIR / folder / name


def _archived_cache_path(folder: str, name: str) -> Path:
    stem = Path(name).stem
    return ARCHIVE_DIR / folder / f"{stem}.json"


# ── list folders ──────────────────────────────────────────────────────────────

@router.get("/folders")
def list_folders():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for p in sorted(ARCHIVE_DIR.rglob("*")):
        if p.is_dir():
            rel = p.relative_to(ARCHIVE_DIR).as_posix()
            result.append(rel)
    return result


# ── browse one level ──────────────────────────────────────────────────────────

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".mkv", ".avi", ".mov", ".webm"}

@router.get("/browse")
def browse(path: str = ""):
    """List direct children of data/archive/<path>: subdirs + archived library entries."""
    base = ARCHIVE_DIR / path if path else ARCHIVE_DIR
    base.mkdir(parents=True, exist_ok=True)

    # subdirectories at this level
    subdirs = sorted([d.name for d in base.iterdir() if d.is_dir()])

    # library entries archived into exactly this folder
    rel_path = path  # e.g. "" or "2024" or "2024/BBC"
    entries = _load_library()
    files = []
    for e in entries:
        if not e.get("archived"):
            continue
        folder = e.get("archive_folder", "")
        if folder != rel_path:
            continue
        ec = dict(e)
        if e["type"] == "local":
            audio = ARCHIVE_DIR / folder / e["name"]
            cache = ARCHIVE_DIR / folder / (Path(e["name"]).stem + ".json")
            ec["exists"]      = audio.exists()
            ec["transcribed"] = cache.exists()
            if audio.exists():
                ec["size"] = audio.stat().st_size
        else:
            cache_name = e.get("archive_cache_name", "")
            ec["transcribed"] = bool(cache_name and (ARCHIVE_DIR / folder / cache_name).exists())
        files.append(ec)

    return {"subdirs": subdirs, "files": files}


# ── list archived entries ─────────────────────────────────────────────────────

@router.get("/entries")
def list_archived():
    entries = _load_library()
    result = []
    for e in entries:
        if not e.get("archived"):
            continue
        ec = dict(e)
        if e["type"] == "local":
            audio = _archived_audio_path(e["archive_folder"], e["name"])
            cache = _archived_cache_path(e["archive_folder"], e["name"])
            ec["exists"]      = audio.exists()
            ec["transcribed"] = cache.exists()
            if audio.exists():
                ec["size"] = audio.stat().st_size
        else:
            ec["transcribed"] = _nas_cache_path(e["nas_idx"], e["path"]).exists()
        result.append(ec)
    return result


# ── archive ───────────────────────────────────────────────────────────────────

class ArchiveRequest(BaseModel):
    folder: str          # destination folder name under data/archive/
    name: str   = ""     # local file name
    path: str   = ""     # NAS remote path
    nas_idx: int = -1


@router.post("/archive")
def archive_entry(req: ArchiveRequest):
    folder = req.folder.strip()
    if not folder:
        raise HTTPException(400, "folder 不能为空")

    dest_dir = ARCHIVE_DIR / folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    entries = _load_library()

    if req.name:
        # ── local file ──
        src_audio = UPLOAD_DIR / req.name
        src_cache = _local_cache_path(req.name)
        if not src_audio.exists():
            raise HTTPException(404, f"文件不存在: {req.name}")

        dst_audio = dest_dir / req.name
        dst_cache = dest_dir / (Path(req.name).stem + ".json")

        shutil.move(str(src_audio), dst_audio)
        if src_cache.exists():
            shutil.move(str(src_cache), dst_cache)

        for e in entries:
            if e["type"] == "local" and e["name"] == req.name:
                e["archived"]       = True
                e["archive_folder"] = folder
                break

    elif req.path and req.nas_idx >= 0:
        # ── NAS entry — move transcription cache only ──
        src_cache = _nas_cache_path(req.nas_idx, req.path)
        if src_cache.exists():
            dst_cache = dest_dir / src_cache.name
            shutil.move(str(src_cache), dst_cache)

        for e in entries:
            if e["type"] == "nas" and e["path"] == req.path:
                e["archived"]            = True
                e["archive_folder"]      = folder
                e["archive_cache_name"]  = src_cache.name
                break
    else:
        raise HTTPException(400, "需要提供 name（本地文件）或 path+nas_idx（NAS）")

    _save_library(entries)
    return {"status": "archived", "folder": folder}


# ── restore ───────────────────────────────────────────────────────────────────

class RestoreRequest(BaseModel):
    name: str   = ""
    path: str   = ""
    nas_idx: int = -1


@router.post("/restore")
def restore_entry(req: RestoreRequest):
    entries = _load_library()

    if req.name:
        entry = next((e for e in entries if e["type"] == "local" and e["name"] == req.name), None)
        if not entry:
            raise HTTPException(404, "库中找不到该条目")

        folder    = entry.get("archive_folder", "")
        src_audio = _archived_audio_path(folder, req.name)
        src_cache = _archived_cache_path(folder, req.name)
        dst_audio = UPLOAD_DIR / req.name
        dst_cache = _local_cache_path(req.name)

        if src_audio.exists():
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_audio), dst_audio)
        if src_cache.exists():
            shutil.move(str(src_cache), dst_cache)

        entry.pop("archived", None)
        entry.pop("archive_folder", None)

    elif req.path and req.nas_idx >= 0:
        entry = next((e for e in entries if e["type"] == "nas" and e["path"] == req.path), None)
        if not entry:
            raise HTTPException(404, "库中找不到该条目")

        folder          = entry.get("archive_folder", "")
        cache_name      = entry.get("archive_cache_name", "")
        src_cache       = ARCHIVE_DIR / folder / cache_name
        dst_cache       = NAS_CACHE_DIR / cache_name

        if src_cache.exists():
            NAS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_cache), dst_cache)

        entry.pop("archived", None)
        entry.pop("archive_folder", None)
        entry.pop("archive_cache_name", None)
    else:
        raise HTTPException(400, "需要提供 name 或 path+nas_idx")

    _save_library(entries)
    return {"status": "restored"}


# ── export segments ───────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    filename: str
    segments: list   # [{id, start, end, text}, ...]


def _fmt_time(s: float) -> str:
    m = int(s) // 60
    sec = s - m * 60
    return f"{m:02d}:{sec:05.2f}"


@router.post("/export")
def export_segments(req: ExportRequest):
    title = Path(req.filename).stem
    lines = [title, "=" * len(title), ""]
    for seg in req.segments:
        lines.append(f"[{_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}]")
        lines.append(seg["text"].strip())
        lines.append("")
    content = "\n".join(lines)
    safe = title.replace(" ", "_")
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.txt"'},
    )
