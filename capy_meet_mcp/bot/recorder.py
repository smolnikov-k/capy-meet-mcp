"""Audio recording via PulseAudio virtual sink + FFmpeg."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Records system audio from PulseAudio monitor source using FFmpeg."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._output_path: str | None = None
        self._start_time: float | None = None
        self._is_recording: bool = False
        self._log_path: Path | None = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def duration_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    async def start(self, output_path: str) -> None:
        """Start recording audio to the specified WAV file."""
        if self._is_recording:
            raise RuntimeError("Already recording")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path

        # FFmpeg: capture from PulseAudio monitor of our virtual sink.
        # The MeetScribe.monitor source captures all audio output routed
        # to the MeetScribe null sink (i.e., Chromium's meeting audio).
        cmd = [
            "ffmpeg",
            "-y",                              # Overwrite output
            "-nostats",                        # No progress lines
            "-loglevel", "warning",
            "-f", "pulse",                     # PulseAudio input
            "-i", os.getenv("PULSE_SINK", "meeting_record") + ".monitor",  # Virtual sink's monitor
            "-ac", "1",                        # Mono
            "-ar", "16000",                    # 16kHz (optimal for Whisper)
            "-acodec", "pcm_s16le",            # 16-bit PCM
            "-flush_packets", "1",             # Grow the file steadily, not in bursts
            output_path,
        ]

        logger.info("Starting FFmpeg recording to %s", output_path)

        # stderr goes to a file, never to an unread pipe: once the 64 KB pipe
        # buffer fills, FFmpeg blocks on write and the recording silently stops
        # growing mid-call (seen live after ~13 minutes).
        self._log_path = Path(output_path).with_name("ffmpeg.log")
        with self._log_path.open("ab") as ffmpeg_log:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=ffmpeg_log,
            )
        self._start_time = time.time()
        self._is_recording = True
        logger.info("Recording started (PID: %d)", self._process.pid)

        # Log PulseAudio state for debugging audio routing
        await self._log_pulse_state()

    async def stop(self) -> str:
        """Stop recording and return the output file path."""
        if not self._is_recording or self._process is None:
            raise RuntimeError("Not currently recording")

        logger.info(
            "Stopping recording after %.1f seconds", self.duration_seconds
        )

        # Send SIGINT for clean FFmpeg shutdown (writes proper file headers)
        try:
            self._process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            logger.warning("FFmpeg process already terminated")

        try:
            await asyncio.wait_for(self._process.wait(), timeout=10)
            if self._process.returncode not in (0, 255):
                try:
                    stderr_text = self._log_path.read_text(errors="replace") if self._log_path else ""
                except FileNotFoundError:
                    stderr_text = ""
                logger.warning(
                    "FFmpeg exited with code %d: %s",
                    self._process.returncode,
                    stderr_text[-500:],
                )
        except asyncio.TimeoutError:
            logger.warning("FFmpeg did not stop gracefully, killing")
            self._process.kill()
            await self._process.wait()

        self._is_recording = False
        output = self._output_path or ""
        logger.info("Recording saved to %s", output)

        # Verify file exists and has content
        path = Path(output)
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Recording file is empty or missing: {output}")

        return output

    @staticmethod
    async def _log_pulse_state() -> None:
        """Log PulseAudio sink-inputs and sources for debugging audio routing."""
        for cmd_name, cmd in [
            ("sink-inputs", ["pactl", "list", "short", "sink-inputs"]),
            ("sources", ["pactl", "list", "short", "sources"]),
            ("clients", ["pactl", "list", "short", "clients"]),
        ]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode().strip()
                if output:
                    logger.info("PulseAudio %s:\n%s", cmd_name, output)
                else:
                    logger.warning("PulseAudio %s: (empty)", cmd_name)
            except Exception as e:
                logger.warning("Could not query PulseAudio %s: %s", cmd_name, e)

    async def cleanup(self) -> None:
        """Force-stop recording if still running."""
        if self._process and self._is_recording:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
            self._is_recording = False
