"""Унифицированные модели данных треков и артистов."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
    duration: int = 0  # секунды
    cover_small: str = ""
    cover_big: str = ""
    preview_url: str = ""  # 30-секундное превью (если есть)
    source: str = "deezer"
    artist_id: str = ""
    explicit: bool = False  # пометка E (18+)
    album_id: str = ""
    # Полный список артистов (включая фитов). Каждый: {"id": str, "name": str}.
    # Если пуст — клиент использует одно поле artist.
    artists: list = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.artist} — {self.title}"


@dataclass
class Artist:
    id: str
    name: str
    picture_small: str = ""
    picture_big: str = ""
    fans: int = 0
    nb_album: int = 0
    bio: str = ""
    source: str = "deezer"
    top_tracks: list[Track] = field(default_factory=list)
    albums: list[dict] = field(default_factory=list)
