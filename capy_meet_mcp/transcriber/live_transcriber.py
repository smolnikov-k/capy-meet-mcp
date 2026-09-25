"""Real-time transcription engine — chunks audio during recording."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single transcription segment."""
    start: float
    end: float
    text: str
    chunk_idx: int


class LiveTranscriber:
    """Transcribes audio in real-time by processing chunks during recording.
    
    Uses a ring buffer approach:
    1. FFmpeg records continuously to a file
    2. Every chunk_seconds, extract the latest chunk
    3. Transcribe with faster-whisper
    4. Output results as they come
    """
    
    def __init__(
        self,
        chunk_seconds: int = 30,
        model_size: str = "small",
        language: str = "ru",
    ):
        self.chunk_seconds = chunk_seconds
        self.model_size = model_size
        self.language = language
        self.model = None
        self.segments: list[TranscriptSegment] = []
        self.chunk_counter = 0
        self.running = False
        self._tmp_dir = None
        
    def load_model(self):
        """Lazy-load Whisper model."""
        from faster_whisper import WhisperModel
        
        if self.model is not None:
            return
            
        logger.info("Loading Whisper model: %s", self.model_size)
        t0 = time.time()
        self.model = WhisperModel(
            self.model_size,
            device="cpu",
            compute_type="int8",
        )
        logger.info("Model loaded in %.1f sec", time.time() - t0)
        
    def _format_time(self, seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
        
    def _get_recorded_duration(self, recording_path: str) -> float:
        """Get current duration of the recording file."""
        import subprocess
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", recording_path],
                capture_output=True, text=True, timeout=5
            )
            return float(result.stdout.strip())
        except Exception:
            return 0
            
    def _extract_chunk(self, recording_path: str, start: float, end: float, output: str):
        """Extract audio chunk from recording."""
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-i", recording_path,
             "-ss", str(start), "-to", str(end),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
             output],
            capture_output=True, check=True, timeout=30
        )
        
    def _transcribe_chunk(self, chunk_path: str, chunk_idx: int) -> list[TranscriptSegment]:
        """Transcribe a single chunk."""
        segments_iter, info = self.model.transcribe(
            chunk_path,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        
        offset = chunk_idx * self.chunk_seconds
        results = []
        for seg in segments_iter:
            results.append(TranscriptSegment(
                start=offset + seg.start,
                end=offset + seg.end,
                text=seg.text.strip(),
                chunk_idx=chunk_idx,
            ))
        return results
        
    async def process_chunks(self, recording_path: str, callback=None):
        """Process chunks while recording is happening.
        
        Args:
            recording_path: Path to the recording file
            callback: Optional callback(segment) for each new segment
        """
        self.load_model()
        self.running = True
        self._tmp_dir = tempfile.mkdtemp(prefix="livetranscribe_")
        
        logger.info("Starting live transcription (chunk=%ds, model=%s)",
                     self.chunk_seconds, self.model_size)
        
        while self.running:
            await asyncio.sleep(self.chunk_seconds)
            
            if not self.running:
                break
                
            duration = self._get_recorded_duration(recording_path)
            expected_end = (self.chunk_counter + 1) * self.chunk_seconds
            
            if duration < expected_end:
                continue  # Not enough audio yet
                
            # Extract chunk
            start = self.chunk_counter * self.chunk_seconds
            chunk_path = os.path.join(self._tmp_dir, f"chunk_{self.chunk_counter}.wav")
            
            try:
                self._extract_chunk(recording_path, start, expected_end, chunk_path)
            except Exception as e:
                logger.warning("Failed to extract chunk %d: %s", self.chunk_counter, e)
                self.chunk_counter += 1
                continue
                
            # Transcribe
            try:
                new_segments = self._transcribe_chunk(chunk_path, self.chunk_counter)
                self.segments.extend(new_segments)
                
                for seg in new_segments:
                    ts = f"[{self._format_time(seg.start)} → {self._format_time(seg.end)}]"
                    logger.info("%s %s", ts, seg.text)
                    
                    if callback:
                        callback(seg)
                        
            except Exception as e:
                logger.warning("Failed to transcribe chunk %d: %s", self.chunk_counter, e)
                
            self.chunk_counter += 1
            
            # Cleanup chunk file
            try:
                os.unlink(chunk_path)
            except Exception:
                pass
                
    def stop(self):
        """Stop processing chunks."""
        self.running = False
        
    def get_full_text(self) -> str:
        """Get concatenated transcript text."""
        return " ".join(s.text for s in self.segments)
        
    def get_segments(self) -> list[TranscriptSegment]:
        """Get all segments."""
        return self.segments.copy()
        
    async def transcribe_file(self, audio_path: str) -> list[TranscriptSegment]:
        """Transcribe an existing audio file (post-recording)."""
        self.load_model()
        
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", audio_path],
            capture_output=True, text=True
        )
        duration = float(result.stdout.strip())
        
        logger.info("Transcribing file: %s (%.1f sec)", audio_path, duration)
        
        segments_iter, info = self.model.transcribe(
            audio_path,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        
        self.segments = []
        for seg in segments_iter:
            self.segments.append(TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                chunk_idx=0,
            ))
            
        return self.segments
