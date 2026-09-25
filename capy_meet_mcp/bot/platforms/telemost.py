"""Yandex Telemost platform joiner."""

from __future__ import annotations

import logging
import re

from playwright.async_api import Frame, Page, TimeoutError as PlaywrightTimeoutError

from capy_meet_mcp.bot.platforms.base import PlatformJoiner
from capy_meet_mcp.bot.stealth import apply_stealth

logger = logging.getLogger(__name__)

_MIC_ON_SELECTORS = [
    'button[title*="Выключить микрофон"]',
    'button[aria-label*="Выключить микрофон"]',
    'button[title*="Turn off microphone"]',
    'button[aria-label*="Turn off microphone"]',
    'button[title*="Mute microphone"]',
    'button[aria-label*="Mute microphone"]',
]

_CAMERA_ON_SELECTORS = [
    'button[title*="Выключить камеру"]',
    'button[aria-label*="Выключить камеру"]',
    'button[title*="Turn off camera"]',
    'button[aria-label*="Turn off camera"]',
    'button[title*="Stop camera"]',
    'button[aria-label*="Stop camera"]',
]

_JOIN_SELECTORS = [
    'button:has-text("Подключиться")',
    'button:has-text("Присоединиться")',
    'button:has-text("Join")',
    'button:has-text("Вступить")',
    'button.joinMeetingButton_M38VH',
]


