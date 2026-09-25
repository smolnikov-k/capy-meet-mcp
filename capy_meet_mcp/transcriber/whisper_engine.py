"""Transcription engine using faster-whisper (CTranslate2 backend)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable
from dataclasses import dataclass, field

from capy_meet_mcp.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Segment:
    """A single transcription segment."""

    start: float
    end: float
    text: str
    speaker: str = "Speaker"


@dataclass(frozen=True)
class TranscriptionResult:
    """Immutable transcription output."""

    segments: tuple[Segment, ...]
    full_text: str
    language: str = "en"
    duration_seconds: float = 0.0


class WhisperTranscriber:
    """Transcribe audio files using faster-whisper."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self._model_size = model_size or settings.whisper_model
        self._device = device or settings.whisper_device
        self._compute_type = compute_type or settings.whisper_compute_type
        self._model = None
        # Called with the end time (seconds) of each finished segment.
        self.on_progress: Callable[[float], None] | None = None

    def _load_model(self):
        """Lazy-load the Whisper model on first use."""
        if self._model is not None:
            return

        logger.info(
            "Loading faster-whisper model: %s (device=%s, compute=%s)",
            self._model_size,
            self._device,
            self._compute_type,
        )

        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )
        logger.info("Model loaded successfully")

    def _transcribe_sync(self, audio_path: str) -> TranscriptionResult:
        """Synchronous transcription (runs in thread pool)."""
        self._load_model()
        assert self._model is not None

        logger.info("Transcribing: %s", audio_path)

        segments_iter, info = self._model.transcribe(
            audio_path,
            beam_size=5,
            language=os.getenv("WHISPER_LANGUAGE") or None,  # None = auto-detect
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )

        detected_lang = info.language
        duration = info.duration
        logger.info(
            "Detected language: %s, Duration: %.1fs", detected_lang, duration
        )

        segments = []
        for seg in segments_iter:
            segments.append(
                Segment(
                    start=round(seg.start, 2),
                    end=round(seg.end, 2),
                    text=seg.text.strip(),
                )
            )
            if self.on_progress is not None:
                self.on_progress(seg.end)
            if len(segments) % 50 == 0:
                logger.info("Processed %d segments...", len(segments))

        full_text = " ".join(s.text for s in segments)
        logger.info(
            "Transcription complete: %d segments, %d chars",
            len(segments),
            len(full_text),
        )

        return TranscriptionResult(
            segments=tuple(segments),
            full_text=full_text,
            language=detected_lang,
            duration_seconds=duration,
        )

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        """Transcribe audio file asynchronously."""
        return await asyncio.to_thread(self._transcribe_sync, audio_path)
