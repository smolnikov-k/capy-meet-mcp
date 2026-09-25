import struct
from pathlib import Path

import pytest

from capy_meet_mcp import audio, store


@pytest.mark.parametrize("url,platform", [
    ("https://telemost.yandex.ru/j/12345678901234", "telemost"),
    ("https://telemost.360.yandex.ru/j/12345678901234", "telemost"),
    ("https://meet.google.com/abc-defg-hij", "google_meet"),
    ("https://us02web.zoom.us/j/123456789?pwd=x", "zoom"),
    ("https://zoom.us/j/123456789", "zoom"),
    ("https://company.webex.com/meet/room", "webex"),
    ("https://example.com/meeting", None),
    ("not a url", None),
    ("https://evil-telemost.yandex.ru.example.com/j/1", None),
])
def test_detect_platform(url, platform):
    assert store.detect_platform(url) == platform


def test_rec_dir_rejects_foreign_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPY_MEET_HOME", str(tmp_path))
    rec_id = store.new_id("telemost")
    assert store.rec_dir(rec_id).parent == tmp_path / "recordings"
    for bad in ("../etc", "20260925-120000-telemost-zzzz", "", "a/b"):
        with pytest.raises(ValueError):
            store.rec_dir(bad)


def test_update_state_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPY_MEET_HOME", str(tmp_path))
    rec_id = store.new_id("zoom")
    store.rec_dir(rec_id).mkdir(parents=True)
    store.update_state(rec_id, phase="joining")
    state = store.update_state(rec_id, worker_pid=1)
    assert state == {"phase": "joining", "worker_pid": 1}
    assert store.list_ids() == [rec_id]


def _wav(path: Path, samples: list[int]) -> None:
    path.write_bytes(b"\0" * audio.HEADER_BYTES + struct.pack(f"<{len(samples)}h", *samples))


def test_tail_level_silence_and_sound(tmp_path):
    silent = tmp_path / "silent.wav"
    _wav(silent, [0] * audio.SAMPLE_RATE)
    assert audio.tail_level(silent).silent

    loud = tmp_path / "loud.wav"
    _wav(loud, [8000, -8000] * (audio.SAMPLE_RATE // 2))
    level = audio.tail_level(loud)
    assert not level.silent
    assert -15 < level.rms_db < -10
    assert audio.duration_seconds(loud) == pytest.approx(1.0)


def test_missing_wav(tmp_path):
    missing = tmp_path / "none.wav"
    assert audio.duration_seconds(missing) == 0
    assert audio.tail_level(missing).silent
    assert audio.growth(missing, 0) == 0
