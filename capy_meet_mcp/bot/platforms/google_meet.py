"""Google Meet joiner — browser-based headless join."""

from __future__ import annotations

import logging
import re

from playwright.async_api import Page

from capy_meet_mcp.bot.platforms.base import PlatformJoiner
from capy_meet_mcp.bot.stealth import human_like_click, random_delay

logger = logging.getLogger(__name__)


class GoogleMeetJoiner(PlatformJoiner):
    """Join Google Meet meetings via browser automation."""

    def parse_meeting_url(self, meeting_input: str) -> str:
        """Normalize Google Meet input to a valid URL."""
        meeting_input = meeting_input.strip()

        # Already a full URL
        if meeting_input.startswith("https://meet.google.com/"):
            return meeting_input

        # Meeting code: xxx-xxxx-xxx
        code_match = re.match(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", meeting_input, re.IGNORECASE)
        if code_match:
            return f"https://meet.google.com/{meeting_input.lower()}"

        # Bare code without hyphens
        if re.match(r"^[a-z]{10}$", meeting_input, re.IGNORECASE):
            code = meeting_input.lower()
            return f"https://meet.google.com/{code[:3]}-{code[3:7]}-{code[7:]}"

        return f"https://meet.google.com/{meeting_input}"

    async def join(self, page: Page, meeting_url: str, display_name: str) -> bool:
        """Join a Google Meet meeting."""
        url = self.parse_meeting_url(meeting_url)
        logger.info("Navigating to Google Meet: %s", url)

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await random_delay(3, 6)

        # Dismiss cookie consent if present
        for selector in [
            'button:has-text("Accept all")',
            'button:has-text("Got it")',
            'button:has-text("I agree")',
        ]:
            try:
                btn = await page.wait_for_selector(selector, timeout=3000)
                if btn:
                    await btn.click()
                    await random_delay(1, 2)
            except Exception:
                continue

        # Enter display name (for non-logged-in users)
        name_selectors = [
            'input[placeholder*="name" i]',
            'input[aria-label*="name" i]',
            'input[jsname="YPqjbf"]',
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

        await random_delay(1, 2)

        # Turn off microphone and camera before joining
        await self._toggle_av_off(page)

        # Click "Ask to join" or "Join now"
        join_selectors = [
            'button:has-text("Ask to join")',
            'button:has-text("Join now")',
            'button:has-text("Join")',
            '[data-idom-class*="join"]',
        ]
        clicked = False
        for selector in join_selectors:
            try:
                btn = await page.wait_for_selector(selector, timeout=5000)
                if btn:
                    await human_like_click(page, selector)
                    clicked = True
                    logger.info("Clicked join button")
                    break
            except Exception:
                continue

        if not clicked:
            logger.error("Could not find Google Meet join button")
            return False

        # Wait for admission (host may need to admit from waiting room)
        logger.info("Waiting for meeting admission...")
        for _ in range(30):  # Up to 5 minutes
            if await self.is_in_meeting(page):
                logger.info("Successfully joined Google Meet")
                return True

            # Check if still in "waiting for host" state
            try:
                waiting = await page.query_selector(
                    ':has-text("Waiting for the host")'
                )
                if waiting:
                    logger.debug("Still waiting for host to admit...")
            except Exception:
                pass

            await random_delay(8, 12)

        logger.error("Timed out waiting to join Google Meet")
        return False

    async def is_in_meeting(self, page: Page) -> bool:
        """Check if in an active Google Meet session."""
        indicators = [
            '[data-call-active="true"]',
            'button[aria-label*="Leave call" i]',
            'button[aria-label*="people" i]',
            '[data-meeting-title]',
            ".google-material-icons:has-text('call_end')",
        ]
        for selector in indicators:
            try:
                el = await page.query_selector(selector)
                if el:
                    return True
            except Exception:
                continue

        # Fallback: check page title changes during active meeting
        title = await page.title()
        if title and "Meet" in title and "|" in title:
            return True

        return False

    async def leave_meeting(self, page: Page) -> None:
        """Leave the Google Meet session."""
        logger.info("Leaving Google Meet")
        leave_selectors = [
            'button[aria-label*="Leave call" i]',
            'button:has-text("Leave call")',
            '[data-tooltip*="Leave" i]',
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

    async def _toggle_av_off(self, page: Page) -> None:
        """Turn off mic and camera in the preview screen."""
        mic_selectors = [
            'button[aria-label*="microphone" i]',
            'button[data-is-muted="false"][aria-label*="mic" i]',
        ]
        camera_selectors = [
            'button[aria-label*="camera" i]',
            'button[data-is-muted="false"][aria-label*="video" i]',
        ]
        for selector in mic_selectors + camera_selectors:
            try:
                btn = await page.wait_for_selector(selector, timeout=3000)
                if btn:
                    is_muted = await btn.get_attribute("data-is-muted")
                    if is_muted != "true":
                        await btn.click()
            except Exception:
                continue
