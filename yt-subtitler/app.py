"""yt-subtitler — clip a portion of a YouTube video and burn in subtitles.

Pipeline per job: yt-dlp --download-sections (only fetches the slice) →
faster-whisper transcribes/translates → ffmpeg burns subtitles into the clip.
Outputs three files: original clip, .srt, and the subtitled .mp4.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import gc
import json
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8077"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Model cache lives outside data/ so cleaning jobs doesn't nuke it.
MODEL_CACHE = Path(os.environ.get("MODEL_CACHE", str(Path(__file__).parent / "models"))).resolve()
MODEL_CACHE.mkdir(parents=True, exist_ok=True)

VALID_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
MAX_JOB_SECONDS = 60 * 30
MAX_SAVED_JOBS = int(os.environ.get("MAX_SAVED_JOBS", "25"))
FAILED_HISTORY_LIMIT = int(os.environ.get("FAILED_HISTORY_LIMIT", "100"))
MODEL_IDLE_EVICT_SECONDS = int(os.environ.get("MODEL_IDLE_EVICT_SECONDS", "900"))
MAX_QUEUED_JOBS = int(os.environ.get("MAX_QUEUED_JOBS", "8"))

# Browser to pull cookies from (helps yt-dlp past YouTube's bot checks).
# Empty string disables.
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER", "")

# LM Studio (or any OpenAI-compatible endpoint) for high-quality LLM translation.
# Defaults — overridable via env or via the Settings tab (persisted to data/settings.json).
LLM_BASE_URL_DEFAULT = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:1234/v1"
                                      if os.environ.get("IN_DOCKER") else
                                      "http://127.0.0.1:1234/v1")
LLM_MODEL_DEFAULT    = os.environ.get("LLM_MODEL", "")
LLM_API_KEY_DEFAULT  = os.environ.get("LLM_API_KEY", "lm-studio")


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_settings() -> dict:
    """Read persisted settings, falling back to env-var defaults for any missing keys."""
    out = {
        "llm_base_url": LLM_BASE_URL_DEFAULT,
        "llm_model":    LLM_MODEL_DEFAULT,
        "llm_api_key":  LLM_API_KEY_DEFAULT,
    }
    p = _settings_path()
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
            for k in out:
                if isinstance(stored.get(k), str) and stored[k].strip():
                    out[k] = stored[k].strip()
        except Exception:
            pass
    return out


def save_settings(updates: dict) -> dict:
    settings = load_settings()
    for k in ("llm_base_url", "llm_model", "llm_api_key"):
        if k in updates and isinstance(updates[k], str):
            settings[k] = updates[k].strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings

# Extend PATH so subprocesses (yt-dlp's ffmpeg/node lookup) see Homebrew tools
# even when Flask is launched from a non-shell env (e.g. python.framework binary).
_PATH_EXTRA = ["/opt/homebrew/bin", "/usr/local/bin"]
SUBPROC_ENV = os.environ.copy()
SUBPROC_ENV["PATH"] = ":".join(
    [p for p in _PATH_EXTRA if p not in SUBPROC_ENV.get("PATH", "")]
    + [SUBPROC_ENV.get("PATH", "")]
).strip(":")

FFMPEG_BIN = shutil.which("ffmpeg", path=SUBPROC_ENV["PATH"]) or "ffmpeg"


def _ffmpeg_has_libass() -> bool:
    """Check whether ffmpeg was built with libass (needed for burn-in)."""
    try:
        r = subprocess.run([FFMPEG_BIN, "-hide_banner", "-filters"],
                           capture_output=True, text=True, env=SUBPROC_ENV, timeout=10)
        return " subtitles " in r.stdout
    except Exception:
        return False


HAS_LIBASS = _ffmpeg_has_libass()

app = Flask(__name__)

# --------------------------------------------------------------------------
# Job state — single-process in-memory
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    url: str
    start: float
    end: float
    model_size: str
    task: str           # "transcribe" or "translate"
    source: str = "auto"  # "auto" | "youtube" | "whisper"
    status: str = "queued"
    message: str = ""
    error: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "start": self.start,
            "end": self.end,
            "model_size": self.model_size,
            "task": self.task,
            "source": self.source,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "files": list(self.files.keys()),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        return cls(
            id=str(data["id"]),
            url=str(data.get("url") or ""),
            start=float(data.get("start", 0)),
            end=float(data.get("end", 0)),
            model_size=str(data.get("model_size") or "small"),
            task=str(data.get("task") or "translate"),
            source=str(data.get("source") or "auto"),
            status=str(data.get("status") or "queued"),
            message=str(data.get("message") or ""),
            error=data.get("error"),
            files=dict(data.get("files") or {}),
            created_at=float(data.get("created_at") or time.time()),
            completed_at=float(data["completed_at"]) if data.get("completed_at") is not None else None,
        )


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
JOB_QUEUE: Queue[str] = Queue(maxsize=MAX_QUEUED_JOBS)

# Whisper models are heavy to load — keep at most one in memory.
_MODEL_CACHE: dict[str, Any] = {"size": None, "model": None, "last_used": 0.0}
_MODEL_LOCK = threading.Lock()


def _jobs_path() -> Path:
    return DATA_DIR / "jobs.json"


def persist_jobs() -> None:
    with JOBS_LOCK:
        snapshot = [job.to_dict() for job in sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)]
    _jobs_path().write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def load_jobs() -> None:
    path = _jobs_path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, list):
        return

    interrupted_states = {"queued", "downloading", "subs", "transcribing", "translating", "burning"}
    loaded: dict[str, Job] = {}
    now = time.time()
    for item in raw:
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            job = Job.from_dict(item)
        except Exception:
            continue
        if job.status in interrupted_states:
            job.status = "error"
            job.error = "interrupted by restart"
            job.message = "interrupted by restart"
            job.completed_at = now
        loaded[job.id] = job

    with JOBS_LOCK:
        JOBS.clear()
        JOBS.update(loaded)
    prune_job_records()
    persist_jobs()


def _failed_jobs_path() -> Path:
    return DATA_DIR / "failed_jobs.json"


def record_failed_job(job: Job, error: str) -> None:
    history = []
    path = _failed_jobs_path()
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            history = []
    history.insert(0, {
        "id": job.id,
        "url": job.url,
        "start": job.start,
        "end": job.end,
        "source": job.source,
        "task": job.task,
        "model_size": job.model_size,
        "error": error,
        "created_at": job.created_at,
        "failed_at": time.time(),
    })
    path.write_text(json.dumps(history[:FAILED_HISTORY_LIMIT], indent=2), encoding="utf-8")


def cleanup_job_dir(job_id: str) -> None:
    job_dir = DATA_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def prune_old_jobs() -> None:
    clips = []
    for d in DATA_DIR.iterdir():
        if not d.is_dir():
            continue
        if (d / "subtitled.mp4").exists():
            clips.append((d.stat().st_mtime, d))
    clips.sort(key=lambda item: item[0], reverse=True)
    removed_ids = []
    for _, d in clips[MAX_SAVED_JOBS:]:
        removed_ids.append(d.name)
        shutil.rmtree(d, ignore_errors=True)
    if removed_ids:
        with JOBS_LOCK:
            for job_id in removed_ids:
                job = JOBS.get(job_id)
                if job is not None:
                    del JOBS[job_id]
        persist_jobs()


def prune_job_records() -> None:
    changed = False
    with JOBS_LOCK:
        completed = [(job.completed_at or 0.0, job_id) for job_id, job in JOBS.items()
                     if job.status in {"done", "error"}]
        completed.sort(reverse=True)
        keep_ids = {job_id for _, job_id in completed[:max(MAX_SAVED_JOBS, FAILED_HISTORY_LIMIT)]}
        for job_id in list(JOBS.keys()):
            job = JOBS[job_id]
            if job.status in {"done", "error"} and job_id not in keep_ids:
                del JOBS[job_id]
                changed = True
    if changed:
        persist_jobs()


def evict_model_if_idle(force: bool = False) -> None:
    with _MODEL_LOCK:
        model = _MODEL_CACHE.get("model")
        if model is None:
            return
        idle = time.time() - float(_MODEL_CACHE.get("last_used", 0.0))
        if force or idle >= MODEL_IDLE_EVICT_SECONDS:
            _MODEL_CACHE["model"] = None
            _MODEL_CACHE["size"] = None
            _MODEL_CACHE["last_used"] = 0.0
    if force or model is not None:
        gc.collect()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_timestamp(value: str | float | int) -> float:
    """Accept seconds (number) or HH:MM:SS / MM:SS / SS strings."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        if len(parts) == 2:
            m, sec = parts
            return m * 60 + sec
        if len(parts) == 3:
            h, m, sec = parts
            return h * 3600 + m * 60 + sec
        raise ValueError(f"bad timestamp: {value}")
    return float(s)


