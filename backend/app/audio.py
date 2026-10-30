"""Audio pipeline: split recordings with ffmpeg and transcribe chunks with Groq Whisper."""
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from groq import Groq
from pydub import AudioSegment

from . import config
from .utils import with_retry

SEARCH_WINDOW_MS = 15_000
STEP_MS = 250


def _quietest_point(audio: AudioSegment, target_ms: int) -> int:
    lo = max(0, target_ms - SEARCH_WINDOW_MS)
    hi = min(len(audio) - STEP_MS, target_ms + SEARCH_WINDOW_MS)
    if hi <= lo:
        return min(target_ms, len(audio))
    best, best_db = target_ms, float("inf")
    for position in range(lo, hi, STEP_MS):
        db = audio[position : position + STEP_MS].dBFS
        if db < best_db:
            best, best_db = position, db
    return best


def split_audio(path: Path, chunk_minutes: float = config.CHUNK_MINUTES) -> list[AudioSegment]:
    """Compatibility helper for callers that need in-memory audio chunks."""
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


def _split_audio(path: Path, output_dir: Path, chunk_minutes: int) -> list[tuple[Path, float]]:
    """Convert audio in bounded-memory chunks and return each chunk with its duration."""
    pattern = output_dir / "part_%04d.mp3"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_minutes * 60),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mp3",
            str(pattern),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip()[-1000:]
        raise RuntimeError(f"Could not prepare audio with ffmpeg: {detail or 'unknown ffmpeg error'}")

    chunks = sorted(output_dir.glob("part_*.mp3"))
    if not chunks:
        raise RuntimeError("Could not prepare audio: ffmpeg produced no audio chunks.")

    with_durations = []
    for chunk in chunks:
        try:
            duration_text = subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(chunk),
                ],
                text=True,
                stderr=subprocess.STDOUT,
            ).strip()
            duration = float(duration_text)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise RuntimeError(f"Could not read duration for audio chunk {chunk.name}: {exc}") from exc
        if duration <= 0:
            raise RuntimeError(f"Audio chunk {chunk.name} has an invalid duration.")
        with_durations.append((chunk, duration))
    return with_durations


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
        raise RuntimeError("GROQ_API_KEY is not set. Add it to your environment.")
    client = Groq(api_key=config.GROQ_API_KEY)

    if progress:
        progress("Preparing audio")

    with tempfile.TemporaryDirectory(prefix="notesgraph-audio-") as temp_dir:
        chunks = _split_audio(path, Path(temp_dir), config.CHUNK_MINUTES)
        parts: list[str] = []
        segments: list[dict] = []
        offset_seconds = 0.0
        for i, (chunk_path, duration) in enumerate(chunks, start=1):
            if progress:
                progress(f"Transcribing part {i} of {len(chunks)}")
            data = chunk_path.read_bytes()
            tail = parts[-1][-300:] if parts else ""
            prompt = f"{config.WHISPER_HINT} {tail}".strip()[-800:]
            kwargs = dict(
                file=(chunk_path.name, data),
                model=config.WHISPER_MODEL,
                prompt=prompt,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                temperature=0.0,
            )
            if language:
                kwargs["language"] = language
            resp = with_retry(
                lambda: client.audio.transcriptions.create(**kwargs),
                tries=5,
                base_delay=5.0,
            )
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
            offset_seconds += duration

    return "\n\n".join(part for part in parts if part), segments
