"""Anti-detection utilities for headless browser automation."""

from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def get_realistic_user_agent() -> str:
    """Return a realistic Chrome on Linux User-Agent string."""
    return random.choice(_USER_AGENTS)


async def random_delay(min_s: float = 2.0, max_s: float = 8.0) -> None:
    """Sleep for a random duration to mimic human behavior."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Random delay: %.1fs", delay)
    await asyncio.sleep(delay)


async def apply_stealth(page: Page) -> None:
    """Apply stealth patches to reduce bot detection signals."""
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
        logger.info("Playwright stealth applied")
    except ImportError:
        logger.warning("playwright-stealth not installed, applying manual patches")
        await _manual_stealth(page)


async def _manual_stealth(page: Page) -> None:
    """Fallback stealth patches when playwright-stealth is unavailable."""
    await page.add_init_script("""
        // Remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Fake plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });

        // Fake languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });

        // Remove automation flags
        delete window.__playwright;
        delete window.__pw_manual;

        // Chrome runtime mock
        window.chrome = { runtime: {} };
    """)


async def human_like_click(page: Page, selector: str) -> None:
    """Click an element with slight mouse movement randomness."""
    element = await page.wait_for_selector(selector, timeout=15000)
    if not element:
        raise ValueError(f"Element not found: {selector}")

    box = await element.bounding_box()
    if not box:
        await element.click()
        return

    # Add small random offset within the element
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    await page.mouse.move(x, y, steps=random.randint(5, 15))
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.click(x, y)
    logger.debug("Clicked %s at (%.0f, %.0f)", selector, x, y)