def fmt_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_SRT_CUE_RE = re.compile(
    r"(\d+)\s*\r?\n"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3}).*?\r?\n"
    r"(.*?)(?=\r?\n\r?\n|\Z)",
    re.DOTALL,
)


def _srt_time_to_sec(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def trim_srt(raw: str, start: float, end: float) -> str:
    """Keep only cues overlapping [start, end] and shift them to clip-relative time."""
    out_lines = []
    idx = 0
    for m in _SRT_CUE_RE.finditer(raw):
        cue_start = _srt_time_to_sec(m.group(2))
        cue_end = _srt_time_to_sec(m.group(3))
        if cue_end <= start or cue_start >= end:
            continue
        new_start = max(0.0, cue_start - start)
        new_end = max(new_start + 0.1, min(cue_end, end) - start)
        text = m.group(4).strip()
        if not text:
            continue
        idx += 1
        out_lines.append(str(idx))
        out_lines.append(f"{fmt_srt_time(new_start)} --> {fmt_srt_time(new_end)}")
        out_lines.append(text)
        out_lines.append("")
    return "\n".join(out_lines)


def smooth_srt(
    raw: str,
    *,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    min_duration: float = 1.2,
    min_gap: float = 0.04,
    merge_threshold: float = 0.4,
) -> str:
    """Clean up an SRT for stable burn-in.

    YouTube auto-gen subs come with overlapping cue ranges (rolling display
    effect) which look jittery when burned in. We:
      - merge cues whose starts are within `merge_threshold` seconds (same utterance)
      - de-dup repeated text fragments inside merged cues
      - force non-overlapping ranges with `min_gap`
      - enforce `min_duration` per cue
      - word-wrap text to at most `max_lines` lines of `max_chars_per_line`
    """
    import textwrap

    cues: list[list] = []  # [start, end, text]
    for m in _SRT_CUE_RE.finditer(raw):
        s = _srt_time_to_sec(m.group(2))
        e = _srt_time_to_sec(m.group(3))
        t = " ".join(m.group(4).split())
        if t:
            cues.append([s, e, t])
    if not cues:
        return raw

    cues.sort(key=lambda c: (c[0], c[1]))

    merged: list[list] = []
    for c in cues:
        if merged and (c[0] - merged[-1][0] < merge_threshold):
            prev = merged[-1]
            prev[1] = max(prev[1], c[1])
            # Append only the new tail (avoid duplicating accumulated text).
            if c[2] != prev[2] and c[2] not in prev[2]:
                if prev[2] in c[2]:
                    prev[2] = c[2]
                else:
                    prev[2] = f"{prev[2]} {c[2]}".strip()
        else:
            merged.append(list(c))

    # Enforce non-overlap by trimming each cue's end to the next cue's start.
    for i in range(len(merged) - 1):
        cap = merged[i + 1][0] - min_gap
        if merged[i][1] > cap:
            merged[i][1] = cap

    # Enforce min duration without re-introducing overlap.
    for i, cue in enumerate(merged):
        if cue[1] - cue[0] < min_duration:
            target = cue[0] + min_duration
            if i + 1 < len(merged):
                target = min(target, merged[i + 1][0] - min_gap)
            cue[1] = max(cue[1], target)

    # Split cues whose text won't fit in `max_lines` lines of `max_chars_per_line`.
    # Whisper often emits one cue per sentence; long sentences need multi-cue splits.
    char_budget = max_chars_per_line * max_lines
    split: list[list] = []
    for s, e, t in merged:
        if len(t) <= char_budget:
            split.append([s, e, t])
            continue
        words = t.split()
        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for w in words:
            add = len(w) + (1 if cur else 0)
            if cur_len + add > char_budget and cur:
                chunks.append(" ".join(cur))
                cur, cur_len = [w], len(w)
            else:
                cur.append(w)
                cur_len += add
        if cur:
            chunks.append(" ".join(cur))
        if len(chunks) <= 1:
            split.append([s, e, t])
            continue
        total_chars = sum(len(c) for c in chunks)
        cursor = s
        duration = e - s
        for j, c in enumerate(chunks):
            chunk_dur = duration * len(c) / total_chars
            chunk_end = e if j == len(chunks) - 1 else cursor + chunk_dur
            split.append([cursor, chunk_end, c])
            cursor = chunk_end
    merged = split

    out_lines: list[str] = []
    for idx, (s, e, text) in enumerate(merged, 1):
        wrapped = textwrap.wrap(
            text,
            width=max_chars_per_line,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if len(wrapped) > max_lines:
            # Re-flow into exactly max_lines by widening the wrap width.
            words = text.split()
            target_len = max(max_chars_per_line, (len(text) + max_lines - 1) // max_lines)
            wrapped = textwrap.wrap(text, width=target_len + 4, break_long_words=False)
            if len(wrapped) > max_lines:
                wrapped = wrapped[:max_lines]
                # Append truncated remainder to last line with ellipsis.
                wrapped[-1] = wrapped[-1].rstrip() + "…"
        out_lines.append(str(idx))
        out_lines.append(f"{fmt_srt_time(s)} --> {fmt_srt_time(e)}")
        out_lines.append("\n".join(wrapped) if wrapped else text)
        out_lines.append("")
    return "\n".join(out_lines)


def parse_yt_json3(data: dict) -> list[tuple[float, float, str]]:
    """Build clean, non-overlapping cues from a YouTube json3 subtitle file.

    json3 has three event kinds:
      - window setup (no segs) — skipped
      - content events (segs with text + word-level tOffsetMs)
      - append events (aAppend=1, only "\n") — the rolling-overlap markers,
        responsible for the flicker when SRT is naively burned in. Skipped.

    For content events, dDurationMs includes a long fade-out tail used by YT's
    player. We compute the real end as last-word-offset + estimated word
    duration, then cap each cue's end at the next cue's start (no overlap).
    """
    raw_cues: list[tuple[int, int, str]] = []  # ms, ms, text
    for event in data.get("events", []):
        segs = event.get("segs") or []
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if not text:
            continue  # pure "\n" append markers fall out here

        start_ms = int(event.get("tStartMs", 0))
        offsets = [int(s.get("tOffsetMs", 0)) for s in segs]
        last_offset = max(offsets) if offsets else 0
        stated_end_ms = start_ms + int(event.get("dDurationMs", 0) or 0)

        if last_offset > 0:
            # Word-level timing available — strip the fade-out tail. Last word
            # finishes ~one inter-word gap after its onset.
            gaps = [offsets[i] - offsets[i - 1] for i in range(1, len(offsets))
                    if offsets[i] > offsets[i - 1]]
            avg = sum(gaps) / len(gaps) if gaps else 500
            last_word_dur = max(280, min(900, int(avg)))
            speech_end_ms = start_ms + last_offset + last_word_dur
            end_ms = min(speech_end_ms, stated_end_ms) if stated_end_ms else speech_end_ms
        else:
            # No word offsets (sound marker, single phrase) — trust stated duration.
            end_ms = stated_end_ms or (start_ms + 1500)
        if end_ms <= start_ms:
            end_ms = start_ms + 800  # safety floor

        raw_cues.append((start_ms, end_ms, text))

    raw_cues.sort(key=lambda c: c[0])

    # Force non-overlap: trim each cue's end to (next start - tiny gap).
    GAP_MS = 40
    out: list[tuple[float, float, str]] = []
    for i, (s, e, t) in enumerate(raw_cues):
        if i + 1 < len(raw_cues):
            e = min(e, raw_cues[i + 1][0] - GAP_MS)
        if e <= s:
            continue
        out.append((s / 1000.0, e / 1000.0, t))
    return out


def fetch_youtube_subs_orig(url: str, start: float, end: float, out_srt: Path) -> tuple[bool, str]:
    """Fetch the ORIGINAL-language subs (not auto-translated) for LLM translation.

    Returns (success, lang_code). Prefers manual subs in source lang, then auto-gen
    original (-orig.srt), then any non-translated track.
    """
    tmpdir = out_srt.parent / "_yt_subs_orig"
    if tmpdir.exists():
        for p in tmpdir.iterdir():
            p.unlink()
    else:
        tmpdir.mkdir(parents=True)

    # Request json3 first (richer timing → cleaner cues for auto-gen subs),
    # fall back to srt/vtt for manual subs that don't come in json3.
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", ".*-orig,.*",
        "--sub-format", "json3/srt/vtt/best",
        "--extractor-args", "youtube:player_client=tv,web_safari,default",
        "--remote-components", "ejs:github",
        "-o", str(tmpdir / "subs.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV, timeout=120)

    def _lang_of(p: Path) -> str:
        # subs.<lang>.json3 / subs.<lang>.srt / subs.<lang>-orig.json3 etc.
        stem = p.stem
        return stem.split(".", 1)[1] if "." in stem else stem

    def _rank(p: Path) -> tuple[int, int, int, str]:
        lang = _lang_of(p)
        # Skip auto-translations like "en-de-DE"; -orig is fine.
        is_translated = "-" in lang and not lang.endswith("-orig")
        is_auto = lang.endswith("-orig")
        # Prefer json3 over srt/vtt (json3 = clean cues for auto-gen).
        prefer_json = 0 if p.suffix == ".json3" else 1
        return (int(is_translated), int(is_auto), prefer_json, lang)

    candidates = list(tmpdir.glob("*.json3")) + list(tmpdir.glob("*.srt")) + list(tmpdir.glob("*.vtt"))
    if not candidates:
        return False, ""
    candidates.sort(key=_rank)
    pick = candidates[0]
    lang = _lang_of(pick).removesuffix("-orig")

    if pick.suffix == ".json3":
        import json as _json
        try:
            data = _json.loads(pick.read_text(encoding="utf-8"))
            cues = parse_yt_json3(data)
        except Exception as e:
            print(f"json3 parse failed for {pick}: {e}", flush=True)
            cues = []
        if not cues:
            return False, ""
        # Trim to [start, end] and shift to clip-relative time.
        out_lines = []
        idx = 0
        for s, e, t in cues:
            if e <= start or s >= end:
                continue
            ns = max(0.0, s - start)
            ne = max(ns + 0.1, min(e, end) - start)
            idx += 1
            out_lines.append(str(idx))
            out_lines.append(f"{fmt_srt_time(ns)} --> {fmt_srt_time(ne)}")
            out_lines.append(t)
            out_lines.append("")
        if not out_lines:
            return False, ""
        out_srt.write_text("\n".join(out_lines), encoding="utf-8")
        return True, lang

    # Fallback: vtt or srt — parse via the existing trim_srt (works on both
    # since SRT and VTT cue blocks share enough syntax for our regex).
    raw = pick.read_text(encoding="utf-8", errors="replace")
    trimmed = trim_srt(raw, start, end)
    if not trimmed.strip():
        return False, ""
    out_srt.write_text(trimmed, encoding="utf-8")
    return True, lang


def fetch_youtube_subs(url: str, start: float, end: float, out_srt: Path) -> bool:
    """Try to download English subs (manual, auto-gen, or auto-translated) from YouTube.

    Writes a clip-relative SRT to out_srt and returns True on success.
    """
    tmpdir = out_srt.parent / "_yt_subs"
    if tmpdir.exists():
        for p in tmpdir.iterdir():
            p.unlink()
    else:
        tmpdir.mkdir(parents=True)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*,en",
        "--sub-format", "srt/vtt/best",
        "--convert-subs", "srt",
        "--extractor-args", "youtube:player_client=tv,web_safari,default",
        "--remote-components", "ejs:github",
        "-o", str(tmpdir / "subs.%(ext)s"),
        url,
    ]
    # Don't check return code: yt-dlp returns non-zero if ANY of the requested
    # sub variants 429s, but still writes the ones that succeeded.
    subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV, timeout=120)

    srts = list(tmpdir.glob("*.srt"))
    if not srts:
        return False

    # Prefer manual (non-orig, non-translated) > auto-orig > auto-translated.
    # yt-dlp filenames look like subs.en.srt / subs.en-orig.srt / subs.en-en.srt / subs.en-de-DE.srt.
    def _rank(p: Path) -> int:
        stem = p.stem  # e.g. "subs.en-orig"
        lang = stem.split(".", 1)[1] if "." in stem else stem
        if lang == "en":          return 0  # plain English (manual or auto)
        if lang == "en-orig":     return 1  # auto-gen original (English)
        if lang == "en-en":       return 2  # English auto-translated to English (no-op)
        return 3                              # auto-translated from foreign

    srts.sort(key=_rank)
    raw = srts[0].read_text(encoding="utf-8", errors="replace")
    trimmed = trim_srt(raw, start, end)
    if not trimmed.strip():
        return False
    out_srt.write_text(trimmed, encoding="utf-8")
    return True


def write_srt(segments, path: Path) -> None:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{fmt_srt_time(seg.start)} --> {fmt_srt_time(seg.end)}")
        lines.append((seg.text or "").strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


_LLM_CLIENT = None
_LLM_RESOLVED_MODEL: str | None = None
_LLM_CACHE_KEY: tuple[str, str] | None = None


def reset_llm_client() -> None:
    """Force the next get_llm_client() call to rebuild from current settings."""
    global _LLM_CLIENT, _LLM_RESOLVED_MODEL, _LLM_CACHE_KEY
    _LLM_CLIENT = None
    _LLM_RESOLVED_MODEL = None
    _LLM_CACHE_KEY = None


def get_llm_client():
    """Build (and cache) an OpenAI client from current settings."""
    global _LLM_CLIENT, _LLM_RESOLVED_MODEL, _LLM_CACHE_KEY
    s = load_settings()
    cache_key = (s["llm_base_url"], s["llm_api_key"])
    if _LLM_CLIENT is None or _LLM_CACHE_KEY != cache_key:
        from openai import OpenAI
        _LLM_CLIENT = OpenAI(base_url=s["llm_base_url"], api_key=s["llm_api_key"] or "x")
        _LLM_CACHE_KEY = cache_key
        _LLM_RESOLVED_MODEL = None

    if _LLM_RESOLVED_MODEL is None or (s["llm_model"] and _LLM_RESOLVED_MODEL != s["llm_model"]):
        if s["llm_model"]:
            _LLM_RESOLVED_MODEL = s["llm_model"]
        else:
            try:
                models = _LLM_CLIENT.models.list()
                _LLM_RESOLVED_MODEL = models.data[0].id if models.data else "local-model"
            except Exception:
                _LLM_RESOLVED_MODEL = "local-model"
    return _LLM_CLIENT, _LLM_RESOLVED_MODEL


_GARBAGE_RE = re.compile(r"^[\s\[\]\{\}\(\),:;\"'`/\\|<>=+*&^%$#@!~]*$")


def _looks_like_garbage(s: str) -> bool:
    """True if a 'translated' string is clearly LLM noise (brackets, punctuation only)."""
    s = (s or "").strip()
    if not s:
        return True
    if _GARBAGE_RE.match(s):
        return True
    # Repeating bracket patterns like "] }", "} ]", "[ ] }", etc.
    stripped = re.sub(r"\s+", "", s)
    if stripped and re.fullmatch(r"[\[\]\{\}]+", stripped):
        return True
    return False


def _llm_translate_one(client, model: str, text: str, src_lang: str) -> str:
    """Translate a single line via plain-text prompt. Returns original on failure."""
    sys_p = (
        "You translate subtitles to natural, fluent English. Output ONLY the "
        "translated line itself — no quotes, no JSON, no commentary, no source text."
    )
    user_p = f"Source language: {src_lang or 'unknown'}.\nTranslate this subtitle line:\n{text}"
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys_p},
                      {"role": "user",   "content": user_p}],
            temperature=0.2,
            max_tokens=400,
        )
        out = (resp.choices[0].message.content or "").strip()
        # Strip <think> blocks and wrapping quotes some models add.
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()
        out = out.strip('"\'`').strip()
        # Take only the first non-empty line in case the model added explanation.
        first = next((ln for ln in out.splitlines() if ln.strip()), "")
        if not first or _looks_like_garbage(first):
            return text
        return first.strip()
    except Exception:
        return text


