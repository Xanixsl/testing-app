"""Снимки персональных предпочтений пользователя.

Собираем все источники сигнала (лайки, дизлайки, история, посещения
страниц, настройки артистов) в один JSON-payload — TasteSnapshot.
Этот снимок используется алгоритмами:

  * `/api/wave` — личная волна;
  * `/api/recommend/for-you` — секция «Возможно вам понравится» на главной;
  * любые будущие персональные подборки.

Снимок пересчитывается лениво (cooldown в часах). Идея — снимок дешёвый
по чтению (один SELECT по таблице taste_snapshots), а пересчёт — вне
горячего пути запроса.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func, text as _sql

from .db import (
    ArtistPref, Dislike, HistoryEntry, Like, PageVisit, Playlist, PlaylistItem,
    TasteSnapshot, db,
)


SNAPSHOT_TTL = timedelta(hours=2)


# ────────────────────────────────────────────────────────────── snapshot

def _serialize_counter(c: Counter, *, top_n: int = 50, meta: dict | None = None) -> list[dict]:
    """Counter → отсортированный список словарей.

    `meta` — опциональный словарь {key: extra_dict} для приклеивания
    метаданных (имя, картинка) к ключу.
    """
    out: list[dict] = []
    for key, w in c.most_common(top_n):
        item = {"key": key, "w": int(w)}
        if meta and key in meta:
            item.update(meta[key])
        out.append(item)
    return out


def compute_snapshot(user_id: int) -> dict:
    """Собирает снимок предпочтений из БД (без побочных эффектов)."""
    # 1. Артисты-лайки/дизлайки.
    prefs = (
        db.session.query(ArtistPref).filter_by(user_id=user_id).all()
    )
    artists_like: list[dict] = []
    artists_dislike: list[dict] = []
    for p in prefs:
        rec = {"id": p.artist_id, "name": p.name or "", "image": p.image or "",
               "source": p.source or "deezer"}
        if p.kind == "like":
            artists_like.append(rec)
        elif p.kind == "dislike":
            artists_dislike.append(rec)

    # 2. Лайкнутые треки.
    likes = (
        db.session.query(Like).filter_by(user_id=user_id)
        .order_by(Like.created_at.desc()).limit(300).all()
    )
    tracks_like = [
        {"id": l.track_id, "title": l.title or "", "artist": l.artist or "",
         "artist_id": l.artist_id or "", "cover": l.cover or "",
         "source": l.source or "deezer"}
        for l in likes if l.track_id
    ]

    # 3. Дизлайки треков и артистов.
    dis = db.session.query(Dislike).filter_by(user_id=user_id).all()
    tracks_dislike = [
        {"id": d.track_id, "artist_id": d.artist_id or ""}
        for d in dis if d.scope == "track" and d.track_id
    ]
    # Артисты-дизлайки могут прийти и из Dislike (scope=artist).
    for d in dis:
        if d.scope == "artist" and d.artist_id:
            artists_dislike.append({
                "id": d.artist_id, "name": d.artist or "",
                "image": d.cover or "", "source": d.source or "deezer",
            })

    # 4. Часто прослушиваемые треки/артисты — из history (count + recency).
    hist_rows = (
        db.session.query(
            HistoryEntry.track_id, HistoryEntry.artist_id, HistoryEntry.title,
            HistoryEntry.artist, HistoryEntry.cover, HistoryEntry.source,
            func.sum(HistoryEntry.play_count).label("plays"),
            func.max(HistoryEntry.played_at).label("last_at"),
        )
        .filter(HistoryEntry.user_id == user_id)
        .group_by(
            HistoryEntry.track_id, HistoryEntry.artist_id, HistoryEntry.title,
            HistoryEntry.artist, HistoryEntry.cover, HistoryEntry.source,
        )
        .order_by(_sql("plays DESC")).limit(200).all()
    )
    top_tracks: list[dict] = []
    artist_play_counter: Counter = Counter()
    artist_meta: dict[str, dict] = {}
    for r in hist_rows:
        if not r.track_id:
            continue
        plays = int(r.plays or 0)
        top_tracks.append({
            "id": r.track_id, "artist_id": r.artist_id or "",
            "title": r.title or "", "artist": r.artist or "",
            "cover": r.cover or "", "source": r.source or "deezer",
            "w": plays,
        })
        if r.artist_id:
            artist_play_counter[r.artist_id] += plays
            artist_meta.setdefault(r.artist_id, {
                "name": r.artist or "", "source": r.source or "deezer",
            })
    top_artists = _serialize_counter(artist_play_counter, top_n=50, meta=artist_meta)

    # 5. Посещения страниц — артистов / альбомов / плейлистов.
    visit_rows = (
        db.session.query(PageVisit).filter_by(user_id=user_id).all()
    )
    freq_artists: list[dict] = []
    freq_albums: list[dict] = []
    freq_playlists: list[dict] = []
    for v in visit_rows:
        rec = {
            "id": v.target_id, "name": v.name or "", "artist": v.artist or "",
            "cover": v.cover or "", "source": v.source or "deezer",
            "w": int(v.count or 1),
        }
        if v.kind == "artist":
            freq_artists.append(rec)
        elif v.kind == "album":
            freq_albums.append(rec)
        elif v.kind == "playlist":
            freq_playlists.append(rec)
    for lst in (freq_artists, freq_albums, freq_playlists):
        lst.sort(key=lambda x: -x["w"])
        del lst[80:]

    # 6. Частые плейлисты пользователя (свои) — добавим в freq_playlists.
    own_playlists = (
        db.session.query(Playlist).filter_by(user_id=user_id).all()
    )
    for p in own_playlists:
        freq_playlists.append({
            "id": str(p.id), "name": p.name, "cover": p.cover or "",
            "source": "local", "w": 1,
        })

    return {
        "artists_like": artists_like,
        "artists_dislike": artists_dislike,
        "tracks_like": tracks_like,
        "tracks_dislike": tracks_dislike,
        "top_played_tracks": top_tracks,
        "top_played_artists": top_artists,
        "frequent_artists": freq_artists,
        "frequent_albums": freq_albums,
        "frequent_playlists": freq_playlists,
        "generated_at": datetime.utcnow().isoformat(),
    }


def get_or_refresh_snapshot(user_id: int, *, force: bool = False) -> dict:
    """Возвращает снимок: либо из таблицы (если свежий), либо пересчитывает.

    При force=True пересчёт безусловный.
    """
    row = (
        db.session.query(TasteSnapshot).filter_by(user_id=user_id).first()
    )
    if row and not force:
        try:
            updated = row.updated_at or row.created_at or datetime.utcnow()
            if datetime.utcnow() - updated < SNAPSHOT_TTL:
                return json.loads(row.payload or "{}")
        except Exception:
            pass

    payload = compute_snapshot(user_id)
    try:
        if row is None:
            row = TasteSnapshot(user_id=user_id, payload=json.dumps(payload, ensure_ascii=False))
            db.session.add(row)
        else:
            row.payload = json.dumps(payload, ensure_ascii=False)
            row.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
    return payload


# ─────────────────────────────────────────── визиты страниц (инкремент)

def record_visit(*, user_id: int, kind: str, target_id: str, source: str = "deezer",
                 name: str = "", artist: str = "", cover: str = "") -> None:
    """UPSERT счётчика посещений (без поднятия исключений)."""
    if kind not in {"artist", "album", "playlist", "track"}:
        return
    target_id = str(target_id or "").strip()
    if not target_id:
        return
    source = source or "deezer"
    try:
        row = (
            db.session.query(PageVisit)
            .filter_by(user_id=user_id, kind=kind, target_id=target_id, source=source)
            .first()
        )
        if row is None:
            row = PageVisit(
                user_id=user_id, kind=kind, target_id=target_id, source=source,
                name=name or "", artist=artist or "", cover=cover or "",
                count=1, last_visited_at=datetime.utcnow(),
            )
            db.session.add(row)
        else:
            row.count = int(row.count or 0) + 1
            row.last_visited_at = datetime.utcnow()
            if name and not row.name:
                row.name = name
            if artist and not row.artist:
                row.artist = artist
            if cover and not row.cover:
                row.cover = cover
        db.session.commit()
    except Exception:
        db.session.rollback()


# ───────────────────────────────────────── веса для алгоритма Wave/For-You

def weighted_artist_seeds(snapshot: dict, *, top_n: int = 12) -> Counter:
    """Возвращает Counter {artist_id: weight} на основе снимка.

    Веса (эмпирические):
      * лайк-трек     +5
      * лайк-артист   +8
      * прослушивание +1 за плеймент (sqrt сглаживание)
      * визит артиста +2 за посещение
      * визит альбома +1 за посещение
    Дизлайки (артисты) — артист исключается полностью.
    """
    import math

    deny: set[str] = {a.get("id") for a in (snapshot.get("artists_dislike") or []) if a.get("id")}
    weights: Counter = Counter()

    for a in snapshot.get("artists_like", []) or []:
        aid = a.get("id")
        if aid:
            weights[aid] += 8

    for t in snapshot.get("tracks_like", []) or []:
        aid = t.get("artist_id")
        if aid:
            weights[aid] += 5

    for t in snapshot.get("top_played_tracks", []) or []:
        aid = t.get("artist_id")
        if aid:
            weights[aid] += int(math.sqrt(max(1, int(t.get("w") or 0))))

    for a in snapshot.get("top_played_artists", []) or []:
        aid = a.get("key") or a.get("id")
        if aid:
            weights[aid] += int(math.sqrt(max(1, int(a.get("w") or 0))))

    for a in snapshot.get("frequent_artists", []) or []:
        aid = a.get("id")
        if aid:
            weights[aid] += 2 * int(a.get("w") or 1)

    for al in snapshot.get("frequent_albums", []) or []:
        aid = al.get("artist_id") or ""
        if aid:
            weights[aid] += int(al.get("w") or 1)

    for d in deny:
        weights.pop(d, None)

    if top_n:
        return Counter(dict(weights.most_common(top_n)))
    return weights


def denylist(snapshot: dict) -> tuple[set[str], set[str]]:
    """Возвращает (denied_track_ids, denied_artist_ids) из снимка."""
    deny_tracks = {
        str(t.get("id"))
        for t in (snapshot.get("tracks_dislike") or [])
        if t.get("id")
    }
    deny_artists = {
        str(a.get("id"))
        for a in (snapshot.get("artists_dislike") or [])
        if a.get("id")
    }
    return deny_tracks, deny_artists



# --------------------------------- жанры пользователя и палитра

# Карта Deezer-жанр (нижний регистр) -> нормализованный ключ.
GENRE_NORMALIZE = {
    "rap/hip hop": "hiphop", "hip hop": "hiphop", "hip-hop": "hiphop",
    "r&b": "rnb", "rnb": "rnb", "soul": "rnb",
    "rock": "rock", "alternative": "rock", "indie": "rock",
    "punk": "rock", "metal": "metal", "hard rock": "metal",
    "pop": "pop", "pop music": "pop",
    "electro": "electronic", "electronic": "electronic", "dance": "electronic",
    "house": "electronic", "techno": "electronic", "edm": "electronic",
    "jazz": "jazz", "blues": "jazz",
    "classical": "classical",
    "country": "country", "folk": "folk",
    "reggae": "reggae", "latin": "latin", "world": "world",
    "films/games": "soundtrack", "soundtrack": "soundtrack",
    "kids": "kids",
    "russian variete": "russian", "russian": "russian",
}

# Палитра цветов на нормализованный жанр: (accent, accent2, accent3).
GENRE_PALETTE = {
    "hiphop":     ("#b46bff", "#ff5b9c", "#7c4aff"),
    "rnb":        ("#ff7a7a", "#ffb14a", "#ff5b9c"),
    "rock":       ("#ff5454", "#7a3a3a", "#ffd84a"),
    "metal":      ("#c2c2c8", "#5b5b65", "#ff5454"),
    "pop":        ("#ffd84a", "#ff5b9c", "#b46bff"),
    "electronic": ("#4af0ff", "#4a8eff", "#7c4aff"),
    "jazz":       ("#ffd84a", "#a37a3a", "#ff8a4a"),
    "classical":  ("#e8d8a0", "#a3a3c8", "#7c7a99"),
    "country":    ("#ffb14a", "#a3ff4a", "#ffd84a"),
    "folk":       ("#a3ff4a", "#ffb14a", "#7aff5b"),
    "reggae":     ("#a3ff4a", "#ffd84a", "#ff5454"),
    "latin":      ("#ff8a4a", "#ffd84a", "#ff5b9c"),
    "world":      ("#4af0ff", "#a3ff4a", "#ffb14a"),
    "soundtrack": ("#a3a3ff", "#ff5b9c", "#4af0ff"),
    "kids":       ("#a3ff4a", "#4af0ff", "#ffd84a"),
    "russian":    ("#ff5b9c", "#ffd84a", "#b46bff"),
}

DEFAULT_PALETTE = ("#ffd84a", "#ff5b9c", "#b46bff")


def normalize_genre(name: str) -> str:
    """Deezer name -> ключ из GENRE_PALETTE; "" если не распознан."""
    if not name:
        return ""
    key = name.strip().lower()
    if key in GENRE_NORMALIZE:
        return GENRE_NORMALIZE[key]
    for k, v in GENRE_NORMALIZE.items():
        if k in key or key in k:
            return v
    return ""


def compute_user_palette(user_id: int) -> dict:
    """Главный жанр пользователя + цветовая палитра.

    Берём топ-12 артистов из снимка (likes -> top_played -> frequent),
    вытягиваем для каждого жанр (Deezer-клиент кэширует), считаем Counter.
    """
    from collections import Counter
    from .api import deezer

    snap = get_or_refresh_snapshot(user_id)
    artist_ids: list[str] = []
    for a in snap.get("artists_like", []) or []:
        aid = a.get("id")
        if aid:
            artist_ids.append(str(aid))
    for a in snap.get("top_played_artists", []) or []:
        aid = a.get("key") or a.get("id")
        if aid:
            artist_ids.append(str(aid))
    for a in snap.get("frequent_artists", []) or []:
        aid = a.get("id")
        if aid:
            artist_ids.append(str(aid))

    seen: set = set()
    ordered: list[str] = []
    for aid in artist_ids:
        if aid in seen:
            continue
        seen.add(aid)
        ordered.append(aid)
        if len(ordered) >= 12:
            break

    if not ordered:
        return {"genre": "", "genre_raw": "", "palette": {
            "accent": DEFAULT_PALETTE[0],
            "accent2": DEFAULT_PALETTE[1],
            "accent3": DEFAULT_PALETTE[2],
        }, "breakdown": {}}

    counter: Counter = Counter()
    raw_counter: Counter = Counter()
    for aid in ordered:
        try:
            raw = deezer.get_artist_genre(aid)
        except Exception:
            raw = ""
        if not raw:
            continue
        raw_counter[raw] += 1
        norm = normalize_genre(raw)
        if norm:
            counter[norm] += 1

    if counter:
        top_norm = counter.most_common(1)[0][0]
        palette = GENRE_PALETTE.get(top_norm, DEFAULT_PALETTE)
    else:
        top_norm = ""
        palette = DEFAULT_PALETTE
    top_raw = raw_counter.most_common(1)[0][0] if raw_counter else ""

    return {
        "genre": top_norm,
        "genre_raw": top_raw,
        "palette": {
            "accent": palette[0],
            "accent2": palette[1],
            "accent3": palette[2],
        },
        "breakdown": dict(counter),
    }
