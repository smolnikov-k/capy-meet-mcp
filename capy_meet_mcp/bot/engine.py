"""Bot engine — orchestrates browser launch, meeting join, and audio recording."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from capy_meet_mcp.bot.platforms.base import PlatformJoiner
from capy_meet_mcp.bot.platforms.google_meet import GoogleMeetJoiner
from capy_meet_mcp.bot.platforms.telemost import TelemostJoiner
from capy_meet_mcp.bot.platforms.webex import WebexJoiner
from capy_meet_mcp.bot.platforms.zoom import ZoomJoiner
from capy_meet_mcp.bot.recorder import AudioRecorder
from capy_meet_mcp.bot.stealth import apply_stealth, get_realistic_user_agent, random_delay
from capy_meet_mcp.config import settings
from capy_meet_mcp.transcriber.live_transcriber import LiveTranscriber

logger = logging.getLogger(__name__)

_PLATFORM_JOINERS: dict[str, type[PlatformJoiner]] = {
    "webex": WebexJoiner,
    "zoom": ZoomJoiner,
    "google_meet": GoogleMeetJoiner,
    "telemost": TelemostJoiner,
}

MAX_MEETING_DURATION = int(os.getenv("MAX_MEETING_SECONDS", str(6 * 60 * 60)))
POLL_INTERVAL = 30  # seconds


@dataclass(frozen=True)
class BotResult:
    """Immutable result from a bot session."""

    audio_path: str
    duration_seconds: float
    platform: str
    meeting_url: str
    meeting_id: str
    base_name: str
    status: str
    error: str | None = None
    transcript: str | None = None
    transcript_segments: list | None = None


class BotEngine:
    """Orchestrates headless meeting join and audio recording."""

    def __init__(
        self,
        platform: str,
        meeting_url: str,
        display_name: str | None = None,
    ) -> None:
        if platform not in _PLATFORM_JOINERS:
            raise ValueError(
                f"Unsupported platform: {platform}. "
                f"Choose from: {', '.join(_PLATFORM_JOINERS)}"
            )

        self._platform = platform
        self._meeting_url = meeting_url
        self._display_name = display_name or settings.display_name
        self._joiner = _PLATFORM_JOINERS[platform]()
        self._recorder = AudioRecorder()
        # Live transcription competes with Chromium for CPU during the call;
        # capy-meet-mcp transcribes the finished WAV instead, so it is opt-in.
        self._live = os.getenv("LIVE_TRANSCRIBE", "0") == "1"
        self._transcriber = LiveTranscriber(
            chunk_seconds=30,
            model_size=settings.whisper_model,
            language="ru",
        )
        self._shutdown_requested = False

    async def run(self) -> BotResult:
        """Run the full bot lifecycle: launch → join → record → leave."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        meeting_id = self._extract_meeting_id(self._meeting_url)
        base_name = f"{self._platform}_{meeting_id}_{timestamp}"
        audio_filename = f"{base_name}.wav"
        audio_path = os.getenv("AUDIO_PATH") or str(settings.recordings_dir / audio_filename)

        # Ensure output directory exists
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)

        # Generate safe fake media feeds. These are fail-safes: joiners should
        # still turn mic/camera off before joining, but if a platform UI changes
        # and the mic accidentally stays enabled, Chromium must send silence —
        # not its default fake-audio tone/beep. The camera feed is black for the
        # same reason.
        black_video = str(settings.recordings_dir / "_black_feed.y4m")
        silent_audio = str(settings.recordings_dir / "_silent_mic.wav")
        await self._generate_black_video(black_video)
        await self._generate_silent_audio(silent_audio)

        # Setup graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown)

        async with async_playwright() as pw:
            # Prefer the managed Playwright browser, but fall back to the
            # installed system Chromium when the browser cache/package
            # revisions drift apart after an upgrade.
            managed_browser = Path(pw.chromium.executable_path)
            # Prefer full Chromium over headless-shell — headless-shell has
            # no PulseAudio output, so meeting audio cannot be captured.
            full_browser = Path(
                str(managed_browser).replace(
                    "chromium_headless_shell", "chromium"
                )
            )
            if (
                "headless_shell" in str(managed_browser)
                and full_browser.exists()
            ):
                managed_browser = full_browser
            system_browser = next(
                (
                    Path(candidate)
                    for candidate in (
                        shutil.which("chromium-browser"),
                        shutil.which("chromium"),
                        shutil.which("google-chrome"),
                    )
                    if candidate
                ),
                None,
            )
            executable_path = (
                str(managed_browser)
                if managed_browser.exists()
                else str(system_browser) if system_browser else None
            )
            if not executable_path:
                raise RuntimeError(
                    "No Chromium executable found: install Playwright Chromium "
                    "or provide chromium-browser/chromium on PATH"
                )
            logger.info("Launching Chromium: %s", executable_path)
            browser = await pw.chromium.launch(
                executable_path=executable_path,
                headless=False,  # Xvfb provides the display; non-headless enables audio
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--use-fake-ui-for-media-stream",  # Auto-allow mic/camera
                    "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-video-capture={black_video}",
                    f"--use-file-for-fake-audio-capture={silent_audio}",
                    "--autoplay-policy=no-user-gesture-required",
                    # Audio: keep in-process and force PulseAudio output
                    "--disable-features=AudioServiceOutOfProcess",
                    f"--user-agent={get_realistic_user_agent()}",
                ],
            )

            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=get_realistic_user_agent(),
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                permissions=["microphone", "camera"],
            )

            page = await context.new_page()

            try:
                # Apply anti-detection
                await apply_stealth(page)

                # Join meeting
                logger.info(
                    "Joining %s meeting: %s as '%s'",
                    self._platform,
                    self._meeting_url,
                    self._display_name,
                )
                joined = await self._joiner.join(
                    page, self._meeting_url, self._display_name
                )

                if not joined:
                    # Save debug screenshot
                    debug_path = str(
                        settings.recordings_dir / "debug_failed_join.png"
                    )
                    try:
                        await page.screenshot(path=debug_path, full_page=True)
                        logger.info("Debug screenshot saved: %s", debug_path)
                    except Exception as ss_err:
                        logger.warning("Could not save screenshot: %s", ss_err)

                    return BotResult(
                        audio_path="",
                        duration_seconds=0,
                        platform=self._platform,
                        meeting_url=self._meeting_url,
                        meeting_id=meeting_id,
                        base_name=base_name,
                        status="failed",
                        error="Failed to join meeting",
                    )

                # Start recording
                await self._recorder.start(audio_path)
                logger.info("Recording started")

                # Start live transcription in background
                transcribe_task = None
                if self._live:
                    transcribe_task = asyncio.create_task(
                        self._transcriber.process_chunks(audio_path)
                    )
                    logger.info("Live transcription started")

                # Monitor meeting until it ends or timeout
                elapsed = 0.0
                while (
                    not self._shutdown_requested
                    and elapsed < MAX_MEETING_DURATION
                ):
                    # Sleep in one-second steps so a stop request is honoured
                    # promptly instead of after a full poll interval.
                    for _ in range(POLL_INTERVAL):
                        if self._shutdown_requested:
                            break
                        await asyncio.sleep(1)
                        elapsed += 1
                    if self._shutdown_requested:
                        logger.info("Leaving on request")
                        break

                    if not await self._joiner.is_in_meeting(page):
                        logger.info("Meeting has ended")
                        break

                    if elapsed % 300 < POLL_INTERVAL:
                        logger.info(
                            "Still recording... (%.0f min)", elapsed / 60
                        )

                # Stop live transcription
                if transcribe_task is not None:
                    self._transcriber.stop()
                    try:
                        await asyncio.wait_for(transcribe_task, timeout=10)
                    except asyncio.TimeoutError:
                        transcribe_task.cancel()
                    logger.info("Live transcription stopped")

                # Stop recording
                final_path = await self._recorder.stop()
                duration = self._recorder.duration_seconds

                # Leave meeting gracefully
                try:
                    await self._joiner.leave_meeting(page)
                except Exception as e:
                    logger.warning("Error leaving meeting: %s", e)

                logger.info(
                    "Bot session complete. Duration: %.1f min, Audio: %s",
                    duration / 60,
                    final_path,
                )

                return BotResult(
                    audio_path=final_path,
                    duration_seconds=duration,
                    platform=self._platform,
                    meeting_url=self._meeting_url,
                    meeting_id=meeting_id,
                    base_name=base_name,
                    status="completed",
                    transcript=self._transcriber.get_full_text(),
                    transcript_segments=[
                        {"start": s.start, "end": s.end, "text": s.text}
                        for s in self._transcriber.get_segments()
                    ],
                )

            except Exception as e:
                logger.error("Bot engine error: %s", e, exc_info=True)
                await self._recorder.cleanup()
                return BotResult(
                    audio_path=audio_path if Path(audio_path).exists() else "",
                    duration_seconds=self._recorder.duration_seconds,
                    platform=self._platform,
                    meeting_url=self._meeting_url,
                    meeting_id=meeting_id,
                    base_name=base_name,
                    status="failed",
                    error=str(e),
                )
            finally:
                await context.close()
                await browser.close()

    def _request_shutdown(self) -> None:
        """Signal the bot to stop recording and leave."""
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    @staticmethod
    def _extract_meeting_id(url: str) -> str:
        """Extract a short meeting identifier from the URL.

        Examples:
          https://example.webex.com/example/j.php?MTID=m2f0e3... → m2f0e3
          https://example.webex.com/meet/room                    → room
          https://zoom.us/j/12345678?pwd=abc                          → 12345678
          https://meet.google.com/abc-defg-hij                        → abc-defg-hij
          Just a number: 1234567890                                    → 1234567890
        """
        url = url.strip()

        # Webex j.php?MTID=...
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "MTID" in qs:
            mtid = qs["MTID"][0]
            # Use first 8 chars to keep filenames manageable
            return mtid[:8] if len(mtid) > 8 else mtid

        # Webex /meet/<room>
        match = re.search(r"/meet/([^/?#]+)", url)
        if match:
            return match.group(1)

        # Zoom /j/<meeting_id>
        match = re.search(r"/j/(\d+)", url)
        if match:
            return match.group(1)

        # Google Meet /xxx-xxxx-xxx
        match = re.search(r"/([a-z]{3}-[a-z]{4}-[a-z]{3})", url)
        if match:
            return match.group(1)

        # Yandex Telemost /xxxxxxxx
        match = re.search(r"telemost\.yandex\.ru/([a-zA-Z0-9\-]+)", url)
        if match:
            return match.group(1)

        # Bare numeric ID
        cleaned = re.sub(r"[\s\-]", "", url)
        if cleaned.isdigit():
            return cleaned

        # Fallback: last path segment
        path = parsed.path.rstrip("/")
        if path:
            return path.split("/")[-1][:12]

        return "unknown"

    @staticmethod
    async def _generate_black_video(path: str) -> None:
        """Generate a 1-second black Y4M video for the fake camera feed.

        Chromium loops this file, showing a steady black frame instead of
        its default flashing test pattern. Uses ffmpeg which is already
        a required dependency.
        """
        if Path(path).exists():
            return
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "color=c=black:s=640x480:d=1:r=1",
            "-pix_fmt", "yuv420p", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Generated black video feed: %s", path)

    @staticmethod
    async def _generate_silent_audio(path: str) -> None:
        """Generate a silent WAV used as Chromium's fake microphone input.

        Chrome's built-in fake audio device emits a test tone on some builds.
        That is unacceptable for recorder bots: if the meeting UI fails to mute
        us, participants hear a beep. Supplying our own silent mic file makes
        accidental unmute harmless.
        """
        if Path(path).exists():
            return
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "anullsrc=r=48000:cl=mono", "-t", "1",
            "-acodec", "pcm_s16le", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("Generated silent fake microphone feed: %s", path)