def llm_translate_srt(srt_path: Path, src_lang: str, batch: int = 20) -> None:
    """Translate the text of every cue in srt_path to English in-place via LLM."""
    import json as _json
    raw = srt_path.read_text(encoding="utf-8")
    cues = list(_SRT_CUE_RE.finditer(raw))
    if not cues:
        return

    client, model = get_llm_client()
    out_lines: list[str] = []

    for chunk_start in range(0, len(cues), batch):
        chunk = cues[chunk_start:chunk_start + batch]
        texts = [c.group(4).strip().replace("\n", " ") for c in chunk]

        sys_prompt = (
            "You translate subtitles to natural, fluent English suitable for spoken dialogue. "
            "Output a JSON object of the form {\"lines\": [...]} where lines is an array of "
            "translated strings, the same length and order as the input. Translate each input "
            "line to one output line — do not merge or split. Keep it concise and readable. "
            "No commentary, no extra fields."
        )
        user_prompt = (
            f"Source language: {src_lang or 'unknown'}. Translate to English.\n\n"
            f"Input lines (JSON array, {len(texts)} items):\n"
            f"{_json.dumps(texts, ensure_ascii=False)}\n\n"
            f"Reply with a JSON object {{\"lines\": [...]}} containing exactly "
            f"{len(texts)} translated strings, in the same order. Do not include "
            f"any of the source text, instructions, or meta-commentary in the output."
        )

        # response_format forces structured JSON; LM Studio supports json_object.
        # Try with response_format first, fall back without it for endpoints that don't.
        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=8192,
        )
        # No min/maxItems: the model may legitimately collapse near-identical
        # rolling cues. We handle count mismatch ourselves (retry per line below).
        json_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "translation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "lines": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["lines"],
                    "additionalProperties": False,
                },
            },
        }
        try:
            resp = client.chat.completions.create(response_format=json_schema, **kwargs)
        except Exception:
            # Endpoint may not support structured outputs — retry without.
            resp = client.chat.completions.create(**kwargs)

        content = resp.choices[0].message.content or ""
        finish = resp.choices[0].finish_reason
        translated = _parse_llm_array(content, expected=len(texts))
        if len(translated) != len(texts):
            print(
                f"--- LLM translate parse failed (got {len(translated)}, expected {len(texts)}, "
                f"finish={finish}) ---\n{content[:1500]}\n--- end ---",
                flush=True,
            )
            translated = (translated + texts)[:len(texts)]

        # Retry any garbage/empty lines individually with a simple text prompt.
        retried = 0
        for i, t in enumerate(translated):
            if _looks_like_garbage(t):
                translated[i] = _llm_translate_one(client, model, texts[i], src_lang)
                retried += 1
        if retried:
            print(
                f"LLM batch {chunk_start}-{chunk_start + len(chunk)}: "
                f"retried {retried}/{len(texts)} per-line (finish={finish})",
                flush=True,
            )

        for orig_match, new_text in zip(chunk, translated):
            idx = len(out_lines) // 4 + 1  # 4 lines per cue (num, time, text, blank)
            out_lines.append(str(idx))
            out_lines.append(f"{orig_match.group(2)} --> {orig_match.group(3)}")
            out_lines.append(new_text.strip() or orig_match.group(4).strip())
            out_lines.append("")

    srt_path.write_text("\n".join(out_lines), encoding="utf-8")


