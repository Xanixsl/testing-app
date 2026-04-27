"""Обёртка над python-vlc для воспроизведения аудио по URL."""
from __future__ import annotations

import os
import sys
from typing import Callable


def _ensure_vlc_on_path() -> None:
    """На Windows libvlc.dll лежит рядом с VLC. Подхватываем её до import vlc."""
    if sys.platform != "win32":
        return
    candidates = [
        os.environ.get("VLC_HOME"),
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ]
    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "libvlc.dll")):
            try:
                os.add_dll_directory(path)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_vlc_on_path()
import vlc  # noqa: E402


class Player:
    def __init__(self) -> None:
        # --no-video чтобы VLC не пытался открыть окно
        self._vlc = vlc.Instance("--no-video", "--quiet")
        self._mp: vlc.MediaPlayer = self._vlc.media_player_new()
        self._volume = 70
        self._mp.audio_set_volume(self._volume)
        self._on_end: Callable[[], None] | None = None
        em = self._mp.event_manager()
        em.event_attach(vlc.EventType.MediaPlayerEndReached, self._handle_end)

    # ---- управление ----------------------------------------------------
    def play_url(self, url: str) -> None:
        media = self._vlc.media_new(url)
        self._mp.set_media(media)
        self._mp.play()

    def toggle_pause(self) -> None:
        self._mp.pause()

    def stop(self) -> None:
        self._mp.stop()

    def set_volume(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._mp.audio_set_volume(self._volume)

    @property
    def volume(self) -> int:
        return self._volume

    def seek(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self._mp.set_position(fraction)

    # ---- состояние -----------------------------------------------------
    @property
    def position(self) -> float:
        return float(self._mp.get_position() or 0.0)

    @property
    def length_ms(self) -> int:
        return int(self._mp.get_length() or 0)

    @property
    def time_ms(self) -> int:
        return int(self._mp.get_time() or 0)

    @property
    def is_playing(self) -> bool:
        return bool(self._mp.is_playing())

    # ---- события -------------------------------------------------------
    def on_end(self, callback: Callable[[], None]) -> None:
        self._on_end = callback

    def _handle_end(self, _event) -> None:  # noqa: ANN001
        if self._on_end:
            try:
                self._on_end()
            except Exception:
                pass

    def release(self) -> None:
        try:
            self._mp.stop()
            self._mp.release()
            self._vlc.release()
        except Exception:
            pass
