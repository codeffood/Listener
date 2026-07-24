import json
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/library", tags=["library"])

LIBRARY_PATH = Path("data/library.json")
UPLOAD_DIR = Path("data/uploads")
NAS_CACHE_DIR = Path("data/cache/nas")
ARCHIVE_DIR = Path("data/archive")


def _nas_cache_path(nas_idx: int, remote_path: str) -> Path:
    stem = Path(remote_path).stem
    return NAS_CACHE_DIR / str(nas_idx) / f"{stem}.json"


def _local_cache_path(name: str) -> Path:
    return UPLOAD_DIR / (Path(name).stem + ".json")


def _load() -> list:
    if LIBRARY_PATH.exists():
        with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(entries: list):
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _enrich(entry: dict) -> dict:
    """Add live transcribed status without storing it."""
    e = dict(entry)
    if e["type"] == "nas":
        if e.get("archived"):
            cache_name = e.get("archive_cache_name", "")
            folder = e.get("archive_folder", "")
            e["transcribed"] = bool(cache_name and (ARCHIVE_DIR / folder / cache_name).exists())
        else:
            e["transcribed"] = _nas_cache_path(e["nas_idx"], e["path"]).exists()
    else:
        if e.get("archived"):
            folder = e.get("archive_folder", "")
            audio = ARCHIVE_DIR / folder / e["name"]
            cache = ARCHIVE_DIR / folder / (Path(e["name"]).stem + ".json")
        else:
            audio = UPLOAD_DIR / e["name"]
            cache = _local_cache_path(e["name"])
        e["exists"] = audio.exists()
        e["transcribed"] = cache.exists()
        if audio.exists():
            e["size"] = audio.stat().st_size
    return e


def sync_local_files():
    """Add any local uploads not yet in library, remove entries whose file is gone."""
    entries = _load()
    # Only consider non-archived local entries for sync
    local_names = {e["name"] for e in entries if e["type"] == "local" and not e.get("archived")}

    # Add new local files
    changed = False
    for f in UPLOAD_DIR.iterdir():
        if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".mkv", ".avi", ".mov", ".webm"):
            if f.name not in local_names:
                entries.append({"type": "local", "name": f.name, "size": f.stat().st_size})
                changed = True

    if changed:
        _save(entries)
    return entries


@router.get("")
def get_library():
    entries = sync_local_files()
    return [_enrich(e) for e in entries]


class NasEntry(BaseModel):
    nas_idx: int
    nas_name: str
    path: str
    name: str


@router.post("/nas")
def add_nas_entry(entry: NasEntry):
    entries = _load()
    # Update if path already exists, otherwise append
    for e in entries:
        if e["type"] == "nas" and e["path"] == entry.path:
            e["nas_idx"] = entry.nas_idx
            e["nas_name"] = entry.nas_name
            e["name"] = entry.name
            _save(entries)
            return {"status": "updated"}
    entries.append({
        "type": "nas",
        "name": entry.name,
        "nas_idx": entry.nas_idx,
        "nas_name": entry.nas_name,
        "path": entry.path,
    })
    _save(entries)
    return {"status": "added"}


class RemoveRequest(BaseModel):
    path: str = ""
    name: str = ""


@router.delete("/entry")
def remove_entry(req: RemoveRequest):
    entries = _load()
    if req.path:
        entries = [e for e in entries if not (e["type"] == "nas" and e["path"] == req.path)]
    elif req.name:
        entries = [e for e in entries if not (e["type"] == "local" and e["name"] == req.name)]
    _save(entries)
    return {"status": "removed"}
