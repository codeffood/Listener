from fastapi import APIRouter
from pydantic import BaseModel
from typing import List
from core.settings import load_settings, save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class NasEntry(BaseModel):
    name: str = ""
    url: str = ""
    username: str = ""
    password: str = ""
    root: str = "/"


class Settings(BaseModel):
    min_segment_duration: float = 2.5
    max_segment_duration: float = 12.0
    repeat_count: int = 3
    pause_between_repeats: float = 0.8
    pause_between_segments: float = 1.2
    merge_threshold: float = 2.0
    whisper_model: str = "base.en"
    split_by_punctuation: bool = False
    use_srt: bool = False
    nas_list: List[NasEntry] = []


@router.get("")
def get_settings():
    return load_settings()


@router.post("")
def update_settings(settings: Settings):
    data = settings.model_dump()
    data["nas_list"] = [n for n in data["nas_list"]]
    saved = save_settings(data)
    return saved
