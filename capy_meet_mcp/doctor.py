"""Check that the machine can record meetings: capy-meet-doctor."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _ok(flag: bool, text: str, fix: str = "") -> bool:
    print(("✅ " if flag else "🔴 ") + text + ("" if flag or not fix else f"\n   исправить: {fix}"))
    return flag


def main() -> int:
    good = True
    apt = "apt-get install -y pulseaudio xvfb ffmpeg"
    for binary in ("ffmpeg", "Xvfb", "pulseaudio", "pactl"):
        good &= _ok(shutil.which(binary) is not None, f"{binary} есть в системе", apt)

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
        full = Path(str(path).replace("chromium_headless_shell", "chromium"))
        exists = path.exists() or full.exists()
        good &= _ok(exists, f"Chromium для Playwright: {full if full.exists() else path}",
                    f"{sys.executable} -m playwright install --with-deps chromium")
        if exists and "headless_shell" in str(path) and not full.exists():
            good &= _ok(False, "стоит только headless-сборка без звука",
                        f"{sys.executable} -m playwright install chromium")
    except Exception as exc:  # noqa: BLE001
        good &= _ok(False, f"Playwright не запускается: {exc}")

    try:
        import faster_whisper  # noqa: F401

        good &= _ok(True, "faster-whisper установлен (модель скачается при первой расшифровке)")
    except ImportError:
        good &= _ok(False, "faster-whisper не установлен", "uv sync")

    if shutil.which("pulseaudio"):
        check = subprocess.run(["pulseaudio", "--check"], capture_output=True)
        if check.returncode != 0:
            subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], capture_output=True)
            check = subprocess.run(["pulseaudio", "--check"], capture_output=True)
        good &= _ok(check.returncode == 0, "PulseAudio запускается",
                    "запустить от того же пользователя, что и агент: pulseaudio --start")

    print("\nГотово к записи." if good else "\nЕсть что исправить, см. выше.")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