async def run_bot_cli(platform: str, meeting_url: str, display_name: str) -> BotResult:
    """CLI entry point for running the bot."""
    engine = BotEngine(platform, meeting_url, display_name)
    return await engine.run()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="capy-meet-mcp bot engine")
    parser.add_argument("--platform", required=True, choices=list(_PLATFORM_JOINERS))
    parser.add_argument("--meeting-url", required=True)
    parser.add_argument("--display-name", default=settings.display_name)
    args = parser.parse_args()

    result = asyncio.run(
        run_bot_cli(args.platform, args.meeting_url, args.display_name)
    )
    
    # Save transcript if available
    if result.transcript and result.status == "completed":
        transcript_path = str(settings.transcripts_dir / f"{result.base_name}_transcript.txt")
        settings.transcripts_dir.mkdir(parents=True, exist_ok=True)
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(result.transcript)
        
        # Save segments with timestamps
        segments_path = str(settings.transcripts_dir / f"{result.base_name}_segments.txt")
        with open(segments_path, "w", encoding="utf-8") as f:
            for seg in result.transcript_segments or []:
                start = seg["start"]
                end = seg["end"]
                m1, s1 = divmod(int(start), 60)
                m2, s2 = divmod(int(end), 60)
                f.write(f"[{m1:02d}:{s1:02d} → {m2:02d}:{s2:02d}] {seg['text']}\n")
        
        print(f"\n📝 Transcript saved: {transcript_path}")
        print(f"📋 Segments saved: {segments_path}")
    
    print(f"\nResult: {result.status}")
    print(f"Audio: {result.audio_path}")
    print(f"Duration: {result.duration_seconds:.0f}s")
    sys.exit(0 if result.status == "completed" else 1)