def _parse_llm_array(content: str, expected: int) -> list[str]:
    """Best-effort extraction of translated lines from an LLM response.

    Accepts {"lines": [...]} (preferred), bare [...] arrays, or fenced JSON.
    Strips qwen-style <think>...</think> blocks first.
    """
    import json as _json
    s = content.strip()
    # Strip explicit thinking blocks some models emit.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    # Strip fenced code if present.
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)

    # Try parsing as a JSON object {"lines": [...]} or {"translations": [...]}.
    try:
        obj = _json.loads(s)
        if isinstance(obj, dict):
            for key in ("lines", "translations", "output", "result"):
                v = obj.get(key)
                if isinstance(v, list):
                    return [str(x) for x in v]
        if isinstance(obj, list):
            return [str(x) for x in obj]
    except Exception:
        pass

    # Locate the JSON object/array by matching balanced braces from the LAST
    # `{` or `[` (LLMs often dump thinking before the answer).
    for opener, closer in (("{", "}"), ("[", "]")):
        last_open = s.rfind(opener)
        if last_open == -1:
            continue
        depth = 0
        for i in range(last_open, len(s)):
            ch = s[i]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = s[last_open:i + 1]
                    try:
                        obj = _json.loads(candidate)
                        if isinstance(obj, dict):
                            for key in ("lines", "translations", "output", "result"):
                                v = obj.get(key)
                                if isinstance(v, list):
                                    return [str(x) for x in v]
                        elif isinstance(obj, list):
                            return [str(x) for x in obj]
                    except Exception:
                        pass
                    break
    return []