class TelemostJoiner(PlatformJoiner):
    """Joins Yandex Telemost meetings as a guest via browser."""

    async def join(self, page: Page, meeting_url: str, display_name: str) -> bool:
        """Join a Telemost meeting."""
        logger.info("Joining Telemost meeting: %s", meeting_url)
        self._active_page = page

        try:
            # Telemost keeps long-lived background connections, so networkidle
            # may never occur even when the meeting UI is usable. Treat a
            # navigation timeout as non-fatal and let the selector checks below
            # decide whether the pre-join screen actually loaded.
            try:
                await page.goto(meeting_url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                logger.warning("Telemost DOM did not finish loading; continuing with selector checks")
            await page.wait_for_timeout(5000)
            embedded_meeting = await self._get_meeting_frame(page)
            if (
                isinstance(embedded_meeting, Frame)
                and "private-join" in embedded_meeting.url
                and embedded_meeting.url != page.url
            ):
                # The updated 360 shell leaves the guest iframe behind a
                # parent overlay whose postMessage origin is inconsistent.
                # Navigating directly to the discovered guest URL preserves
                # the same conference session and makes the join button usable.
                direct_url = re.sub(r"([?&])mic=(?:on|off)", r"\1mic=off", embedded_meeting.url)
                direct_url = re.sub(r"([?&])camera=(?:on|off)", r"\1camera=off", direct_url)
                logger.info("Opening Telemost guest frame directly: %s", direct_url)
                direct_page = await page.context.new_page()
                await apply_stealth(direct_page)
                await direct_page.goto(
                    direct_url, wait_until="domcontentloaded", timeout=60000
                )
                await direct_page.wait_for_timeout(3000)
                meeting: Page | Frame = direct_page
                self._active_page = direct_page
            else:
                meeting = embedded_meeting
            logger.info("Using Telemost guest frame: %s", getattr(meeting, "url", "main page"))
            await self._dismiss_overlays(page)

            # Try to find and fill name input
            name_selectors = [
                'input[data-tid="input"]',
                'input[placeholder*="имя"]',
                'input[placeholder*="Имя"]',
                'input[placeholder*="name"]',
                'input[placeholder*="Name"]',
                'input[type="text"]',
            ]

            for selector in name_selectors:
                name_input = await meeting.query_selector(selector)
                if name_input:
                    await name_input.fill(display_name)
                    logger.info("Filled name: %s", display_name)
                    break

            await meeting.wait_for_timeout(1000)

            await self._ensure_devices_off(meeting, phase="pre-join")
            await self._dismiss_overlays(meeting)

            # Try to find and click join button in the guest iframe. Do not
            # include the main-page account button labelled "Войти" here.
            clicked_join = False
            for selector in _JOIN_SELECTORS:
                join_btn = meeting.locator(selector).first
                try:
                    if not await join_btn.is_visible(timeout=500):
                        continue
                    await join_btn.click(force=True, timeout=5000)
                    logger.info("Clicked join button: %s", selector)
                    clicked_join = True
                    break
                except Exception as exc:
                    logger.debug("Could not click Telemost join button via %s: %s", selector, exc)

            if not clicked_join:
                logger.error("Could not find Telemost join/connect button")
                return False

            # Wait for meeting to connect
            await meeting.wait_for_timeout(5000)

            # Defense in depth: if Telemost ignores pre-join state or re-enables
            # devices after connect, mute/disable again from the in-call UI.
            await self._ensure_devices_off(meeting, phase="in-call")

            # If the pre-join connect button is still visible, we did not enter
            # the meeting. Do not report success and start a misleading recorder.
            if await self._has_visible_join_button(meeting):
                logger.error("Telemost still shows pre-join screen after connect click")
                return False

            # Try to dismiss any remaining device-error popups.
            await self._dismiss_overlays(meeting)

            logger.info("Successfully joined Telemost meeting")
            if self._active_page is not page and not page.is_closed():
                await page.close()
            return True

        except Exception as e:
            logger.error("Failed to join Telemost meeting: %s", e)
            return False

    async def _get_meeting_frame(self, page: Page) -> Page | Frame:
        """Return the nested guest-join frame used by current Telemost UI."""
        for _ in range(30):
            for frame in page.frames:
                if "private-join" in frame.url:
                    return frame
            await page.wait_for_timeout(500)
        logger.warning("Telemost guest iframe was not found; falling back to main page")
        return page

    async def _dismiss_overlays(self, page: Page | Frame) -> None:
        """Dismiss informational Telemost overlays before clicking join."""
        selectors = [
            'button[aria-label="Закрыть ознакомление"]',
            'button.yamb-telemost-3-onboarding__confirm',
            'button:has-text("Звучит отлично")',
            'button:has-text("Понятно")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            'button:has-text("Закрыть")',
        ]
        for _ in range(4):
            clicked_any = False
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    for index in range(await locator.count()):
                        button = locator.nth(index)
                        if await button.is_visible(timeout=500):
                            await button.evaluate("(element) => element.click()")
                            logger.info("Dismissed Telemost overlay via %s", selector)
                            await page.wait_for_timeout(500)
                            clicked_any = True
                except Exception as exc:
                    logger.debug("Could not dismiss Telemost overlay via %s: %s", selector, exc)
            if not clicked_any:
                break

        # Wait for the active overlay to finish its close animation. Escape is
        # a safe fallback for Telemost's informational dialogs.
        try:
            active_overlay = page.locator('.Orb-Overlay2[aria-hidden="false"]')
            if await active_overlay.count() > 0 and isinstance(page, Page):
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
        except Exception:
            pass

    async def _ensure_devices_off(self, page: Page | Frame, phase: str) -> None:
        """Turn Telemost microphone and camera off if they are currently on.

        Telemost labels active controls as actions: "Выключить микрофон" /
        "Выключить камеру". If those buttons are visible, clicking them mutes
        us. This prevents the recorder bot from beeping into calls or showing a
        black fake-camera tile.
        """
        if isinstance(page, Page):
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            await page.mouse.move(viewport["width"] / 2, viewport["height"] - 40)
            await page.wait_for_timeout(300)

        mic_clicked = await self._click_first_visible(
            page, _MIC_ON_SELECTORS, f"{phase} microphone"
        )
        camera_clicked = await self._click_first_visible(
            page, _CAMERA_ON_SELECTORS, f"{phase} camera"
        )

        if not mic_clicked:
            logger.info("Telemost %s microphone already off or control hidden", phase)
        if not camera_clicked:
            logger.info("Telemost %s camera already off or control hidden", phase)

    async def _click_first_visible(
        self, page: Page | Frame, selectors: list[str], label: str
    ) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() == 0:
                    continue
                element = locator.first
                if not await element.is_visible(timeout=500):
                    continue
                await element.click(timeout=2000)
                logger.info("Turned off Telemost %s via %s", label, selector)
                await page.wait_for_timeout(500)
                return True
            except Exception as exc:
                logger.debug(
                    "Could not click Telemost %s via %s: %s", label, selector, exc
                )
        return False

    async def _has_visible_join_button(self, page: Page | Frame) -> bool:
        for selector in _JOIN_SELECTORS:
            try:
                locator = page.locator(selector)
                if await locator.count() == 0:
                    continue
                if await locator.first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    async def is_in_meeting(self, page: Page) -> bool:
        """Check if still in Telemost meeting."""
        target = getattr(self, "_active_page", page)
        try:
            # Look for meeting UI elements
            indicators = [
                'button:has-text("Покинуть")',
                'button:has-text("Leave")',
                'button:has-text("Выйти")',
                '[data-tid="chat"]',
                '[data-tid="participants"]',
            ]

            for selector in indicators:
                element = await target.query_selector(selector)
                if element:
                    return True

            # Check URL is still telemost
            url = target.url
            if "telemost" in url or "video.yandex" in url:
                return True

            return False

        except Exception:
            return False

    async def leave_meeting(self, page: Page) -> None:
        """Leave the Telemost meeting."""
        target = getattr(self, "_active_page", page)
        try:
            leave_selectors = [
                'button:has-text("Покинуть")',
                'button:has-text("Leave")',
                'button:has-text("Выйти")',
            ]

            for selector in leave_selectors:
                leave_btn = await target.query_selector(selector)
                if leave_btn:
                    await leave_btn.click()
                    logger.info("Left Telemost meeting")
                    return

            # Fallback: close the active page
            await target.close()

        except Exception as e:
            logger.warning("Error leaving Telemost: %s", e)

    def parse_meeting_url(self, meeting_input: str) -> str:
        """Normalize Telemost URL."""
        meeting_input = meeting_input.strip()

        # Already a full URL
        if meeting_input.startswith("http"):
            return meeting_input

        # Telemost room ID (alphanumeric)
        if re.match(r"^[a-zA-Z0-9\-]+$", meeting_input):
            return f"https://telemost.yandex.ru/{meeting_input}"

        return meeting_input
