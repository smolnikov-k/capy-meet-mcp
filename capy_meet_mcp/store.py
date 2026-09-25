"""Recording directory layout and JSON state shared by the server and workers.

Every recording lives in its own directory under ``$CAPY_MEET_HOME/recordings``:

    meta.json        what was asked: url, platform, display name, created
    state.json       what the worker reports: phase, pids, timestamps, error
    audio.wav        the recording
    engine.log       browser and recorder log
    worker.log       worker log
    segments.json    transcript segments with start/end seconds
    transcript.txt   transcript with [HH:MM:SS] timecodes
    debug_failed_join.png  screenshot when joining failed
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PLATFORMS = ("telemost", "google_meet", "zoom", "webex")

# Phases written by the worker. Final phases never change again.
FINAL_PHASES = {"done", "failed", "transcribe_failed"}


def home() -> Path:
    base = os.getenv("CAPY_MEET_HOME") or str(Path.home() / ".capy-meet")
    return Path(base).expanduser()


def recordings_root() -> Path:
    root = home() / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def detect_platform(url: str) -> str | None:
    """Platform by meeting link; None when the link is not recognised."""
    host = (urlparse(url.strip()).hostname or "").lower()
    if not host:
        return None
    if host.startswith("telemost.") and host.endswith("yandex.ru"):
        # telemost.yandex.ru and telemost.360.yandex.ru are the same service.
        return "telemost"
    if host == "meet.google.com":
        return "google_meet"
    if host == "zoom.us" or host.endswith(".zoom.us") or host.endswith("zoomgov.com"):
        return "zoom"
    if host == "webex.com" or host.endswith(".webex.com"):
        return "webex"
    return None


_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z_]+-[0-9a-f]{4}$")


def new_id(platform: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{platform}-{secrets.token_hex(2)}"


def rec_dir(rec_id: str) -> Path:
    # The id arrives from the model: never let it address anything outside
    # the recordings directory.
    if not _ID_RE.match(rec_id or ""):
        raise ValueError(f"unknown recording id: {rec_id!r}")
    return recordings_root() / rec_id


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def update_state(rec_id: str, **fields) -> dict:
    """Read-modify-write under a lock: the server and the worker both write."""
    d = rec_dir(rec_id)
    with (d / ".state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(d / "state.json")
        state.update(fields)
        write_json(d / "state.json", state)
    return state


def list_ids() -> list[str]:
    ids = [p.name for p in recordings_root().iterdir() if p.is_dir() and _ID_RE.match(p.name)]
    return sorted(ids, reverse=True)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as gone.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return True


def worker_alive(pid: int | None, rec_id: str) -> bool:
    """The recording worker is running: the pid is alive AND is our worker
    for this id. State lives on disk, so this works for a server restarted
    after the worker was spawned; the cmdline check guards against pid reuse."""
    if not pid_alive(pid):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return True
    return b"capy_meet_mcp.worker" in cmdline and rec_id.encode() in cmdline


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
