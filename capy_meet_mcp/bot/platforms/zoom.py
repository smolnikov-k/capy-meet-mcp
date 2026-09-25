"""Zoom meeting joiner — browser-based join without Zoom client."""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page

from capy_meet_mcp.bot.platforms.base import PlatformJoiner
from capy_meet_mcp.bot.stealth import human_like_click, random_delay

logger = logging.getLogger(__name__)


class ZoomJoiner(PlatformJoiner):
    """Join Zoom meetings via the web client (no desktop app)."""

    def parse_meeting_url(self, meeting_input: str) -> str:
        """Convert Zoom input to web client URL."""
        meeting_input = meeting_input.strip()

        # Extract meeting ID from various URL formats
        # zoom.us/j/12345678?pwd=xxx -> zoom.us/wc/join/12345678?pwd=xxx
        match = re.search(r"zoom\.us/j/(\d+)(.*)?", meeting_input)
        if match:
            meeting_id = match.group(1)
            params = match.group(2) or ""
            return f"https://zoom.us/wc/join/{meeting_id}{params}"

        # Already a web client URL
        if "zoom.us/wc/join/" in meeting_input:
            if not meeting_input.startswith("http"):
                return f"https://{meeting_input}"
            return meeting_input

        # Bare meeting ID
        cleaned = re.sub(r"[\s\-]", "", meeting_input)
        if cleaned.isdigit() and len(cleaned) >= 9:
            return f"https://zoom.us/wc/join/{cleaned}"

        # Full URL with custom domain
        if meeting_input.startswith("http"):
            return meeting_input

        return f"https://zoom.us/wc/join/{meeting_input}"

    async def join(self, page: Page, meeting_url: str, display_name: str) -> bool:
        """Join a Zoom meeting via browser."""
        url = self.parse_meeting_url(meeting_url)
        logger.info("Navigating to Zoom web client: %s", url)

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await random_delay(3, 6)

        # Handle "Join from Your Browser" link if Zoom tries to open desktop app
        try:
            browser_link = await page.wait_for_selector(
                'a:has-text("Join from Your Browser")', timeout=8000
            )
            if browser_link:
                await browser_link.click()
                await random_delay(2, 4)
                logger.info("Clicked 'Join from Your Browser'")
        except Exception:
            logger.debug("No 'Join from Browser' link found, may already be on web client")

        # Accept cookies / terms if prompted
        for selector in ['button:has-text("Accept")', 'button:has-text("I Agree")']:
            try:
                btn = await page.wait_for_selector(selector, timeout=3000)
                if btn:
                    await btn.click()
            except Exception:
                continue

        # Enter display name
        name_selectors = [
            "#inputname",
            "#input-for-name",
            'input[placeholder*="name" i]',
            'input[aria-label*="name" i]',
            'input[aria-describedby="error-for-name"]',
        ]
        for selector in name_selectors:
            try:
                name_input = await page.wait_for_selector(selector, timeout=5000)
                if name_input:
                    await name_input.clear()
                    await name_input.type(display_name, delay=80)
                    logger.info("Entered display name: %s", display_name)
                    break
            except Exception:
                continue

        # Enter passcode if required
        passcode = self._extract_passcode(url)
        if passcode:
            try:
                pwd_input = await page.wait_for_selector(
                    'input[type="password"], input[placeholder*="passcode" i]',
                    timeout=5000,
                )
                if pwd_input:
                    await pwd_input.type(passcode, delay=80)
                    logger.info("Entered meeting passcode")
            except Exception:
                logger.debug("No passcode field found")

        await random_delay(1, 2)

        # Click Join button
        # Zoom localizes the pre-join CTA; the web client currently uses
        # "Войти" on ru-RU pages and "Join" on English pages.
        join_selectors = [
            'button:has-text("Join")',
            'button:has-text("Войти")',
            'button[id="joinBtn"]',
            'button:has-text("Join Meeting")',
            'button.preview-join-button',
        ]
        joined = False
        for selector in join_selectors:
            try:
                btn = await page.wait_for_selector(selector, timeout=5000)
                if btn:
                    await human_like_click(page, selector)
                    joined = True
                    break
            except Exception:
                continue

        if not joined:
            logger.error("Could not find Zoom join button")
            return False

        logger.info("Clicked join, waiting for meeting admission...")

        # Wait for meeting (handle waiting room)
        for _ in range(30):  # Up to 5 minutes
            if await self.is_in_meeting(page):
                logger.info("Successfully joined Zoom meeting")

                # Mute audio and video
                await self._mute_av(page)
                return True
            await random_delay(8, 12)

        logger.error("Timed out waiting to join Zoom meeting")
        return False

    async def is_in_meeting(self, page: Page) -> bool:
        """Check if in an active Zoom meeting."""
        indicators = [
            "#wc-footer",
            ".meeting-app",
            '[aria-label*="Leave" i]',
            '[aria-label="Выйти"]',
            'button:has-text("Leave")',
            'button:has-text("Выйти")',
            'button:has-text("Включить звук")',
            'button:has-text("Участники")',
            ".participants-section",
        ]
        for selector in indicators:
            try:
                el = await page.query_selector(selector)
                if el:
                    return True
            except Exception:
                continue
        return False

    async def leave_meeting(self, page: Page) -> None:
        """Leave the Zoom meeting."""
        logger.info("Leaving Zoom meeting")
        leave_selectors = [
            'button:has-text("Leave")',
            'button[aria-label*="Leave" i]',
            ".leave-btn",
        ]
        for selector in leave_selectors:
            try:
                btn = await page.wait_for_selector(selector, timeout=3000)
                if btn:
                    await btn.click()
                    break
            except Exception:
                continue

        await random_delay(1, 2)

        # Confirm leave
        try:
            confirm = await page.wait_for_selector(
                'button:has-text("Leave Meeting")', timeout=3000
            )
            if confirm:
                await confirm.click()
        except Exception:
            pass

    async def _mute_av(self, page: Page) -> None:
        """Mute microphone and camera."""
        mute_selectors = [
            'button[aria-label*="mute" i]',
            'button[aria-label*="Mute" i]',
        ]
        video_selectors = [
            'button[aria-label*="Stop Video" i]',
            'button[aria-label*="video" i]',
        ]
        for selector in mute_selectors + video_selectors:
            try:
                btn = await page.wait_for_selector(selector, timeout=2000)
                if btn:
                    await btn.click()
            except Exception:
                continue

    @staticmethod
    def _extract_passcode(url: str) -> str | None:
        """Extract passcode from URL parameters."""
        match = re.search(r"[?&]pwd=([^&]+)", url)
        return match.group(1) if match else None
