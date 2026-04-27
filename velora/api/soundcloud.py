"""Прямой клиент к публичному API SoundCloud (api-v2.soundcloud.com).

В норме отрабатывает за ~1 сек: получаем client_id со страницы soundcloud.com,
ищем треки через /search/tracks, резолвим mp3 URL через media.transcodings[*].url.

Особенности окружения:
- На проде (Sprinthost) нужен IPv4 — патчим Curl.perform → setopt(IPRESOLVE,1).
- TLS-fingerprint обходится через curl_cffi.Session(impersonate="chrome").
- В mod_wsgi sub-interpreter curl_cffi не импортируется (PEP 489 single-phase init).
  В этом случае используем subprocess: один запуск Python делает ВСЮ pipeline
  (home → js → search → resolve), чтобы не платить за повторный startup.
- client_id кешируется на диск (instance/.sc_client_id.json) на 12ч, чтобы 99%
  запросов делали ровно 2 HTTP вызова (search + transcoding resolve) ~0.5s.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

# In-memory cache (per-process)
_CLIENT_ID: Optional[str] = None
_CLIENT_ID_AT: float = 0.0
_CID_TTL = 12 * 3600

# Disk cache shared across workers/subprocess invocations
_CID_FILE = os.environ.get(
    "VELORA_SC_CID_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "instance", ".sc_client_id.json"),
)

# Кэш прямых mp3-URL (ключ — нормализованный запрос + округлённая дительность).
_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_CACHE_TTL = 5 * 60

# Если SoundCloud временно блокирует наш IP — не дёргаем его 5 минут,
# чтобы не тормозить /api/stream
_BLOCKED_UNTIL: float = 0.0
_BLOCK_TTL = 5 * 60

_SC_HOME = "https://soundcloud.com/"
_SC_SEARCH = "https://api-v2.soundcloud.com/search/tracks"

_BAD_MARKERS = (
    "clean", "censored", "no swearing", "radio edit", "edited",
    "без мата", "цензур", "без цензур",
)


# ----------------------------------------------------------------- disk cid
def _load_disk_cid() -> Optional[str]:
    try:
        with open(_CID_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        cid = obj.get("client_id")
        ts = float(obj.get("ts") or 0)
        if cid and (time.time() - ts) < _CID_TTL:
            return cid
    except Exception:
        pass
    return None


def _save_disk_cid(cid: str) -> None:
    try:
        os.makedirs(os.path.dirname(_CID_FILE), exist_ok=True)
        tmp = _CID_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"client_id": cid, "ts": time.time()}, f)
        os.replace(tmp, _CID_FILE)
    except Exception:
        pass


# --------------------------------------------------------------------- in-proc
try:  # pragma: no cover
    from curl_cffi import requests as _cr  # type: ignore
    from curl_cffi import Curl as _Curl, CurlOpt as _CurlOpt  # type: ignore

    _orig_perform = _Curl.perform

    def _v4_perform(self, *a, **kw):  # type: ignore[no-untyped-def]
        try:
            self.setopt(_CurlOpt.IPRESOLVE, 1)
        except Exception:
            pass
        return _orig_perform(self, *a, **kw)

    _Curl.perform = _v4_perform  # type: ignore[assignment]
    _SESSION = _cr.Session(impersonate="chrome")
    HAVE_CURL_CFFI = True
except Exception:  # noqa: BLE001
    _SESSION = None
    HAVE_CURL_CFFI = False


def _http_in_proc(url: str, params: dict | None = None, timeout: int = 15):
    assert _SESSION is not None
    r = _SESSION.get(url, params=params or None, timeout=timeout)
    return r.status_code, r.text


def _refresh_client_id_using(http_get) -> Optional[str]:
    try:
        st, html = http_get(_SC_HOME, None, 5)
    except Exception:
        return None
    if st != 200 or not html:
        return None
    urls = re.findall(
        r'<script[^>]+src="(https://[^"]+/assets/[a-zA-Z0-9_-]+\.js)"', html
    )
    for u in reversed(urls):
        try:
            st2, body = http_get(u, None, 5)
        except Exception:
            continue
        if st2 != 200:
            continue
        m = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{20,})"', body)
        if m:
            return m.group(1)
    return None


def _score(track: dict, target_ms: int) -> int:
    score = 0
    dur_ms = int(track.get("duration") or 0)
    if target_ms and dur_ms:
        diff = abs(dur_ms - target_ms) / 1000.0
        if diff < 5:
            score += 60
        elif diff < 15:
            score += 25
        elif diff > 60:
            score -= 120
    title = (track.get("title") or "").lower()
    if any(b in title for b in _BAD_MARKERS):
        score -= 60
    if "explicit" in title or "uncensored" in title or "original" in title:
        score += 20
    plays = int(track.get("playback_count") or 0)
    if plays > 1_000_000:
        score += 8
    elif plays > 100_000:
        score += 4
    user = (track.get("user") or {})
    if user.get("verified"):
        score += 12
    return score


def _pipeline(query: str, target_duration: int, http_get, cached_cid: Optional[str]):
    target_ms = int(target_duration or 0) * 1000
    cid = cached_cid or _refresh_client_id_using(http_get)
    if not cid:
        return None, None

    def _search(c):
        return http_get(_SC_SEARCH, {"q": query, "client_id": c, "limit": 12}, 5)

    st, body = _search(cid)
    if st in (401, 403):
        cid = _refresh_client_id_using(http_get)
        if not cid:
            return None, None
        st, body = _search(cid)
    if st != 200 or not body:
        return None, cid
    try:
        data = json.loads(body)
    except Exception:
        return None, cid
    items = data.get("collection") or []
    items.sort(key=lambda t: _score(t, target_ms), reverse=True)
    for t in items:
        dur_ms = int(t.get("duration") or 0)
        if target_ms and dur_ms and abs(dur_ms - target_ms) > 60_000:
            continue
        transcodings = ((t.get("media") or {}).get("transcodings") or [])
        progressive = [
            tc for tc in transcodings
            if (tc.get("format") or {}).get("protocol") == "progressive"
        ]
        if not progressive:
            continue
        tr_url = progressive[0].get("url")
        if not tr_url:
            continue
        try:
            st2, body2 = http_get(tr_url, {"client_id": cid}, 5)
            if st2 != 200:
                continue
            obj = json.loads(body2)
        except Exception:
            continue
        mp3 = obj.get("url")
        if mp3:
            return mp3, cid
    return None, cid


# ------------------------------------------------------------- subprocess fb
_HELPER_SRC = r"""
import sys, json, re, time
from curl_cffi import requests as cr, Curl, CurlOpt
_orig = Curl.perform
def _v4(self, *a, **kw):
    try: self.setopt(CurlOpt.IPRESOLVE, 1)
    except Exception: pass
    return _orig(self, *a, **kw)
