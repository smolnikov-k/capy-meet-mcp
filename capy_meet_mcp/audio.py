"""WAV probes: is the recording growing and is there sound in its tail.

The recorder writes 16 kHz mono s16le PCM through FFmpeg. While FFmpeg is still
running the WAV header is not final, so the probes read raw bytes after the
44-byte header instead of trusting the header or a whole-file volume scan.
"""

from __future__ import annotations

import math
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
HEADER_BYTES = 44
# Below this RMS level the tail counts as silence: an empty sink sits near
# -90 dB, quiet speech is well above -60 dB.
SILENCE_DB = -60.0


@dataclass(frozen=True)
class TailLevel:
    seconds: float
    rms_db: float
    peak_db: float

    @property
    def silent(self) -> bool:
        return self.rms_db < SILENCE_DB


def _db(value: float) -> float:
    return float("-inf") if value <= 0 else 20 * math.log10(value / 32768)


def duration_seconds(path: Path) -> float:
    """Recorded length estimated from the file size, valid mid-recording."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return 0.0
    return max(0, size - HEADER_BYTES) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def tail_level(path: Path, seconds: float = 20.0) -> TailLevel:
    """RMS and peak of the last ``seconds`` of PCM."""
    want = int(SAMPLE_RATE * seconds) * BYTES_PER_SAMPLE
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return TailLevel(0.0, float("-inf"), float("-inf"))
    start = max(HEADER_BYTES, size - want)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(want)
    count = len(raw) // BYTES_PER_SAMPLE
    if count == 0:
        return TailLevel(0.0, float("-inf"), float("-inf"))
    values = struct.unpack(f"<{count}h", raw[: count * BYTES_PER_SAMPLE])
    rms = math.sqrt(sum(v * v for v in values) / count)
    peak = max(abs(v) for v in values)
    return TailLevel(count / SAMPLE_RATE, _db(rms), _db(peak))


def seconds_since_write(path: Path) -> float | None:
    """How long ago the file was last written; None if it does not exist."""
    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except FileNotFoundError:
        return None


def is_growing(path: Path, interval: float = 4.0, fresh: float = 10.0) -> bool:
    """True if the recorder is writing: the file grew during ``interval`` or
    was written within the last ``fresh`` seconds. Either alone misreads a
    writer that flushes in bursts."""
    age = seconds_since_write(path)
    if age is None:
        return False
    if age < fresh:
        return True
    return growth(path, interval) > 0


def growth(path: Path, interval: float = 2.0) -> int:
    """Bytes added to the file over ``interval`` seconds (0 if missing)."""
    try:
        before = path.stat().st_size
    except FileNotFoundError:
        return 0
    time.sleep(interval)
    try:
        return path.stat().st_size - before
    except FileNotFoundError:
        return 0
