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

import base64
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import yt_dlp

BASE_URL = (os.environ.get("VELORA_BASE_URL") or "").rstrip("/")
TOKEN = (os.environ.get("RESOLVE_PUSH_TOKEN") or "").strip()
LIMIT = int(os.environ.get("RESOLVE_LIMIT") or "20")
PER_TRACK_TIMEOUT = 25  # секунд на один резолв через yt-dlp
MIN_DURATION = 60       # отсекаем 30-сек preview
PUSH_BATCH = 5          # сколько треков пушить за раз (Sprinthost иногда тормозит)

if not BASE_URL or not TOKEN:
    print("ERR: VELORA_BASE_URL and RESOLVE_PUSH_TOKEN must be set", file=sys.stderr)
    sys.exit(1)

# Опциональные YouTube cookies (Netscape txt) — base64 в Secret YT_COOKIES_B64
# или прямой путь в YT_COOKIES_FILE.
_YT_COOKIES_PATH: str | None = None
_yt_b64 = (os.environ.get("YT_COOKIES_B64") or "").strip()
if _yt_b64:
    try:
        _decoded = base64.b64decode(_yt_b64)
        _tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", prefix="ytck_", delete=False
        )
        _tmp.write(_decoded)
        _tmp.close()
        _YT_COOKIES_PATH = _tmp.name
        print(f"[worker] yt cookies loaded from secret ({len(_decoded)} bytes)")
    except Exception as _e:
        print(f"[worker] yt cookies decode err: {_e}", file=sys.stderr)
elif os.environ.get("YT_COOKIES_FILE"):
    _p = os.environ["YT_COOKIES_FILE"]
    if os.path.isfile(_p):
        _YT_COOKIES_PATH = _p
        print(f"[worker] yt cookies file: {_p}")


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

# SoundCloud первым: его IP-бан Actions runner'ов не трогает,
# а YouTube стабильно требует cookies на дата-центровых IP.
SOURCES = [
    ("scsearch10", "SoundCloud"),
    ("ytsearch10", "YouTube"),
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
    with urllib.request.urlopen(req, timeout=60) as r:
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
    """Резолвит один трек: пробует SoundCloud → YouTube."""
    for src, name in SOURCES:
        query = f"{src}:{q}"
        opts = dict(YDL_OPTS)
        if name == "YouTube" and _YT_COOKIES_PATH:
            opts["cookiefile"] = _YT_COOKIES_PATH
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(query, download=False)
            url = _pick_audio_url(info, target_dur)
            if url:
                print(f"  OK via {name}: {url[:100]}...", flush=True)
                return url
            print(f"  {name}: no audio in entries", flush=True)
        except Exception as exc:
            msg = str(exc)
            # Длинные YouTube-логи режем — забивают Actions log.
            if len(msg) > 200:
                msg = msg[:200] + "..."
            print(f"  {name} ERR: {type(exc).__name__}: {msg}", flush=True)
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

    # Пушим маленькими батчами — на Sprinthost запись в _resolver_cache.json
    # синхронная и крупный POST иногда отваливается по таймауту.
    total_accepted = 0
    failed_batches = 0
    for offset in range(0, len(resolved), PUSH_BATCH):
        chunk = resolved[offset : offset + PUSH_BATCH]
        try:
            resp = http_post_json(
                f"{BASE_URL}/api/resolve/push", {"items": chunk}
            )
            acc = int(resp.get("accepted") or 0)
            total_accepted += acc
            print(
                f"[worker] push batch {offset}-{offset+len(chunk)}: "
                f"accepted={acc}/{len(chunk)}",
                flush=True,
            )
        except Exception as exc:
            failed_batches += 1
            print(f"[worker] push batch {offset} failed: {exc}", file=sys.stderr)
    print(
        f"[worker] DONE total_accepted={total_accepted} "
        f"of {len(resolved)} (failed_batches={failed_batches})"
    )
    # Не фейлим весь job, даже если часть батчей не прошла —
    # принятые URL уже попали в кэш.
    return 0


if __name__ == "__main__":
    sys.exit(main())