def get_whisper_model(size: str):
    from faster_whisper import WhisperModel
    evict_model_if_idle()
    with _MODEL_LOCK:
        if _MODEL_CACHE["model"] is not None and _MODEL_CACHE["size"] != size:
            _MODEL_CACHE["model"] = None
            _MODEL_CACHE["size"] = None
            gc.collect()
        if _MODEL_CACHE["model"] is None:
            # int8 on CPU is the universally-fast default; works on Apple Silicon too.
            _MODEL_CACHE["model"] = WhisperModel(
                size,
                device="cpu",
                compute_type="int8",
                download_root=str(MODEL_CACHE),
            )
            _MODEL_CACHE["size"] = size
        _MODEL_CACHE["last_used"] = time.time()
        return _MODEL_CACHE["model"]


def update(job: Job, status: str, message: str = "") -> None:
    with JOBS_LOCK:
        job.status = status
        job.message = message
        if status in {"done", "error"}:
            job.completed_at = time.time()
    persist_jobs()
    if status in {"done", "error"}:
        prune_job_records()


def _job_worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is not None:
                run_job(job)
        finally:
            JOB_QUEUE.task_done()


load_jobs()
threading.Thread(target=_job_worker, daemon=True).start()


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def run_yt_dlp(job: Job, out_path: Path) -> None:
    """Fetch only the requested time range. Re-encodes at cut points for clean boundaries."""
    section = f"*{job.start:.2f}-{job.end:.2f}"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--download-sections", section,
        "--force-keyframes-at-cuts",
        "--ffmpeg-location", FFMPEG_BIN,
        # TV + web_safari clients sidestep YouTube's n-challenge for many videos.
        "--extractor-args", "youtube:player_client=tv,web_safari,default",
        # If the n-challenge IS still needed, allow yt-dlp to fetch its solver
        # script from GitHub (one-time auto-download, then cached).
        "--remote-components", "ejs:github",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(out_path),
    ]
    if COOKIES_FROM_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    cmd.append(job.url)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV)
    if proc.returncode != 0:
        # Print full stderr to server log so we can see what's actually wrong.
        print("--- yt-dlp stderr ---\n" + proc.stderr + "\n--- end ---", flush=True)
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()[-1500:]}")
    if not out_path.exists():
        # yt-dlp may pick a different extension despite -o; find the produced file.
        produced = list(out_path.parent.glob(f"{out_path.stem}.*"))
        if not produced:
            raise RuntimeError("yt-dlp produced no output file")
        produced[0].rename(out_path)


