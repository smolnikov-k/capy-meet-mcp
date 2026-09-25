"""MCP server: record online meetings and transcribe them locally.

Tools never block for the length of a call. ``join_meeting`` starts a detached
worker and waits only until the recording is proven to be written (the WAV
exists and grows between two probes); everything after that is polled with
``meeting_status``. A log line saying a button was clicked is not proof of
joining, a growing WAV is.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from capy_meet_mcp import store
from capy_meet_mcp.audio import duration_seconds, growth, tail_level

mcp = FastMCP("capy-meet")

ACTIVE_PHASES = {"starting", "joining", "recording", "leaving"}
DEFAULT_NAME = os.getenv("CAPY_MEET_DISPLAY_NAME", "Запись встречи")
TRANSCRIPT_LIMIT = 20000


def _state(rec_id: str) -> tuple[dict, dict]:
    d = store.rec_dir(rec_id)
    if not d.exists():
        raise ValueError(f"записи {rec_id} нет")
    return store.read_json(d / "meta.json"), store.read_json(d / "state.json")


def _spawn_worker(rec_id: str, *extra: str) -> int:
    d = store.rec_dir(rec_id)
    with (d / "worker.out").open("ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "capy_meet_mcp.worker", rec_id, *extra],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    return proc.pid


def _probe(rec_id: str) -> dict:
    """Live facts about the WAV: grows, sound in the tail, length."""
    wav = store.rec_dir(rec_id) / "audio.wav"
    if not wav.exists():
        return {"wav_exists": False}
    grew = growth(wav, 2.0)
    tail = tail_level(wav, 20.0)
    return {
        "wav_exists": True,
        "wav_growing": grew > 0,
        "minutes": round(duration_seconds(wav) / 60, 1),
        "tail_rms_db": None if tail.rms_db == float("-inf") else round(tail.rms_db, 1),
        "tail_silent": tail.silent,
    }


def _verdict(state: dict, probe: dict, worker_alive: bool) -> str:
    phase = state.get("phase", "starting")
    if phase == "failed":
        return f"не получилось: {state.get('error', 'причина в engine.log')}"
    if phase == "transcribe_failed":
        return f"запись есть, расшифровка упала: {state.get('error')}. Повторить: transcribe_recording"
    if phase == "done":
        return "готово: запись и расшифровка лежат на месте, текст отдаёт get_transcript"
    if phase == "transcribing":
        if not worker_alive:
            return "расшифровка оборвалась. Повторить: transcribe_recording"
        done = state.get("transcribed_seconds", 0)
        total = state.get("audio_seconds") or 0
        return f"из встречи вышел, идёт расшифровка ({store.hhmmss(done)} из {store.hhmmss(total)})"
    if not worker_alive:
        return "процесс записи умер без выхода из встречи. Если WAV есть: transcribe_recording"
    if phase in ("starting", "joining") and not probe.get("wav_exists"):
        return "захожу во встречу, запись ещё не началась"
    if phase == "leaving":
        return "выхожу из встречи"
    if not probe.get("wav_growing"):
        return "🔴 файл записи не растёт: запись встала. Выйти (leave_meeting) и зайти заново"
    if state.get("stalled"):
        return "🔴 файл записи долго не рос, проверь ещё раз"
    if probe.get("tail_silent"):
        return ("в звонке, файл растёт, но последние 20 секунд тишина. Если люди "
                "говорят, а тишина держится, звук не доходит: перезайти")
    return "в звонке, пишу, звук есть"


@mcp.tool(annotations=ToolAnnotations(title="Зайти во встречу и начать запись", destructiveHint=False, openWorldHint=True))
def join_meeting(url: str, display_name: str = "", platform: str = "auto", wait_seconds: int = 90) -> dict:
    """Зайти в онлайн-встречу молча (микрофон и камера выключены) и начать запись.

    Платформы: Яндекс Телемост (telemost.yandex.ru и telemost.360.yandex.ru),
    Google Meet, Zoom, Webex. platform="auto" определяет её по ссылке.
    Возвращает id записи. Вход считается состоявшимся только когда файл записи
    появился и растёт; ждём этого не дольше wait_seconds, дальше проверять
    через meeting_status. Если по этой ссылке запись уже идёт, вернёт её id,
    второго бота не запускает.
    """
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        return {"ok": False, "error": "нужна ссылка на встречу целиком, начиная с https://"}
    if platform == "auto":
        detected = store.detect_platform(url)
        if detected is None:
            return {"ok": False, "error": "платформа по ссылке не определилась, укажи platform: "
                    + ", ".join(store.PLATFORMS)}
        platform = detected
    elif platform not in store.PLATFORMS:
        return {"ok": False, "error": f"platform должна быть одной из: {', '.join(store.PLATFORMS)}"}

    for other in store.list_ids()[:20]:
        meta, state = _state(other)
        if meta.get("url") == url and state.get("phase") in ACTIVE_PHASES \
                and store.pid_alive(state.get("worker_pid")):
            return {"ok": True, "id": other, "already_running": True,
                    "status": _verdict(state, _probe(other), True)}

    rec_id = store.new_id(platform)
    d = store.rec_dir(rec_id)
    d.mkdir(parents=True)
    store.write_json(d / "meta.json", {
        "url": url, "platform": platform, "display_name": display_name or DEFAULT_NAME,
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    store.write_json(d / "state.json", {"phase": "starting"})
    pid = _spawn_worker(rec_id)
    store.update_state(rec_id, worker_pid=pid)

    deadline = time.time() + max(10, min(wait_seconds, 300))
    while time.time() < deadline:
        time.sleep(2)
        _, state = _state(rec_id)
        if state.get("phase") == "failed":
            return {"ok": False, "id": rec_id, "error": state.get("error"),
                    "engine_log_tail": state.get("engine_log_tail", "")[-600:], "dir": str(d),
                    "screenshot": str(d / "debug_failed_join.png")
                    if (d / "debug_failed_join.png").exists() else None}
        if (d / "audio.wav").exists():
            probe = _probe(rec_id)
            if probe.get("wav_growing"):
                return {"ok": True, "id": rec_id, "joined": True, **probe,
                        "status": _verdict(state, probe, store.pid_alive(state.get("worker_pid")))}
    _, state = _state(rec_id)
    return {"ok": True, "id": rec_id, "joined": False,
            "status": "вход ещё не подтверждён: запись не началась за отведённое время, "
                      "проверь meeting_status через минуту",
            "phase": state.get("phase")}


@mcp.tool(annotations=ToolAnnotations(title="Состояние записи", readOnlyHint=True))
def meeting_status(id: str) -> dict:
    """Состояние записи: в звонке ли бот, растёт ли файл, есть ли звук в последних
    20 секундах, сколько минут записано, идёт ли расшифровка."""
    meta, state = _state(id)
    alive = store.pid_alive(state.get("worker_pid"))
    probe = _probe(id) if state.get("phase") in ACTIVE_PHASES else {}
    return {
        "id": id, "platform": meta.get("platform"), "url": meta.get("url"),
        "phase": state.get("phase"), "worker_alive": alive, **probe,
        "audio_minutes": round(duration_seconds(store.rec_dir(id) / "audio.wav") / 60, 1),
        "status": _verdict(state, probe, alive),
    }


@mcp.tool(annotations=ToolAnnotations(title="Выйти из встречи", destructiveHint=False))
def leave_meeting(id: str) -> dict:
    """Выйти из встречи, закончить запись и запустить локальную расшифровку.
    Расшифровка идёт в фоне; готовый текст отдаёт get_transcript."""
    _, state = _state(id)
    pid = state.get("worker_pid")
    if state.get("phase") not in ACTIVE_PHASES or not store.pid_alive(pid):
        return {"ok": False, "id": id, "phase": state.get("phase"),
                "status": "запись уже не идёт"}
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(2)
        _, state = _state(id)
        if state.get("phase") not in ACTIVE_PHASES:
            break
    alive = store.pid_alive(state.get("worker_pid"))
    return {"ok": True, "id": id, "phase": state.get("phase"),
            "audio_minutes": round((state.get("audio_seconds") or 0) / 60, 1),
            "status": _verdict(state, {}, alive)}


@mcp.tool(annotations=ToolAnnotations(title="Текст расшифровки", readOnlyHint=True))
def get_transcript(id: str, offset: int = 0) -> dict:
    """Расшифровка встречи с таймкодами [ЧЧ:ММ:СС] и путь к WAV.
    Длинный текст отдаётся частями: следующий кусок с offset=next_offset."""
    _, state = _state(id)
    d = store.rec_dir(id)
    text_path = d / "transcript.txt"
    result = {"id": id, "phase": state.get("phase"),
              "wav": str(d / "audio.wav") if (d / "audio.wav").exists() else None,
              "transcript_path": str(text_path) if text_path.exists() else None}
    if not text_path.exists():
        alive = store.pid_alive(state.get("worker_pid"))
        result["status"] = _verdict(state, {}, alive)
        return result
    text = text_path.read_text(encoding="utf-8")
    chunk = text[offset:offset + TRANSCRIPT_LIMIT]
    result.update({"language": state.get("language"), "text": chunk,
                   "total_chars": len(text)})
    if offset + TRANSCRIPT_LIMIT < len(text):
        result["next_offset"] = offset + TRANSCRIPT_LIMIT
    if not text.strip():
        result["status"] = "речи в записи не найдено (тишина или звук не доходил)"
    return result


@mcp.tool(annotations=ToolAnnotations(title="Последние записи", readOnlyHint=True))
def list_meetings(limit: int = 10) -> dict:
    """Последние записи: id, платформа, ссылка, когда, фаза, длительность."""
    items = []
    for rec_id in store.list_ids()[: max(1, min(limit, 100))]:
        meta, state = _state(rec_id)
        items.append({
            "id": rec_id, "platform": meta.get("platform"), "url": meta.get("url"),
            "created": meta.get("created"), "phase": state.get("phase"),
            "audio_minutes": round(duration_seconds(store.rec_dir(rec_id) / "audio.wav") / 60, 1),
        })
    return {"meetings": items}


@mcp.tool(annotations=ToolAnnotations(title="Повторить расшифровку", destructiveHint=False))
def transcribe_recording(id: str) -> dict:
    """Заново расшифровать готовую запись: если расшифровка упала или процесс
    записи умер, а WAV остался. Идёт в фоне, результат в get_transcript."""
    _, state = _state(id)
    d = store.rec_dir(id)
    if store.pid_alive(state.get("worker_pid")):
        return {"ok": False, "status": "по этой записи ещё работает процесс, дождись его"}
    if duration_seconds(d / "audio.wav") < 1:
        return {"ok": False, "status": "записи нет, расшифровывать нечего"}
    store.update_state(id, audio_seconds=round(duration_seconds(d / "audio.wav")),
                       phase="transcribing", error=None)
    pid = _spawn_worker(id, "--transcribe-only")
    store.update_state(id, worker_pid=pid)
    return {"ok": True, "id": id, "status": "расшифровка запущена"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
