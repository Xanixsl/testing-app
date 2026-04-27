"""Внешний резолвер для Velora Sound (запускается из GitHub Actions).

Алгоритм:
    1. GET {VELORA_BASE_URL}/api/resolve/queue?token=...&limit=N
       → список нерезолвленных треков [{q, duration, quality}, ...]
    2. Для каждого: yt-dlp → bestaudio URL (на чистом IP runner'а).
    3. POST {VELORA_BASE_URL}/api/resolve/push?token=... с готовыми URL.
       Сервер кладёт их в _resolver_cache.json — и /api/stream начинает
       отвечать мгновенно из кэша.

Авторизация: общий секрет в env RESOLVE_PUSH_TOKEN
(должен совпадать с настроенным на проде).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

import yt_dlp

BASE_URL = (os.environ.get("VELORA_BASE_URL") or "").rstrip("/")
TOKEN = (os.environ.get("RESOLVE_PUSH_TOKEN") or "").strip()
LIMIT = int(os.environ.get("RESOLVE_LIMIT") or "20")
PER_TRACK_TIMEOUT = 25  # секунд на один резолв через yt-dlp
MIN_DURATION = 60       # отсекаем 30-сек preview

if not BASE_URL or not TOKEN:
    print("ERR: VELORA_BASE_URL and RESOLVE_PUSH_TOKEN must be set", file=sys.stderr)
    sys.exit(1)


YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "format": "bestaudio/best",
    "extract_flat": False,
    "geo_bypass": True,
    "geo_bypass_country": "US",
    "socket_timeout": 15,
    "retries": 1,
    "fragment_retries": 0,
    "extractor_retries": 1,
    "nocheckcertificate": True,
    "extractor_args": {
        "youtube": {"player_client": ["ios", "android", "mweb", "web"]},
        "youtubetab": {"skip": ["webpage"]},
    },
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
}

SOURCES = [
    ("ytsearch10", "YouTube"),
    ("scsearch10", "SoundCloud"),
]


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"X-Resolve-Token": TOKEN, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def http_post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "X-Resolve-Token": TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _pick_audio_url(info: dict, target_dur: int) -> str | None:
    """Выбирает лучший audio-URL из yt-dlp info."""
    if not info:
        return None
    entries = info.get("entries") or [info]
    best_entry = None
    best_score = -1.0
    for ent in entries:
        if not ent:
            continue
        dur = int(ent.get("duration") or 0)
        if dur and dur < MIN_DURATION:
            continue
        score = 0.0
        if target_dur and dur:
            diff = abs(dur - target_dur)
            if diff <= 5:
                score += 100
            elif diff <= 15:
                score += 60
            elif diff <= 30:
                score += 20
            else:
                score -= diff
        title = (ent.get("title") or "").lower()
        for bad in ("clean", "radio edit", "censored", "karaoke", "instrumental"):
            if bad in title:
                score -= 40
        if score > best_score:
            best_score = score
            best_entry = ent

    if not best_entry:
        return None

    fmts = best_entry.get("formats") or []
    audio_fmts = [
        f for f in fmts
        if f.get("acodec") and f["acodec"] != "none"
        and (not f.get("vcodec") or f["vcodec"] == "none")
        and f.get("url") and not str(f.get("protocol", "")).startswith(("m3u8", "dash"))
    ]
    if not audio_fmts:
        # запасной: любой URL верхнего уровня
        return best_entry.get("url")
    audio_fmts.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 0), reverse=True)
    return audio_fmts[0].get("url")


def resolve_one(q: str, target_dur: int) -> str | None:
    """Резолвит один трек: пробует YouTube → SoundCloud."""
    for src, name in SOURCES:
        query = f"{src}:{q}"
        opts = dict(YDL_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            url = _pick_audio_url(info, target_dur)
            if url:
                print(f"  OK via {name}: {url[:100]}...", flush=True)
                return url
            print(f"  {name}: no audio in entries", flush=True)
        except Exception as exc:
            print(f"  {name} ERR: {type(exc).__name__}: {exc}", flush=True)
    return None


def main() -> int:
    print(f"[worker] base={BASE_URL} limit={LIMIT}")
    try:
        queue = http_get_json(f"{BASE_URL}/api/resolve/queue?limit={LIMIT}")
    except Exception as exc:
        print(f"[worker] queue fetch failed: {exc}", file=sys.stderr)
        return 1

    items = queue.get("items") or []
    print(f"[worker] got {len(items)} items in queue")
    if not items:
        return 0

    resolved: list[dict] = []
    started = time.time()
    for i, it in enumerate(items, 1):
        # Жёсткий бюджет — не вылетим за timeout-minutes job'а.
        if time.time() - started > 6 * 60:
            print("[worker] hard time budget reached; stopping", flush=True)
            break
        q = (it.get("q") or "").strip()
        if not q:
            continue
        try:
            dur = int(it.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        quality = (it.get("quality") or "hi").strip().lower() or "hi"
        print(f"[{i}/{len(items)}] {q!r} dur={dur} q={quality}", flush=True)
        url = resolve_one(q, dur)
        if url:
            resolved.append({"q": q, "duration": dur, "quality": quality, "url": url})

    if not resolved:
        print("[worker] nothing resolved")
        return 0

    try:
        resp = http_post_json(f"{BASE_URL}/api/resolve/push", {"items": resolved})
        print(f"[worker] push accepted={resp.get('accepted')} of {len(resolved)}")
    except Exception as exc:
        print(f"[worker] push failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