def transcribe(job: Job, clip_path: Path, srt_path: Path) -> None:
    model = get_whisper_model(job.model_size)
    segments, info = model.transcribe(
        str(clip_path),
        task=job.task,            # "translate" → English; "transcribe" → original lang
        vad_filter=True,
        beam_size=5,
    )
    # The generator is lazy; materialize so we can iterate twice if needed.
    seg_list = list(segments)
    if not seg_list:
        # Still write an empty SRT so downstream burn step doesn't blow up.
        srt_path.write_text("", encoding="utf-8")
        return
    write_srt(seg_list, srt_path)
    job.message = f"detected lang={info.language} ({info.language_probability:.2f}); {len(seg_list)} segments"


def _probe_video_size(path: Path) -> tuple[int, int]:
    """Return (width, height) of the video, or (1920, 1080) on failure."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, env=SUBPROC_ENV, timeout=10,
        )
        w_s, h_s = proc.stdout.strip().split("x")
        return int(w_s), int(h_s)
    except Exception:
        return 1920, 1080


def burn_subs(clip_path: Path, srt_path: Path, out_path: Path) -> str:
    """Produce a subtitled mp4. Returns 'burned' or 'soft' depending on what worked.

    Burn-in requires ffmpeg built with libass. If unavailable (common with the
    minimal homebrew formula), fall back to a soft mov_text track so the result
    is still a single playable mp4 — viewers just toggle CC instead of seeing
    them automatically.
    """
    if srt_path.stat().st_size == 0:
        cmd = [FFMPEG_BIN, "-y", "-i", str(clip_path), "-c", "copy", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg copy failed: {proc.stderr.strip()[-500:]}")
        return "none"

    if HAS_LIBASS:
        # Run from the SRT's directory so the filename is bare — sidesteps all
        # the filtergraph path-escaping pitfalls (colons, quotes, spaces).
        cwd = str(srt_path.parent)
        srt_name = srt_path.name
        # Probe the actual video size and tell libass to size text relative to
        # it. Without original_size, libass uses its 384x288 default PlayRes
        # so FontSize=22 balloons to ~80px on a 1080p video.
        vw, vh = _probe_video_size(clip_path)
        # Font ~3.5% of video height: 38 at 1080p, 25 at 720p, 50 at 1440p.
        font_size = max(18, int(vh * 0.035))
        outline = max(2, int(vh * 0.003))
        margin_v = max(30, int(vh * 0.06))
        style = (
            rf"FontName=Helvetica\,FontSize={font_size}\,"
            r"PrimaryColour=&H00FFFFFF\,OutlineColour=&H00000000\,"
            rf"BorderStyle=1\,Outline={outline}\,Shadow=0\,"
            rf"MarginV={margin_v}\,WrapStyle=2"
        )
        vf = f"subtitles=f={srt_name}:original_size={vw}x{vh}:force_style={style}"
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(clip_path),
            "-vf", vf,
            "-c:a", "copy",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV, cwd=cwd)
        if proc.returncode == 0:
            return "burned"
        # Fall through to soft-sub if burn unexpectedly fails.

    # Soft-subtitle fallback: mux the .srt as a mov_text track inside the mp4.
    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(clip_path),
        "-i", str(srt_path),
        "-c:v", "copy", "-c:a", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg sub-mux failed: {proc.stderr.strip()[-500:]}")
    return "soft"


def run_job(job: Job) -> None:
    job_dir = DATA_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    clip_path = job_dir / "clip.mp4"
    srt_path = job_dir / "subs.srt"
    subbed_path = job_dir / "subtitled.mp4"

    # Persist job metadata so the Re-crop tab can show what each clip came from.
    try:
        (job_dir / "meta.json").write_text(json.dumps({
            "url": job.url,
            "start": job.start,
            "end": job.end,
            "source": job.source,
            "task": job.task,
            "model_size": job.model_size,
            "created_at": job.created_at,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass

    try:
        update(job, "downloading", "fetching clip from YouTube")
        run_yt_dlp(job, clip_path)
        job.files["clip.mp4"] = str(clip_path)

        got_subs = False

        if job.source == "llm":
            update(job, "subs", "fetching original-language subs from YouTube")
            ok, lang = fetch_youtube_subs_orig(job.url, job.start, job.end, srt_path)
            if not ok:
                raise RuntimeError("no original-language subs available on YouTube; try Whisper")
            # Collapse YT's rolling overlap BEFORE the LLM sees it, so the model
            # translates discrete utterances and isn't tempted to dedup/pad.
            try:
                pre = smooth_srt(
                    srt_path.read_text(encoding="utf-8"),
                    max_chars_per_line=120, max_lines=99,  # don't wrap yet, just merge
                )
                if pre.strip():
                    srt_path.write_text(pre, encoding="utf-8")
            except Exception as e:
                print(f"pre-translate smooth failed (non-fatal): {e}", flush=True)
            _, llm_model_id = get_llm_client()
            update(job, "translating", f"translating {lang or 'subs'} → English via {llm_model_id}")
            llm_translate_srt(srt_path, src_lang=lang)
            job.message = f"YouTube subs ({lang}) translated by {llm_model_id}"
            got_subs = True

        elif job.source in ("auto", "youtube"):
            update(job, "subs", "fetching subtitles from YouTube")
            try:
                got_subs = fetch_youtube_subs(job.url, job.start, job.end, srt_path)
            except Exception as e:
                got_subs = False
                if job.source == "youtube":
                    raise RuntimeError(f"YouTube subs fetch failed: {e}") from e

            if got_subs:
                job.message = "subs from YouTube"
            elif job.source == "youtube":
                raise RuntimeError("no English subs available on YouTube for this video")

        if not got_subs:
            verb = "translating to English" if job.task == "translate" else "transcribing"
            update(job, "transcribing", f"{verb} with whisper-{job.model_size}")
            transcribe(job, clip_path, srt_path)

        # Smooth out overlapping cues + word-wrap based on the actual video
        # width so subs fit on-screen instead of overflowing.
        if srt_path.exists() and srt_path.stat().st_size > 0:
            try:
                vw, vh = _probe_video_size(clip_path)
                font_size = max(18, int(vh * 0.035))
                # Approx char width ≈ 0.55 * font_size; subtract margin for safety.
                usable = max(160, vw - 40)
                max_chars = max(20, int(usable / (font_size * 0.55)))
                cleaned = smooth_srt(
                    srt_path.read_text(encoding="utf-8"),
                    max_chars_per_line=max_chars,
                )
                if cleaned.strip():
                    srt_path.write_text(cleaned, encoding="utf-8")
            except Exception as e:
                print(f"smooth_srt failed (non-fatal): {e}", flush=True)

        job.files["subs.srt"] = str(srt_path)

        verb = "burning subtitles into video" if HAS_LIBASS else "muxing soft subtitles (libass missing)"
        update(job, "burning", verb)
        mode = burn_subs(clip_path, srt_path, subbed_path)
        job.files["subtitled.mp4"] = str(subbed_path)

        suffix = {
            "burned": "subtitles burned into video",
            "soft":   "soft subs muxed (toggle CC in player) — install libass-enabled ffmpeg for burn-in",
            "none":   "no speech detected",
        }.get(mode, "ready")
        update(job, "done", f"{job.message + '; ' if job.message else ''}{suffix}")
        prune_old_jobs()
    except Exception as e:
        job.error = str(e)
        cleanup_job_dir(job.id)
        record_failed_job(job, str(e))
        update(job, "error", str(e))
    finally:
        evict_model_if_idle()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,})"
)

SAFE_ID_RE = re.compile(r'^[a-f0-9]{12}$')

def _validate_id(id_str: str) -> str:
    if not SAFE_ID_RE.match(id_str):
        abort(404)
    return id_str


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/resolve")
def resolve():
    """Extract video id + duration so the UI can embed and bound the time inputs."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400
    m = YT_ID_RE.search(url)
    if not m:
        return jsonify({"error": "not a recognizable YouTube URL"}), 400
    video_id = m.group(1)
    # Probe duration via yt-dlp without downloading.
    try:
        probe_cmd = ["yt-dlp", "--no-playlist", "--print", "%(duration)s\n%(title)s", "--skip-download"]
        if COOKIES_FROM_BROWSER:
            probe_cmd += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
        probe_cmd.append(url)
        proc = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30, env=SUBPROC_ENV)
        duration, title = 0.0, ""
        if proc.returncode == 0:
            parts = proc.stdout.strip().splitlines()
            if parts and parts[0].isdigit():
                duration = float(parts[0])
            if len(parts) > 1:
                title = parts[1]
    except Exception:
        duration, title = 0.0, ""
    return jsonify({"video_id": video_id, "duration": duration, "title": title})