Curl.perform = _v4
S = cr.Session(impersonate='chrome')

def http_get(url, params=None, timeout=15):
    r = S.get(url, params=params or None, timeout=timeout)
    return r.status_code, r.text

req = json.loads(sys.stdin.read() or '{}')
mode = req.get('mode')
try:
    if mode == 'cid':
        st, html = http_get('https://soundcloud.com/', None, 12)
        cid = None
        if st == 200 and html:
            urls = re.findall(r'<script[^>]+src="(https://[^"]+/assets/[a-zA-Z0-9_-]+\.js)"', html)
            for u in reversed(urls):
                st2, body = http_get(u, None, 12)
                if st2 != 200: continue
                m = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{20,})"', body)
                if m:
                    cid = m.group(1); break
        sys.stdout.write(json.dumps({'cid': cid})); sys.exit(0)

    if mode == 'pipeline':
        query = req['query']; target_duration = int(req.get('target_duration') or 0)
        cached_cid = req.get('cached_cid')
        target_ms = target_duration * 1000

        def refresh_cid():
            st, html = http_get('https://soundcloud.com/', None, 12)
            if st != 200 or not html: return None
            urls = re.findall(r'<script[^>]+src="(https://[^"]+/assets/[a-zA-Z0-9_-]+\.js)"', html)
            for u in reversed(urls):
                st2, body = http_get(u, None, 12)
                if st2 != 200: continue
                m = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{20,})"', body)
                if m: return m.group(1)
            return None

        cid = cached_cid or refresh_cid()
        if not cid:
            sys.stdout.write(json.dumps({'mp3': None, 'cid': None})); sys.exit(0)

        BAD = ('clean','censored','no swearing','radio edit','edited','без мата','цензур','без цензур')
        def score(t):
            s = 0; dur = int(t.get('duration') or 0)
            if target_ms and dur:
                d = abs(dur - target_ms)/1000.0
                if d < 5: s += 60
                elif d < 15: s += 25
                elif d > 60: s -= 120
            title = (t.get('title') or '').lower()
            if any(b in title for b in BAD): s -= 60
            if 'explicit' in title or 'uncensored' in title or 'original' in title: s += 20
            p = int(t.get('playback_count') or 0)
            if p > 1000000: s += 8
            elif p > 100000: s += 4
            if (t.get('user') or {}).get('verified'): s += 12
            return s

        def search(c):
            return http_get('https://api-v2.soundcloud.com/search/tracks',
                            {'q': query, 'client_id': c, 'limit': 12}, 10)
        st, body = search(cid)
        if st in (401, 403):
            cid = refresh_cid()
            if not cid:
                sys.stdout.write(json.dumps({'mp3': None, 'cid': None})); sys.exit(0)
            st, body = search(cid)
        if st != 200 or not body:
            sys.stdout.write(json.dumps({'mp3': None, 'cid': cid})); sys.exit(0)
        data = json.loads(body)
        items = data.get('collection') or []
        items.sort(key=score, reverse=True)
        for t in items:
            dur = int(t.get('duration') or 0)
            if target_ms and dur and abs(dur - target_ms) > 60000: continue
            tcs = ((t.get('media') or {}).get('transcodings') or [])
            prog = [tc for tc in tcs if (tc.get('format') or {}).get('protocol') == 'progressive']
            if not prog: continue
            tr_url = prog[0].get('url')
            if not tr_url: continue
            st2, body2 = http_get(tr_url, {'client_id': cid}, 10)
            if st2 != 200: continue
            obj = json.loads(body2)
            mp3 = obj.get('url')
            if mp3:
                sys.stdout.write(json.dumps({'mp3': mp3, 'cid': cid})); sys.exit(0)
        sys.stdout.write(json.dumps({'mp3': None, 'cid': cid}))
    else:
        sys.stdout.write(json.dumps({'error': 'unknown mode'}))
