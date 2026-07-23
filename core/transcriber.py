import os
import re
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from pathlib import Path
from faster_whisper import WhisperModel
from typing import List, Dict, Optional
import spacy

_model_cache: Dict[str, WhisperModel] = {}
_nlp = None


def get_nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def get_model(model_name: str) -> WhisperModel:
    if model_name not in _model_cache:
        _model_cache[model_name] = WhisperModel(
            model_name,
            device="cpu",
            compute_type="float32"
        )
    return _model_cache[model_name]


def transcribe(
    audio_path: str,
    model_name: str = "base.en",
    min_duration: float = 2.5,
    max_duration: float = 12.0,
    merge_threshold: float = 2.0,
    srt_path: Optional[str] = None,
    split_by_punctuation: bool = False,
    use_srt: bool = False,
) -> List[Dict]:
    # Only use SRT if explicitly enabled
    if use_srt:
        if srt_path is None:
            candidate = Path(audio_path).with_suffix(".srt")
            if candidate.exists():
                srt_path = str(candidate)
        if srt_path and Path(srt_path).exists():
            entries = _parse_srt_entries(srt_path)
            if entries:
                return _sentences_from_srt_entries(entries, min_duration, max_duration)

    # Fall back to Whisper
    model = get_model(model_name)
    segments, _ = model.transcribe(
        audio_path,
        word_timestamps=True,
        language="en",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
    )

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({"word": w.word, "start": w.start, "end": w.end})

    if not words:
        return []

    if split_by_punctuation:
        return _sentences_from_punctuation(words, min_duration, max_duration)
    return _sentences_from_words(words, min_duration, max_duration)


def _sentences_from_punctuation(
    words: List[Dict],
    min_dur: float,
    max_dur: float,
) -> List[Dict]:
    """Split at sentence-ending punctuation (.?!), then merge/split by duration."""
    SENT_END = set(".?!。？！…")
    raw = []
    current_words = []
    for w in words:
        current_words.append(w)
        if w["word"].rstrip().endswith(tuple(SENT_END)):
            raw.append({
                "start": current_words[0]["start"],
                "end": current_words[-1]["end"],
                "text": "".join(cw["word"] for cw in current_words).strip(),
                "words": list(current_words),
            })
            current_words = []
    if current_words:
        raw.append({
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"],
            "text": "".join(cw["word"] for cw in current_words).strip(),
            "words": list(current_words),
        })
    return _merge_and_split(raw, min_dur, max_dur)


def _srt_time(ts: str) -> float:
    """Parse SRT timestamp '00:01:23,456' → seconds."""
    ts = ts.replace(",", ".")
    h, m, rest = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def _parse_srt_entries(srt_path: str) -> List[Dict]:
    """Parse SRT into [{start, end, text}] entries, handling mid-word line breaks."""
    text = Path(srt_path).read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    entries = []
    for block in blocks:
        lines = block.strip().splitlines()
        tc_line = None
        text_lines = []
        for line in lines:
            if "-->" in line:
                tc_line = line
            elif tc_line is not None and not line.strip().isdigit():
                text_lines.append(line.strip())
        if not tc_line or not text_lines:
            continue
        try:
            parts = tc_line.split("-->")
            t_start = _srt_time(parts[0].strip())
            t_end = _srt_time(parts[1].strip())
        except Exception:
            continue

        # Join lines — if previous line ends mid-word (letter, no punct),
        # concatenate directly to fix BBC-style line breaks like "f\nrom"
        raw = text_lines[0]
        for line in text_lines[1:]:
            if raw and raw[-1].isalpha() and line and line[0].isalpha():
                raw += line
            else:
                raw += " " + line
        raw = re.sub(r"<[^>]+>", "", raw).strip()
        if raw:
            entries.append({"start": t_start, "end": t_end, "text": raw})
    return entries


def _sentences_from_srt_entries(
    entries: List[Dict],
    min_dur: float,
    max_dur: float,
) -> List[Dict]:
    """
    Use SRT cue times directly (no interpolation) + spaCy for sentence boundaries.
    Each merged sentence's start/end comes from the first/last SRT entry it spans.
    """
    nlp = get_nlp()

    # Build full text, tracking character range of each entry
    full_text = ""
    entry_ranges = []
    for i, entry in enumerate(entries):
        sc = len(full_text)
        full_text += entry["text"].strip()
        entry_ranges.append((sc, len(full_text), i))
        full_text += " "

    doc = nlp(full_text)

    raw_sentences = []
    for sent in doc.sents:
        sc, ec = sent.start_char, sent.end_char
        overlapping = [ei for (es, ee, ei) in entry_ranges if ee > sc and es < ec]
        if not overlapping:
            continue
        raw_sentences.append({
            "start": entries[overlapping[0]]["start"],
            "end":   entries[overlapping[-1]]["end"],
            "text":  sent.text.strip(),
        })

    return _merge_and_split(raw_sentences, min_dur, max_dur)


