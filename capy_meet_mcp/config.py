"""Recorder configuration — all settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    display_name: str
    recordings_dir: Path
    transcripts_dir: Path
    database_path: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    log_level: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> Settings:
        """Create settings from environment variables."""
        return cls(
            display_name=os.getenv("DISPLAY_NAME", "Запись встречи"),
            recordings_dir=Path(os.getenv("RECORDINGS_DIR", "./recordings")),
            transcripts_dir=Path(os.getenv("TRANSCRIPTS_DIR", "./transcripts")),
            database_path=os.getenv("DATABASE_PATH", "meetscribe.db"),
            whisper_model=os.getenv("WHISPER_MODEL", "small"),
            whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
            whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


settings = Settings.from_env()
