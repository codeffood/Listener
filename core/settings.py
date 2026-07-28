import json
import os
from pathlib import Path

SETTINGS_PATH = Path("data/settings.json")

DEFAULT_SETTINGS = {
    "min_segment_duration": 2.5,
    "max_segment_duration": 12.0,
    "repeat_count": 3,
    "pause_between_repeats": 0.8,
    "pause_between_segments": 1.2,
    "merge_threshold": 2.0,
    "whisper_model": "base.en",
    "split_by_punctuation": False,
    "use_srt": False,
    "end_padding": 0.15,
    "vad_min_silence_ms": 300,
    "seek_start_offset": 0.1,
    "nas_list": []
}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        # 兼容旧格式：把单个 webdav_* 字段迁移成 nas_list
        if "webdav_url" in saved and saved.get("webdav_url"):
            saved.setdefault("nas_list", [{
                "name": "默认 NAS",
                "url": saved["webdav_url"],
                "username": saved.get("webdav_username", ""),
                "password": saved.get("webdav_password", ""),
                "root": saved.get("webdav_root", "/"),
            }])
        result = {**DEFAULT_SETTINGS, **saved}
        # 清掉旧字段
        for k in ["webdav_url", "webdav_username", "webdav_password", "webdav_root"]:
            result.pop(k, None)
        return result
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: dict) -> dict:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_SETTINGS, **settings}
    for k in ["webdav_url", "webdav_username", "webdav_password", "webdav_root"]:
        merged.pop(k, None)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged
