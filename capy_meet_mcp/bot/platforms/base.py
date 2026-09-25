"""Abstract base class for platform-specific meeting joiners."""

from __future__ import annotations

from abc import ABC, abstractmethod

from playwright.async_api import Page


class PlatformJoiner(ABC):
    """Base class for meeting platform automation."""

    @abstractmethod
    async def join(self, page: Page, meeting_url: str, display_name: str) -> bool:
        """Join a meeting. Returns True if successfully joined."""
        ...

    @abstractmethod
    async def is_in_meeting(self, page: Page) -> bool:
        """Check if the bot is still in an active meeting."""
        ...

    @abstractmethod
    async def leave_meeting(self, page: Page) -> None:
        """Gracefully leave the meeting."""
        ...

    @abstractmethod
    def parse_meeting_url(self, meeting_input: str) -> str:
        """Normalize meeting input (URL or ID) to a joinable URL."""
        ...
