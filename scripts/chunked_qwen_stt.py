#!/usr/bin/env python3
"""Chunk long audio before calling the existing localhost Qwen3-ASR service.

Hermes command-provider contract:
    chunked_qwen_stt.py INPUT_AUDIO OUTPUT_TEXT

Short recordings are sent to Qwen unchanged. Long recordings are rendered as
slightly overlapping mono 16 kHz WAV chunks, transcribed sequentially, and
merged conservatively: duplicate boundary words are removed only when an exact
normalized overlap is found. If overlap is uncertain, both texts are retained.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

DEFAULT_ENDPOINT = "http://127.0.0.1:8127/transcribe"
DEFAULT_CHUNK_SECONDS = 180.0
DEFAULT_OVERLAP_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 300.0
MINIMUM_OVERLAP_WORDS = 3
MIN_CHUNK_SECONDS = 30.0
MAX_CHUNK_SECONDS = 300.0
MAX_OVERLAP_SECONDS = 30.0
MAX_AUDIO_SECONDS = 6 * 60 * 60
MAX_CHUNKS = 720
MIN_STEP_SECONDS = 1.0


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    chunk_count: int
    duration_seconds: float


def plan_chunks(
    duration_seconds: float,
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> list[tuple[float, float]]:
    if not math.isfinite(duration_seconds):
        raise ValueError("duration must be finite")
    if not math.isfinite(chunk_seconds):
        raise ValueError("chunk duration must be finite")
    if not math.isfinite(overlap_seconds):
        raise ValueError("overlap must be finite")
    if duration_seconds < 0:
        raise ValueError("duration must be non-negative")
    if duration_seconds > MAX_AUDIO_SECONDS:
        raise ValueError(f"duration exceeds the {MAX_AUDIO_SECONDS}-second safety bound")
    if chunk_seconds < MIN_CHUNK_SECONDS or chunk_seconds > MAX_CHUNK_SECONDS:
        raise ValueError(
            f"chunk duration must be between {MIN_CHUNK_SECONDS} and {MAX_CHUNK_SECONDS} seconds"
        )
    if overlap_seconds < 0 or overlap_seconds > MAX_OVERLAP_SECONDS:
        raise ValueError(
            f"overlap must be between 0 and {MAX_OVERLAP_SECONDS} seconds"
        )
    if overlap_seconds >= chunk_seconds:
        raise ValueError("overlap must be smaller than chunk duration")
    if duration_seconds <= chunk_seconds:
        return [(0.0, duration_seconds)]

    step = chunk_seconds - overlap_seconds
    if step < MIN_STEP_SECONDS:
        raise ValueError(
            f"chunk step must be at least {MIN_STEP_SECONDS} second"
        )
    expected_chunks = 1 + math.ceil((duration_seconds - chunk_seconds) / step)
    if expected_chunks > MAX_CHUNKS:
        raise ValueError(f"chunk plan exceeds the {MAX_CHUNKS}-chunk safety bound")

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_seconds:
        end = min(start + chunk_seconds, duration_seconds)
        chunks.append((round(start, 6), round(end, 6)))
        if end >= duration_seconds:
            break
        start += step
    return chunks


def _words(text: str) -> list[str]:
    return text.strip().split()


def _normalized_word(word: str) -> str:
    return "".join(char for char in word.casefold() if char.isalnum())


def _overlap_size(
    left_words: list[str],
    right_words: list[str],
    *,
    minimum_overlap_words: int,
    maximum_overlap_words: int = 80,
) -> int:
    limit = min(len(left_words), len(right_words), maximum_overlap_words)
    left_normalized = [_normalized_word(word) for word in left_words]
    right_normalized = [_normalized_word(word) for word in right_words]
    for size in range(limit, minimum_overlap_words - 1, -1):
        left_slice = left_normalized[-size:]
        right_slice = right_normalized[:size]
        if all(left_slice) and left_slice == right_slice:
            return size
    return 0


def merge_transcripts(
    transcripts: Iterable[str],
    *,
    minimum_overlap_words: int = MINIMUM_OVERLAP_WORDS,
) -> str:
    if minimum_overlap_words <= 0:
        raise ValueError("minimum overlap words must be positive")
    merged = ""
    for transcript in transcripts:
        current = transcript.strip()
        if not current:
            continue
        if not merged:
            merged = current
            continue
        left_words = _words(merged)
        right_words = _words(current)
        overlap = _overlap_size(
            left_words,
            right_words,
            minimum_overlap_words=minimum_overlap_words,
        )
        if overlap:
            # Preserve existing paragraph separators before the overlap. Rebuilding
            # the whole accumulated transcript with split()/join() would flatten
            # earlier conservative no-match boundaries.
            left_matches = list(re.finditer(r"\S+", merged))
            right_matches = list(re.finditer(r"\S+", current))
            overlap_start = left_matches[-overlap].start()
            prefix = merged[:overlap_start]
            left_overlap = merged[overlap_start:]
            last_left = left_matches[-1]
            relative_start = last_left.start() - overlap_start
            relative_end = last_left.end() - overlap_start
            # Keep left casing and separators but take punctuation from the right
            # chunk's last overlapped token.
            left_overlap = (
                left_overlap[:relative_start]
                + right_words[overlap - 1]
                + left_overlap[relative_end:]
            )
            right_suffix = current[right_matches[overlap - 1].end():]
            merged = f"{prefix}{left_overlap}{right_suffix}"
        else:
            merged = f"{merged}\n\n{current}"
    return merged


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    duration = float(json.loads(proc.stdout)["format"]["duration"])
    if duration < 0:
        raise RuntimeError("ffprobe returned a negative duration")
    return duration


def render_wav_chunk(source: Path, start: float, end: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(source),
            "-t",
            f"{end - start:.6f}",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )


def validate_qwen_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    host = parsed.hostname
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Qwen endpoint must be an explicit HTTP loopback URL") from exc
    if (
        parsed.scheme != "http"
        or host not in loopback_hosts
        or port is None
        or port <= 0
        or parsed.path != "/transcribe"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Qwen endpoint must be an explicit HTTP loopback /transcribe URL")
    return endpoint


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def qwen_transcribe(
    path: Path,
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    endpoint = validate_qwen_endpoint(endpoint)
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"path": str(path.resolve())}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout_seconds) as response:
        if response.geturl() != endpoint:
            raise RuntimeError("Qwen STT response URL changed unexpectedly")
        payload = json.loads(response.read())
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Qwen STT response did not contain a text string")
    return text.strip()


def transcribe_in_chunks(
    source: Path,
    *,
    duration_seconds: float,
    transcribe: Callable[[Path], str],
    render_chunk: Callable[[Path, float, float, Path], None],
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> TranscriptionResult:
    chunks = plan_chunks(
        duration_seconds,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )
    if len(chunks) == 1:
        transcript = transcribe(source).strip()
        if not transcript:
            raise ValueError("Qwen returned an empty transcript for chunk 1")
        return TranscriptionResult(
            text=transcript,
            chunk_count=1,
            duration_seconds=duration_seconds,
        )

    transcripts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="hermes-stt-chunks-") as temp_dir:
        root = Path(temp_dir)
        for index, (start, end) in enumerate(chunks):
            chunk_path = root / f"chunk-{index:03d}.wav"
            render_chunk(source, start, end, chunk_path)
            transcript = transcribe(chunk_path).strip()
            if not transcript:
                raise ValueError(
                    f"Qwen returned an empty transcript for chunk {index + 1}"
                )
            transcripts.append(transcript)
    return TranscriptionResult(
        text=merge_transcripts(transcripts),
        chunk_count=len(chunks),
        duration_seconds=duration_seconds,
    )


def run_to_output(
    source: Path,
    output: Path,
    *,
    duration_seconds: float | None = None,
    transcribe: Callable[[Path], str] | None = None,
    render_chunk: Callable[[Path, float, float, Path], None] = render_wav_chunk,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
) -> TranscriptionResult:
    if not source.is_file() and duration_seconds is None:
        raise FileNotFoundError(source)
    duration = probe_duration(source) if duration_seconds is None else duration_seconds
    transcribe_fn = transcribe or qwen_transcribe
    result = transcribe_in_chunks(
        source,
        duration_seconds=duration,
        transcribe=transcribe_fn,
        render_chunk=render_chunk,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(result.text)
            temp_path = Path(handle.name)
        temp_path.replace(output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return result


def _positive_float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _nonnegative_float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: chunked_qwen_stt.py INPUT_AUDIO OUTPUT_TEXT", file=sys.stderr)
        return 2
    source = Path(args[0]).expanduser()
    output = Path(args[1]).expanduser()
    try:
        chunk_seconds = _positive_float_from_env(
            "HERMES_STT_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS
        )
        overlap_seconds = _nonnegative_float_from_env(
            "HERMES_STT_OVERLAP_SECONDS", DEFAULT_OVERLAP_SECONDS
        )
        endpoint = os.environ.get("HERMES_QWEN_STT_ENDPOINT", DEFAULT_ENDPOINT).strip()
        timeout_seconds = _positive_float_from_env(
            "HERMES_QWEN_STT_TIMEOUT", DEFAULT_TIMEOUT_SECONDS
        )
        result = run_to_output(
            source,
            output,
            transcribe=lambda path: qwen_transcribe(
                path,
                endpoint=endpoint,
                timeout_seconds=timeout_seconds,
            ),
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "durationSeconds": round(result.duration_seconds, 3),
                    "chunkCount": result.chunk_count,
                    "transcriptChars": len(result.text),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 0
    except Exception as exc:
        print(f"chunked Qwen STT failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