except Exception as e:
    sys.stdout.write(json.dumps({'error': str(e)}))
"""


def _venv_python() -> Optional[str]:
    base = sys.prefix
    for cand in (
        os.path.join(base, "bin", "python3"),
        os.path.join(base, "bin", "python"),
        os.path.join(base, "Scripts", "python.exe"),
    ):
        if os.path.exists(cand):
            return cand
    return None


def _pipeline_subprocess(query: str, target_duration: int, cached_cid: Optional[str]):
    py = _venv_python()
    if not py:
        return None, cached_cid
    payload = json.dumps({
        "mode": "pipeline",
        "query": query,
        "target_duration": int(target_duration or 0),
        "cached_cid": cached_cid,
    })
    try:
        proc = subprocess.run(
            [py, "-c", _HELPER_SRC],
            input=payload, capture_output=True, text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None, cached_cid
    try:
        obj = json.loads(proc.stdout or "{}")
    except Exception:
        return None, cached_cid
    if "error" in obj:
        return None, cached_cid
    return obj.get("mp3"), obj.get("cid") or cached_cid


def _cid_subprocess() -> Optional[str]:
    py = _venv_python()
    if not py:
        return None
    try:
        proc = subprocess.run(
            [py, "-c", _HELPER_SRC],
            input=json.dumps({"mode": "cid"}), capture_output=True, text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        obj = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    return obj.get("cid")


# --------------------------------------------------------------- public API
def get_client_id() -> Optional[str]:
    global _CLIENT_ID, _CLIENT_ID_AT
    if _CLIENT_ID and (time.time() - _CLIENT_ID_AT) < _CID_TTL:
        return _CLIENT_ID
    cid = _load_disk_cid()
    if cid:
        _CLIENT_ID = cid
        _CLIENT_ID_AT = time.time()
        return cid
    if HAVE_CURL_CFFI:
        cid = _refresh_client_id_using(_http_in_proc)
    else:
        cid = _cid_subprocess()
    if cid:
        _CLIENT_ID = cid
        _CLIENT_ID_AT = time.time()
        _save_disk_cid(cid)
    return cid


def search_stream(query: str, target_duration: int = 0) -> Optional[str]:
    global _CLIENT_ID, _CLIENT_ID_AT, _BLOCKED_UNTIL
    if not query or not query.strip():
        return None
    now = time.time()
    if _BLOCKED_UNTIL and now < _BLOCKED_UNTIL:
        return None
    key = f"{query.strip().lower()}|{int(target_duration or 0)}"
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    cached_cid: Optional[str] = None
    if _CLIENT_ID and (now - _CLIENT_ID_AT) < _CID_TTL:
        cached_cid = _CLIENT_ID
    else:
        cached_cid = _load_disk_cid()
        if cached_cid:
            _CLIENT_ID = cached_cid
            _CLIENT_ID_AT = now

    mp3: Optional[str] = None
    used_cid: Optional[str] = None
    if HAVE_CURL_CFFI:
        try:
            mp3, used_cid = _pipeline(query, target_duration, _http_in_proc, cached_cid)
        except Exception:
            mp3, used_cid = None, cached_cid
    else:
        mp3, used_cid = _pipeline_subprocess(query, target_duration, cached_cid)

    if used_cid and used_cid != cached_cid:
        _CLIENT_ID = used_cid
        _CLIENT_ID_AT = now
        _save_disk_cid(used_cid)

    if mp3 is None and not used_cid:
        # Похоже, SC блокирует — выключаем SC-API на 5 минут, чтобы не тормозить
        _BLOCKED_UNTIL = now + _BLOCK_TTL
    elif mp3:
        _CACHE[key] = (now, mp3)
    return mp3


def invalidate(query: str | None = None) -> int:
    global _CACHE
    if query is None:
        n = len(_CACHE)
        _CACHE = {}
        return n
    q = query.strip().lower()
    n = 0
    for k in [k for k in _CACHE if k.startswith(q + "|")]:
        del _CACHE[k]
        n += 1
    return n