@app.post("/api/clip")
def create_clip():
    data = request.get_json(force=True, silent=True) or {}
    try:
        url = (data.get("url") or "").strip()
        if not url:
            raise ValueError("missing url")
        start = parse_timestamp(data.get("start", 0))
        end = parse_timestamp(data.get("end", 0))
        if end <= start:
            raise ValueError("end must be greater than start")
        if end - start > MAX_JOB_SECONDS:
            raise ValueError("clip cannot exceed 30 minutes")
        model_size = (data.get("model_size") or "small").strip()
        if model_size not in VALID_MODELS:
            raise ValueError(f"model_size must be one of {sorted(VALID_MODELS)}")
        task = (data.get("task") or "translate").strip()
        if task not in ("translate", "transcribe"):
            raise ValueError("task must be 'translate' or 'transcribe'")
        source = (data.get("source") or "auto").strip()
        if source not in ("auto", "youtube", "whisper", "llm"):
            raise ValueError("source must be 'auto', 'youtube', 'whisper', or 'llm'")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job = Job(
        id=uuid.uuid4().hex[:12],
        url=url,
        start=start,
        end=end,
        model_size=model_size,
        task=task,
        source=source,
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
    persist_jobs()
    if JOB_QUEUE.full():
        with JOBS_LOCK:
            JOBS.pop(job.id, None)
        persist_jobs()
        return jsonify({"error": f"queue is full ({MAX_QUEUED_JOBS} jobs max); wait for current work to finish"}), 503
    JOB_QUEUE.put(job.id)
    return jsonify(job.to_dict()), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    _validate_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(job.to_dict())


@app.get("/api/jobs/<job_id>/file/<name>")
def job_file(job_id: str, name: str):
    _validate_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    path = job.files.get(name)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Settings — OpenAI-compatible endpoint config, persisted to data/settings.json.
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return jsonify(load_settings())


@app.post("/api/settings")
def post_settings():
    data = request.get_json(force=True, silent=True) or {}
    saved = save_settings(data)
    reset_llm_client()
    return jsonify(saved)


@app.post("/api/settings/test")
def test_settings():
    """Hit the configured endpoint's /models to verify connectivity."""
    data = request.get_json(force=True, silent=True) or {}
    settings = load_settings()
    for key in ("llm_base_url", "llm_model", "llm_api_key"):
        if isinstance(data.get(key), str):
            settings[key] = data[key].strip()
    try:
        from openai import OpenAI
        client = OpenAI(base_url=settings["llm_base_url"], api_key=settings["llm_api_key"] or "x")
        model = settings["llm_model"].strip() if settings["llm_model"].strip() else None
        ms = client.models.list()
        ids = [m.id for m in (ms.data or [])][:25]
        if not model:
            model = ids[0] if ids else "local-model"
        return jsonify({"ok": True, "selected": model, "available": ids})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ---------------------------------------------------------------------------
# Re-crop: trim an already-subtitled mp4 to a sub-range without re-translating.
# ---------------------------------------------------------------------------

def _list_burned_clips() -> list[dict]:
    """Return all jobs that have a subtitled.mp4, newest first."""
    import json as _json
    out = []
    for d in DATA_DIR.iterdir():
        if not d.is_dir():
            continue
        sub = d / "subtitled.mp4"
        if not sub.exists():
            continue
        try:
            duration = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(sub)],
                capture_output=True, text=True, env=SUBPROC_ENV, timeout=5,
            ).stdout.strip() or 0)
        except Exception:
            duration = 0
        meta = {}
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        out.append({
            "id": d.name,
            "mtime": sub.stat().st_mtime,
            "size": sub.stat().st_size,
            "duration": duration,
            "has_recrop": (d / "recrop.mp4").exists(),
            "url": meta.get("url"),
            "start": meta.get("start"),
            "end": meta.get("end"),
            "source": meta.get("source"),
        })
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out


