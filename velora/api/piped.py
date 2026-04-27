"""Piped резолвер — обход блокировок YouTube/SoundCloud на shared-хостинге.

На Sprinthost yt-dlp работает нестабильно (SSL EOF, age-gate, нужен PoToken).
Вместо этого ходим во внешние публичные Piped-инстансы, которые сами
проксируют запросы в YouTube через ротирующие IP. Sprinthost туда
достучаться может — это обычный HTTPS GET к публичным API.

Piped API endpoints:
  GET {host}/search?q=<query>&filter=music_songs   → results[].url = "/watch?v=ID"
  GET {host}/streams/<videoId>                      → audioStreams[]{url, bitrate, codec}

Архитектура устойчивости:
  1. Параллельно опрашиваем 4 хоста (ThreadPoolExecutor). Первый, кто
     успел вернуть валидный search-результат, выигрывает. Остальные отменяются.
  2. На него же делаем второй запрос /streams/<id>.
  3. Если общий бюджет (HARD_BUDGET) исчерпан — отдаём None, не вешая UI.
  4. Хост, упавший по timeout/connect, на 5 минут уходит в cooldown,
     чтобы следующий пользователь не натыкался на тот же мёртвый узел.
"""
from __future__ import annotations

import concurrent.futures as _futures
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Список Piped backend'ов (НЕ frontend!). Порядок = приоритет (быстрые сверху).
_PIPED_HOSTS: list[str] = [
    "https://pipedapi.kavin.rocks",          # официальный, US/IN/NL/CA/GB/FR
    "https://pipedapi.adminforge.de",        # DE
    "https://api.piped.private.coffee",      # AT
    "https://pipedapi.leptons.xyz",          # AT
    "https://pipedapi-libre.kavin.rocks",    # NL
    "https://pipedapi.nosebs.ru",            # FI
    "https://piped-api.codespace.cz",        # CZ
    "https://pipedapi.reallyaweso.me",       # DE
    "https://pipedapi.ducks.party",          # NL
    "https://pipedapi.drgns.space",          # US
    "https://api.piped.yt",                  # DE
]

_PARALLEL = 4
_TIMEOUT_SEARCH = 3.5
_TIMEOUT_STREAMS = 4.0
_HARD_BUDGET = 6.0
_HOST_BAN: dict[str, float] = {}
_HOST_BAN_TTL = 300.0
_MIN_DURATION = 60

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _flog(msg: str) -> None:
    try:
        import os
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent.parent
        log_dir = root / "instance"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "resolver.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} [piped] {msg}\n")
    except Exception:
        pass


def _http_get_json(url: str, timeout: float) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


_VID_RX = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


def _extract_video_id(url: str) -> str | None:
    if not url:
        return None
    m = _VID_RX.search(url)
    if m:
        return m.group(1)
    if "youtu.be/" in url:
        tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if len(tail) == 11:
            return tail
    return None


_BAD = ("clean", "radio edit", "radio version", "radio mix", "censored",
        "edited", "no swearing", "без мата", "цензур", "детская версия", "for radio")
_GOOD = ("explicit", "uncensored", "original", "оригинал", "без цензур")
_COVER = ("cover", "кавер", "mashup", "мэшап", "remix", "ремикс", "rework",
          "tribute", "karaoke", "караоке", "minus", "минус", "instrumental",
          "инструментал", "live", "лайв", "acoustic", "акустика", "spedup",
          "sped up", "slowed", "nightcore", "найткор", "ai cover", "ai-cover")


def _score_entry(item: dict, target_duration: int) -> float:
    title = (item.get("title") or "").lower()
    score = 0.0
    dur = int(item.get("duration") or 0)
    if dur and target_duration:
        diff = abs(dur - target_duration)
        if diff <= 3:
            score += 100
        elif diff <= 8:
            score += 60
        elif diff <= 15:
            score += 20
        else:
            score -= (diff - 15) * 10
    elif dur:
        score += min(10, dur / 60)
        if dur > 900:
            score -= 30
    if any(m in title for m in _BAD):
        score -= 60
    if any(m in title for m in _GOOD):
        score += 25
    if any(m in title for m in _COVER):
        score -= 80
    return score


