"""iTunes / Apple Music Search API (открытый, без ключей).

https://itunes.apple.com/search
"""
from __future__ import annotations

from velora.api.http import SESSION, DEFAULT_TIMEOUT
from velora.models import Track

BASE = "https://itunes.apple.com/search"


def _hi_res_cover(url: str) -> str:
    if not url:
        return ""
    return url.replace("100x100bb", "600x600bb")


def search_tracks(query: str, limit: int = 30) -> list[Track]:
    if not query.strip():
        return []
    try:
        r = SESSION.get(
            BASE,
            params={"term": query, "media": "music", "entity": "song", "limit": limit},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out: list[Track] = []
    for x in data.get("results", []):
        out.append(
            Track(
                id=str(x.get("trackId", "")),
                title=x.get("trackName", ""),
                artist=x.get("artistName", ""),
                album=x.get("collectionName", ""),
                duration=int((x.get("trackTimeMillis") or 0) // 1000),
                cover_small=x.get("artworkUrl100", ""),
                cover_big=_hi_res_cover(x.get("artworkUrl100", "")),
                preview_url=x.get("previewUrl", ""),
                source="apple",
                artist_id=str(x.get("artistId", "")),
                explicit=(x.get("trackExplicitness") == "explicit"),
            )
        )
    return out