@app.get("/api/clips")
def list_clips():
    return jsonify({"clips": _list_burned_clips()})


@app.get("/api/clips/<clip_id>/video")
def get_clip_video(clip_id: str):
    """Serve the subtitled.mp4 inline so it can play in the HTML5 player."""
    _validate_id(clip_id)
    p = DATA_DIR / clip_id / "subtitled.mp4"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="video/mp4", conditional=True)


@app.get("/api/clips/<clip_id>/recrop")
def get_recrop(clip_id: str):
    _validate_id(clip_id)
    p = DATA_DIR / clip_id / "recrop.mp4"
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True, download_name=f"{clip_id}-recrop.mp4")


@app.post("/api/recrop")
def recrop():
    data = request.get_json(force=True, silent=True) or {}
    clip_id = (data.get("clip_id") or "").strip()
    if not SAFE_ID_RE.match(clip_id):
        return jsonify({"error": "bad clip_id"}), 400
    src = DATA_DIR / clip_id / "subtitled.mp4"
    if not src.exists():
        return jsonify({"error": "subtitled.mp4 not found"}), 404
    try:
        start = parse_timestamp(data.get("start", 0))
        end = parse_timestamp(data.get("end", 0))
        if end <= start:
            raise ValueError("end must be greater than start")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dst = src.parent / "recrop.mp4"
    # Re-encode for accurate cut points (subs are already burned in, fast preset).
    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(src),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROC_ENV, timeout=300)
    if proc.returncode != 0:
        return jsonify({"error": f"ffmpeg failed: {proc.stderr.strip()[-400:]}"}), 500
    return jsonify({
        "ok": True,
        "clip_id": clip_id,
        "duration": end - start,
        "size": dst.stat().st_size,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=LISTEN_PORT, debug=False, threaded=True)
