"""Клиент для публичного Deezer API.

Документация: https://developers.deezer.com/api
Ключи не нужны. Если основной хост недоступен — используется
зеркало через api.allorigins.win как fallback.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from velora.api.http import SESSION, DEFAULT_TIMEOUT
from velora.models import Artist, Track

BASE = "https://api.deezer.com"

# Простой TTL-кэш для дорогих эндпоинтов (related/top_tracks/charts).
# Ключ → (expires_at, value). Защищён локом — потоки в волне могут
# одновременно дёргать один и тот же ключ.
_TTL_CACHE: dict[str, tuple[float, Any]] = {}
_TTL_LOCK = threading.Lock()
_TTL_DEFAULT = 30 * 60  # 30 минут — артистные данные почти не меняются


def _cache_get(key: str):
    with _TTL_LOCK:
        v = _TTL_CACHE.get(key)
    if not v:
        return None
    if v[0] < time.time():
        with _TTL_LOCK:
            _TTL_CACHE.pop(key, None)
        return None
    return v[1]


def _cache_put(key: str, value: Any, ttl: int = _TTL_DEFAULT) -> None:
    with _TTL_LOCK:
        _TTL_CACHE[key] = (time.time() + ttl, value)
        # Грубая обрезка размера, чтоб не разрасталось.
        if len(_TTL_CACHE) > 2000:
            now = time.time()
            for k in [k for k, vv in _TTL_CACHE.items() if vv[0] < now]:
                _TTL_CACHE.pop(k, None)
            if len(_TTL_CACHE) > 2000:
                # Удалим самые старые 200 записей.
                items = sorted(_TTL_CACHE.items(), key=lambda kv: kv[1][0])[:200]
                for k, _ in items:
                    _TTL_CACHE.pop(k, None)


def _get(path: str, **params: Any) -> dict:
    url = f"{BASE}{path}"
    try:
        r = SESSION.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        # CORS-прокси как запасной канал (короткий таймаут, чтобы не вешать UI)
        try:
            proxy = "https://api.allorigins.win/raw"
            r = SESSION.get(proxy, params={"url": _build(url, params)}, timeout=6)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}


def _build(url: str, params: dict) -> str:
    if not params:
        return url
    from urllib.parse import urlencode

    return f"{url}?{urlencode(params)}"


def _track_from_json(j: dict) -> Track:
    artist = j.get("artist") or {}
    album = j.get("album") or {}
    # Contributors есть только в /track/{id}, в search-результатах нет.
    # Если есть — собираем всех артистов (включая фитов).
    contributors = j.get("contributors") or []
    artists_list: list[dict] = []
    if contributors:
        seen = set()
        for c in contributors:
            cid = str(c.get("id", "") or "")
            cname = c.get("name") or ""
            if cname and cname not in seen:
                seen.add(cname)
                artists_list.append({"id": cid, "name": cname})
        # Главный артист — первый в списке (Deezer гарантирует порядок).
        artist_display = ", ".join(a["name"] for a in artists_list) or artist.get("name", "")
    else:
        artist_display = artist.get("name", "")
    return Track(
        id=str(j.get("id", "")),
        title=j.get("title", ""),
        artist=artist_display,
        album=album.get("title", ""),
        duration=int(j.get("duration", 0) or 0),
        cover_small=album.get("cover_small") or artist.get("picture_small", ""),
        cover_big=album.get("cover_xl") or album.get("cover_big") or artist.get("picture_xl", ""),
        preview_url=j.get("preview", "") or "",
        source="deezer",
        artist_id=str(artist.get("id", "")),
        explicit=bool(j.get("explicit_lyrics") or j.get("explicit_content_lyrics", 0) in (1, 4)),
        album_id=str(album.get("id", "") or ""),
        artists=artists_list,
    )


def get_track(track_id: str) -> Track | None:
    """Полная инфо о треке: с contributors (фиты) и album_id."""
    if not track_id:
        return None
    try:
        j = _get(f"/track/{track_id}")
    except Exception:
        return None
    if not j or j.get("error"):
        return None
    return _track_from_json(j)


def search_tracks(query: str, limit: int = 40) -> list[Track]:
    if not query.strip():
        return []
    data = _get("/search", q=query, limit=limit)
    return [_track_from_json(x) for x in data.get("data", [])]


def search_artists(query: str, limit: int = 20) -> list[Artist]:
    if not query.strip():
        return []
    data = _get("/search/artist", q=query, limit=limit)
    out: list[Artist] = []
    for x in data.get("data", []):
        out.append(
            Artist(
                id=str(x.get("id", "")),
                name=x.get("name", ""),
                picture_small=x.get("picture_small", ""),
                picture_big=x.get("picture_xl") or x.get("picture_big", ""),
                fans=int(x.get("nb_fan", 0) or 0),
                nb_album=int(x.get("nb_album", 0) or 0),
                source="deezer",
            )
        )
    return out


def get_artist(artist_id: str) -> Artist:
    info = _get(f"/artist/{artist_id}")
    top = _get(f"/artist/{artist_id}/top", limit=50)
    albums = _get(f"/artist/{artist_id}/albums", limit=100)
    a = Artist(
        id=str(info.get("id", "")),
        name=info.get("name", ""),
        picture_small=info.get("picture_small", ""),
        picture_big=info.get("picture_xl") or info.get("picture_big", ""),
        fans=int(info.get("nb_fan", 0) or 0),
        nb_album=int(info.get("nb_album", 0) or 0),
        source="deezer",
    )
    a.top_tracks = [_track_from_json(t) for t in top.get("data", [])]
    a.albums = [
        {
            "id": str(al.get("id", "")),
            "title": al.get("title", ""),
            "cover": al.get("cover_big") or al.get("cover_medium", ""),
            "cover_small": al.get("cover_small", ""),
            "year": (al.get("release_date") or "")[:4],
            "release_date": al.get("release_date", ""),
            "nb_tracks": int(al.get("nb_tracks", 0) or 0),
            "record_type": al.get("record_type", ""),
            "explicit_lyrics": bool(al.get("explicit_lyrics", False)),
            "fans": int(al.get("fans", 0) or 0),
        }
        for al in albums.get("data", [])
    ]
    return a


def get_album(album_id: str) -> dict:
    """Полные метаданные альбома + список треков."""
    from dataclasses import asdict as _asdict
    data = _get(f"/album/{album_id}")
    cover_big = data.get("cover_xl") or data.get("cover_big", "")
    cover_small = data.get("cover_small", "")
    artist = data.get("artist") or {}
    tracks: list[Track] = []
    for x in data.get("tracks", {}).get("data", []):
        tracks.append(
            Track(
                id=str(x.get("id", "")),
                title=x.get("title", ""),
                artist=(x.get("artist") or artist).get("name", ""),
                album=data.get("title", ""),
                duration=int(x.get("duration", 0) or 0),
                cover_small=cover_small,
                cover_big=cover_big,
                preview_url=x.get("preview", "") or "",
                source="deezer",
                artist_id=str((x.get("artist") or artist).get("id", "")),
            )
        )
    return {
        "id": str(data.get("id", album_id)),
        "title": data.get("title", ""),
        "artist": artist.get("name", ""),
        "artist_id": str(artist.get("id", "")),
        "cover": cover_big,
        "cover_small": cover_small,
        "release_date": data.get("release_date", ""),
        "year": (data.get("release_date") or "")[:4],
        "nb_tracks": int(data.get("nb_tracks", 0) or 0),
        "duration": int(data.get("duration", 0) or 0),
        "fans": int(data.get("fans", 0) or 0),
        "label": data.get("label", ""),
        "record_type": data.get("record_type", ""),
        "explicit_lyrics": bool(data.get("explicit_lyrics", False)),
        "genres": [g.get("name", "") for g in (data.get("genres", {}) or {}).get("data", [])],
        "tracks": [_asdict(t) for t in tracks],
    }


def get_album_tracks(album_id: str) -> list[Track]:
    data = _get(f"/album/{album_id}")
    cover_big = data.get("cover_xl") or data.get("cover_big", "")
    cover_small = data.get("cover_small", "")
    out: list[Track] = []
    for x in data.get("tracks", {}).get("data", []):
        out.append(
            Track(
                id=str(x.get("id", "")),
                title=x.get("title", ""),
                artist=(x.get("artist") or {}).get("name", ""),
                album=data.get("title", ""),
                duration=int(x.get("duration", 0) or 0),
                cover_small=cover_small,
                cover_big=cover_big,
                preview_url=x.get("preview", "") or "",
                source="deezer",
                artist_id=str((x.get("artist") or {}).get("id", "")),
            )
        )
    return out


def get_related_artists(artist_id: str, limit: int = 10) -> list[dict]:
    if not artist_id:
        return []
    ck = f"rel:{artist_id}:{limit}"
    cv = _cache_get(ck)
    if cv is not None:
        return cv
    data = _get(f"/artist/{artist_id}/related", limit=limit)
    out = [
        {
            "id": str(x.get("id", "")),
            "name": x.get("name", ""),
            "picture": x.get("picture_big") or x.get("picture_medium", ""),
        }
        for x in data.get("data", [])
    ]
    _cache_put(ck, out)
    return out


def get_top_tracks(artist_id: str, limit: int = 10) -> list[Track]:
    if not artist_id:
        return []
    ck = f"top:{artist_id}:{limit}"
    cv = _cache_get(ck)
    if cv is not None:
        return cv
    data = _get(f"/artist/{artist_id}/top", limit=limit)
    out = [_track_from_json(x) for x in data.get("data", [])]
    _cache_put(ck, out)
    return out


def get_charts(limit: int = 30) -> list[Track]:
    ck = f"charts:{limit}"
    cv = _cache_get(ck)
    if cv is not None:
        return cv
    data = _get("/chart/0/tracks", limit=limit)
    out = [_track_from_json(x) for x in data.get("data", [])]
    _cache_put(ck, out, ttl=15 * 60)
    return out


def get_artist_genre(artist_id: str) -> str:
    """Возвращает основной жанр артиста (имя строкой) либо "".

    Deezer не даёт жанр прямо в /artist/<id>; забираем первый альбом и
    смотрим его genres. Кэшируем агрессивно (24ч) — жанр почти не меняется.
    """
    if not artist_id:
        return ""
    ck = f"artist_genre:{artist_id}"
    cv = _cache_get(ck)
    if cv is not None:
        return cv
    try:
        albums = _get(f"/artist/{artist_id}/albums", limit=3)
        for al in albums.get("data", []) or []:
            aid = str(al.get("id") or "")
            if not aid:
                continue
            try:
                meta = _get(f"/album/{aid}")
            except Exception:
                continue
            genres = (meta.get("genres") or {}).get("data") or []
            for g in genres:
                name = (g.get("name") or "").strip()
                if name:
                    _cache_put(ck, name, ttl=24 * 3600)
                    return name
    except Exception:
        pass
    _cache_put(ck, "", ttl=6 * 3600)
    return ""