def _pick_best_audio(streams: list[dict]) -> str | None:
    if not streams:
        return None
    best = None
    best_br = -1
    for s in streams:
        url = s.get("url")
        if not url:
            continue
        fmt = (s.get("format") or "").upper()
        if "HLS" in fmt or "DASH" in fmt:
            continue
        br = int(s.get("bitrate") or 0)
        if br > best_br:
            best = url
            best_br = br
    if best is None and streams:
        best = streams[0].get("url")
    return best


def _filter_candidates(items: list[dict], target_duration: int) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for it in items or []:
        if (it.get("type") or "").lower() not in ("stream", "video", ""):
            continue
        vid = _extract_video_id(it.get("url") or "")
        if not vid:
            continue
        dur = int(it.get("duration") or 0)
        if dur and dur < _MIN_DURATION:
            continue
        if target_duration and dur and abs(dur - target_duration) > 25:
            continue
        out.append((vid, it))
    out.sort(key=lambda x: -_score_entry(x[1], target_duration))
    return out


def _try_host(host: str, q_enc: str, target_duration: int, deadline: float) -> tuple[str, str | None]:
    """search → /streams/<id> → URL. Все сетевые ошибки = (host, None)."""
    remain = max(0.5, deadline - time.time())
    timeout = min(_TIMEOUT_SEARCH, remain)
    data = _http_get_json(f"{host}/search?q={q_enc}&filter=music_songs", timeout)
    if not data or not isinstance(data, dict):
        return host, None
    items = data.get("items") or []
    if not items:
        remain = max(0.3, deadline - time.time())
        if remain < 1.0:
            return host, None
        data = _http_get_json(f"{host}/search?q={q_enc}&filter=videos", min(_TIMEOUT_SEARCH, remain))
        if not data or not isinstance(data, dict):
            return host, None
        items = data.get("items") or []
    candidates = _filter_candidates(items, target_duration)
    if not candidates:
        return host, None
    vid, _meta = candidates[0]
    remain = max(0.5, deadline - time.time())
    timeout = min(_TIMEOUT_STREAMS, remain)
    sdata = _http_get_json(f"{host}/streams/{vid}", timeout)
    if not sdata or not isinstance(sdata, dict):
        return host, None
    return host, _pick_best_audio(sdata.get("audioStreams") or [])


def _hosts_alive(now: float) -> list[str]:
    return [h for h in _PIPED_HOSTS if _HOST_BAN.get(h, 0) <= now]


def search_stream(query: str, target_duration: int = 0) -> str | None:
    """Параллельный опрос 4 хостов, первый успех выигрывает.
    Жёсткий бюджет _HARD_BUDGET секунд. None если никто не успел.
    """
    if not query:
        return None
    started = time.time()
    deadline = started + _HARD_BUDGET
    hosts = _hosts_alive(started)
    if not hosts:
        _HOST_BAN.clear()
        hosts = list(_PIPED_HOSTS)
    pool_hosts = hosts[:_PARALLEL]
    q_enc = urllib.parse.quote(query, safe="")
    winner: str | None = None
    failed: list[str] = []
    with _futures.ThreadPoolExecutor(max_workers=_PARALLEL) as ex:
        futs = {ex.submit(_try_host, h, q_enc, target_duration, deadline): h for h in pool_hosts}
        try:
            for fut in _futures.as_completed(futs, timeout=_HARD_BUDGET):
                host = futs[fut]
                try:
                    _h, url = fut.result()
                except Exception:
                    failed.append(host)
                    continue
                if url:
                    winner = url
                    _flog(f"OK host={host} q={query!r} t={round(time.time()-started, 2)}s")
                    break
                failed.append(host)
        except _futures.TimeoutError:
            _flog(f"BUDGET expired q={query!r} t={round(time.time()-started, 2)}s")
        finally:
            for fut in futs:
                if not fut.done():
                    fut.cancel()
    now = time.time()
    for h in failed:
        _HOST_BAN[h] = now + _HOST_BAN_TTL
    if winner:
        return winner
    _flog(f"MISS q={query!r} t={round(time.time()-started, 2)}s tried={len(pool_hosts)}")
    return None