def _sentences_from_words(
    words: List[Dict],
    min_dur: float,
    max_dur: float,
) -> List[Dict]:
    nlp = get_nlp()

    full_text = ""
    offsets = []
    for w in words:
        start_char = len(full_text)
        token = w["word"]
        full_text += token
        end_char = len(full_text)
        offsets.append((start_char, end_char))
        full_text += " "

    doc = nlp(full_text)
    sent_spans = list(doc.sents)

    raw_sentences = []
    for sent in sent_spans:
        sent_start_char = sent.start_char
        sent_end_char = sent.end_char

        matched_words = []
        for i, (wstart, wend) in enumerate(offsets):
            if wend > sent_start_char and wstart < sent_end_char:
                matched_words.append(words[i])

        if not matched_words:
            continue

        raw_sentences.append({
            "start": matched_words[0]["start"],
            "end": matched_words[-1]["end"],
            "text": sent.text.strip(),
            "words": matched_words,
        })

    return _merge_and_split(raw_sentences, min_dur, max_dur)


def _merge_and_split(
    segments: List[Dict],
    min_dur: float,
    max_dur: float,
) -> List[Dict]:
    if not segments:
        return []

    merged = []
    current = segments[0].copy()

    for seg in segments[1:]:
        current_dur = current["end"] - current["start"]
        projected_dur = seg["end"] - current["start"]

        if current_dur < min_dur and projected_dur <= max_dur:
            current["end"] = seg["end"]
            current["text"] = current["text"] + " " + seg["text"].strip()
            # Keep merged word list so _split_long_segment has accurate timestamps
            current["words"] = current.get("words", []) + seg.get("words", [])
        else:
            merged.append(current)
            current = seg.copy()

    merged.append(current)

    result = []
    for seg in merged:
        if seg["end"] - seg["start"] > max_dur:
            result.extend(_split_long_segment(seg, max_dur))
        else:
            result.append(seg)

    END_PADDING = 0.15  # 150ms tail padding to avoid clipping trailing consonants
    for i, seg in enumerate(result):
        seg["id"] = i
        next_start = result[i + 1]["start"] if i + 1 < len(result) else None
        padded_end = seg["end"] + END_PADDING
        if next_start is not None:
            padded_end = min(padded_end, next_start)
        seg["end"] = round(padded_end, 3)
        seg.pop("words", None)  # Strip internal data from API output

    return result


def _split_long_segment(seg: Dict, max_dur: float) -> List[Dict]:
    """Split using actual word timestamps; fall back to character ratio if unavailable."""
    word_list = seg.get("words", [])

    if len(word_list) >= 2:
        return _split_by_words(seg, word_list, max_dur)

    # Fallback for SRT segments (no word timestamps): estimate by character position
    text = seg["text"]
    duration = seg["end"] - seg["start"]
    start = seg["start"]

    candidates = []
    for i, char in enumerate(text):
        if char in ".?!":
            ratio = (i + 1) / len(text)
            split_time = start + duration * ratio
            if max_dur * 0.25 < (split_time - start) < duration * 0.75:
                candidates.append((i + 1, split_time))

    if candidates:
        mid_time = start + duration / 2
        idx, split_time = min(candidates, key=lambda c: abs(c[1] - mid_time))
        first  = {"start": start,      "end": split_time,  "text": text[:idx].strip()}
        second = {"start": split_time, "end": seg["end"],  "text": text[idx:].strip()}
    else:
        fallback_words = text.split()
        if len(fallback_words) <= 1:
            return [{k: v for k, v in seg.items() if k != "words"}]
        mid_idx = len(fallback_words) // 2
        first_text  = " ".join(fallback_words[:mid_idx])
        second_text = " ".join(fallback_words[mid_idx:])
        ratio = len(first_text) / max(len(text), 1)
        mid_time = start + duration * ratio
        first  = {"start": start,    "end": mid_time,    "text": first_text}
        second = {"start": mid_time, "end": seg["end"],  "text": second_text}

    result = []
    for part in [first, second]:
        if part["end"] - part["start"] > max_dur:
            result.extend(_split_long_segment(part, max_dur))
        else:
            result.append(part)
    return result


def _split_by_words(seg: Dict, word_list: List[Dict], max_dur: float) -> List[Dict]:
    """Split at a word boundary: prefer sentence-ending punctuation near midpoint."""
    duration = seg["end"] - seg["start"]
    mid_time = seg["start"] + duration / 2

    # Look for sentence-ending punctuation that's not too close to either end
    candidates = []
    for i, w in enumerate(word_list[:-1]):
        if w["word"].rstrip()[-1:] in ".?!":
            pos_ratio = (w["end"] - seg["start"]) / duration
            if 0.2 < pos_ratio < 0.8:
                candidates.append(i)

    if candidates:
        split_idx = min(candidates, key=lambda i: abs(word_list[i]["end"] - mid_time))
    else:
        # No good punctuation — split at word boundary closest to time midpoint
        split_idx = min(
            range(len(word_list) - 1),
            key=lambda i: abs(word_list[i]["end"] - mid_time),
        )

    first_words  = word_list[:split_idx + 1]
    second_words = word_list[split_idx + 1:]

    first = {
        "start": first_words[0]["start"],
        "end":   first_words[-1]["end"],
        "text":  "".join(w["word"] for w in first_words).strip(),
        "words": first_words,
    }
    second = {
        "start": second_words[0]["start"],
        "end":   second_words[-1]["end"],
        "text":  "".join(w["word"] for w in second_words).strip(),
        "words": second_words,
    }

    result = []
    for part in [first, second]:
        if part["end"] - part["start"] > max_dur:
            result.extend(_split_long_segment(part, max_dur))
        else:
            result.append(part)
    return result
