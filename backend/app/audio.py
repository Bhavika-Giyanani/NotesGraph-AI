"""Audio pipeline: split a lecture with pydub, transcribe each chunk with Groq Whisper."""
import io
from pathlib import Path
from typing import Callable, Optional

from groq import Groq
from pydub import AudioSegment

from . import config
from .utils import with_retry

SEARCH_WINDOW_MS = 15_000  # look +/- 15 s around the target cut for a quiet moment
STEP_MS = 250


def _quietest_point(audio: AudioSegment, target_ms: int) -> int:
    """Return a cut position near target_ms where the audio is quietest (avoids cutting mid-word)."""
    lo = max(0, target_ms - SEARCH_WINDOW_MS)
    hi = min(len(audio) - STEP_MS, target_ms + SEARCH_WINDOW_MS)
    if hi <= lo:
        return min(target_ms, len(audio))
    best, best_db = target_ms, float("inf")
    for t in range(lo, hi, STEP_MS):
        db = audio[t : t + STEP_MS].dBFS
        if db < best_db:
            best, best_db = t, db
    return best


def split_audio(path: Path, chunk_minutes: int = config.CHUNK_MINUTES) -> list:
    """Decode to 16 kHz mono (small in memory) and cut into ~chunk_minutes pieces at quiet points."""
    audio = AudioSegment.from_file(str(path), parameters=["-ac", "1", "-ar", "16000"])
    chunk_ms = int(chunk_minutes * 60 * 1000)
    if len(audio) <= chunk_ms * 1.2:
        return [audio]
    chunks, start = [], 0
    while start < len(audio):
        if len(audio) - start <= chunk_ms * 1.2:
            end = len(audio)
        else:
            end = _quietest_point(audio, start + chunk_ms)
        chunks.append(audio[start:end])
        start = end
    return chunks


def _to_mp3_bytes(chunk: AudioSegment) -> bytes:
    buf = io.BytesIO()
    chunk.export(buf, format="mp3", bitrate="64k")
    return buf.getvalue()


def transcribe_audio(
    path: Path,
    language: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> str:
    """Return the full transcript of an audio file."""
    text, _ = transcribe_audio_with_segments(path, language, progress)
    return text


def transcribe_audio_with_segments(
    path: Path,
    language: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[str, list[dict]]:
    """Return transcript text and audio-aligned Whisper segments."""
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to your .env file.")
    client = Groq(api_key=config.GROQ_API_KEY)

    if progress:
        progress("Preparing audio")
    chunks = split_audio(path)

    parts: list = []
    segments: list[dict] = []
    offset_seconds = 0.0
    for i, chunk in enumerate(chunks, start=1):
        if progress:
            progress(f"Transcribing part {i} of {len(chunks)}")
        data = _to_mp3_bytes(chunk)
        # Whisper's prompt = style hint + tail of the previous part, for continuity across cuts
        tail = parts[-1][-300:] if parts else ""
        prompt = f"{config.WHISPER_HINT} {tail}".strip()[-800:]
        kwargs = dict(
            file=(f"part_{i}.mp3", data),
            model=config.WHISPER_MODEL,
            prompt=prompt,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
            temperature=0.0,
        )
        if language:
            kwargs["language"] = language
        resp = with_retry(lambda: client.audio.transcriptions.create(**kwargs), tries=5, base_delay=5.0)
        parts.append((resp.text or "").strip())
        for segment in getattr(resp, "segments", None) or []:
            start = segment.get("start") if isinstance(segment, dict) else segment.start
            end = segment.get("end") if isinstance(segment, dict) else segment.end
            text = segment.get("text") if isinstance(segment, dict) else segment.text
            segments.append(
                {
                    "start": round(offset_seconds + float(start), 3),
                    "end": round(offset_seconds + float(end), 3),
                    "text": (text or "").strip(),
                }
            )
        offset_seconds += len(chunk) / 1000

    return "\n\n".join(p for p in parts if p), segments
