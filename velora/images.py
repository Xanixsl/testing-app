"""Загрузка и кэширование изображений (обложки/аватарки)."""
from __future__ import annotations

import io
import threading
from functools import lru_cache

from PIL import Image

from velora.api.http import SESSION, DEFAULT_TIMEOUT

_lock = threading.Lock()


@lru_cache(maxsize=512)
def _fetch_bytes(url: str) -> bytes:
    if not url:
        return b""
    with _lock:
        r = SESSION.get(url, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.content


def load_image(url: str, size: tuple[int, int]) -> Image.Image | None:
    if not url:
        return None
    try:
        data = _fetch_bytes(url)
        if not data:
            return None
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail(size, Image.Resampling.LANCZOS)
        return img
    except Exception:
        return None
