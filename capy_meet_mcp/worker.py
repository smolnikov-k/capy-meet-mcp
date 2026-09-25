"""One recording = one worker process.

The MCP server starts this module detached and returns immediately. The worker
owns everything the recording needs and cleans it up itself:

    PulseAudio null sink (its own, so parallel recordings never mix)
    Xvfb display (Chromium must run headed: headless builds have no audio out)
    the bot engine (Playwright Chromium + FFmpeg recorder)
    local transcription of the finished WAV (faster-whisper, no network keys)

Stop is SIGTERM to the worker: it asks the engine to leave, waits, and
escalates to killing the engine's process group only if leaving hangs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from capy_meet_mcp import store
from capy_meet_mcp.audio import duration_seconds

log = logging.getLogger("capy_meet.worker")

LEAVE_TIMEOUT = 60
# Chromium has been seen to stop feeding audio mid-call while FFmpeg keeps
# running; a WAV that stops growing this long is flagged in state.json.
STALL_SECONDS = 180


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)


def ensure_pulseaudio() -> None:
    if _run(["pulseaudio", "--check"]).returncode == 0:
        return
    res = _run(["pulseaudio", "--start", "--exit-idle-time=-1"])
    if res.returncode != 0 and _run(["pulseaudio", "--check"]).returncode != 0:
        raise RuntimeError(f"PulseAudio не запустился: {res.stderr.strip()[-300:]}")


def load_sink(sink: str) -> str:
    res = _run([
        "pactl", "load-module", "module-null-sink",
        f"sink_name={sink}", f"sink_properties=device.description={sink}",
    ])
    if res.returncode != 0:
        raise RuntimeError(f"не удалось создать звуковое устройство: {res.stderr.strip()[-300:]}")
    return res.stdout.strip()


def unload_sink(module_id: str | None) -> None:
    if module_id:
        _run(["pactl", "unload-module", module_id])


def start_xvfb() -> tuple[subprocess.Popen, str]:
    """Xvfb on a free display; -displayfd reports the number it picked."""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    os.close(write_fd)
    with os.fdopen(read_fd) as fh:
        number = fh.readline().strip()
    if not number:
        proc.kill()
        raise RuntimeError("Xvfb не запустился")
    return proc, f":{number}"


def kill_group(proc: subprocess.Popen | None, sig: int = signal.SIGTERM) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_process(proc: subprocess.Popen | None, timeout: float = 10) -> None:
    if proc is None or proc.poll() is not None:
        kill_group(proc, signal.SIGKILL)  # leftovers such as orphaned Chromium
        return
    kill_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc, signal.SIGKILL)
        proc.wait(timeout=5)


def transcribe(rec_id: str) -> None:
    d = store.rec_dir(rec_id)
    wav = d / "audio.wav"
    store.update_state(rec_id, phase="transcribing", transcribe_started=_now(), transcribed_seconds=0)

    from capy_meet_mcp.transcriber.whisper_engine import WhisperTranscriber

    engine = WhisperTranscriber()
    last = [0.0]

    def progress(end: float) -> None:
        if end - last[0] >= 30:
            last[0] = end
            store.update_state(rec_id, transcribed_seconds=round(end))

    engine.on_progress = progress
    result = asyncio.run(engine.transcribe(str(wav)))
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in result.segments]
    store.write_json(d / "segments.json", {"language": result.language, "segments": segments})
    lines = [f"[{store.hhmmss(s['start'])}] {s['text']}" for s in segments]
    tmp = d / "transcript.txt.tmp"
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, d / "transcript.txt")
    store.update_state(
        rec_id, phase="done", finished=_now(), language=result.language,
        segments=len(segments), transcribed_seconds=round(result.duration_seconds),
    )
    log.info("transcript ready: %d segments", len(segments))


def record(rec_id: str) -> int:
    d = store.rec_dir(rec_id)
    meta = store.read_json(d / "meta.json")
    wav = d / "audio.wav"
    stop = {"requested": False}

    def on_term(signum, frame):
        stop["requested"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    sink = "capymeet_" + rec_id.replace("-", "_")
    module_id = xvfb = engine = None
    try:
        ensure_pulseaudio()
        module_id = load_sink(sink)
        xvfb, display = start_xvfb()
        env = dict(os.environ)
        env.update({
            "DISPLAY": display,
            "PULSE_SINK": sink,
            "AUDIO_PATH": str(wav),
            "RECORDINGS_DIR": str(d),
            "TRANSCRIPTS_DIR": str(d),
            "LIVE_TRANSCRIBE": "0",
            "PYTHONUNBUFFERED": "1",
        })
        with (d / "engine.log").open("ab") as engine_log:
            engine = subprocess.Popen(
                [sys.executable, "-m", "capy_meet_mcp.bot.engine",
                 "--platform", meta["platform"], "--meeting-url", meta["url"],
                 "--display-name", meta["display_name"]],
                cwd=str(d), env=env, stdout=engine_log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        store.update_state(rec_id, phase="joining", engine_pid=engine.pid, sink=sink, display=display)
        log.info("engine started pid=%s sink=%s display=%s", engine.pid, sink, display)

        last_size, last_change = -1, time.time()
        stop_sent_at = None
        while engine.poll() is None:
            time.sleep(1)
            if stop["requested"] and stop_sent_at is None:
                log.info("leave requested")
                store.update_state(rec_id, phase="leaving", leave_requested=_now())
                try:
                    engine.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                stop_sent_at = time.time()
            if stop_sent_at and time.time() - stop_sent_at > LEAVE_TIMEOUT:
                log.warning("engine did not leave in %ss, killing", LEAVE_TIMEOUT)
                kill_group(engine, signal.SIGKILL)
                break
            if wav.exists():
                size = wav.stat().st_size
                now = time.time()
                if size != last_size:
                    if last_size < 0:
                        store.update_state(rec_id, phase="recording", recording_started=_now())
                    last_size, last_change = size, now
                    store.update_state(rec_id, stalled=False)
                elif now - last_change > STALL_SECONDS and not stop_sent_at:
                    store.update_state(rec_id, stalled=True)
        rc = engine.wait()
        log.info("engine exited rc=%s", rc)
    except Exception as exc:  # noqa: BLE001 - every failure must land in state.json
        log.exception("recording failed")
        store.update_state(rec_id, phase="failed", finished=_now(), error=str(exc))
        return 1
    finally:
        stop_process(engine)
        stop_process(xvfb, timeout=5)
        unload_sink(module_id)

    seconds = duration_seconds(wav)
    store.update_state(rec_id, left=_now(), audio_seconds=round(seconds))
    if seconds < 1:
        tail = ""
        try:
            tail = (d / "engine.log").read_text(errors="replace")[-1500:]
        except FileNotFoundError:
            pass
        # The last exception line tells "browser did not start" apart from
        # "the meeting page refused us"; both end without a recording.
        reason = next((ln.strip() for ln in reversed(tail.splitlines())
                       if "Error" in ln and not ln.startswith(" ")), "")
        error = "не удалось войти во встречу, запись не начиналась"
        if reason:
            error += f" ({reason[:200]})"
        store.update_state(
            rec_id, phase="failed", finished=_now(), error=error, engine_log_tail=tail,
        )
        return 1
    return run_transcription(rec_id)


def run_transcription(rec_id: str) -> int:
    try:
        transcribe(rec_id)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("transcription failed")
        store.update_state(rec_id, phase="transcribe_failed", finished=_now(), error=str(exc))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capy-meet-mcp recording worker")
    parser.add_argument("rec_id")
    parser.add_argument("--transcribe-only", action="store_true")
    args = parser.parse_args(argv)

    d = store.rec_dir(args.rec_id)
    logging.basicConfig(
        filename=str(d / "worker.log"), level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    store.update_state(args.rec_id, worker_pid=os.getpid())
    if args.transcribe_only:
        return run_transcription(args.rec_id)
    return record(args.rec_id)


if __name__ == "__main__":
    sys.exit(main())
