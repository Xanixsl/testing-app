"""Genius scraper — только для извлечения section-маркеров `[Verse 1: Drake]`,
`[Hook]`, `[Куплет 2: Сява]`, которых нет в LRCLIB.

Текст и тайминги берём из LRCLIB; здесь — только метки.

Работает БЕЗ ключа: используем публичный JSON-эндпоинт поиска и парсим HTML
страницы трека. На случай блокировки/изменения вёрстки оборачиваем всё в
try/except — сбой никогда не должен ломать ответ /api/lyrics.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import unicodedata
from threading import Lock

from velora.api.http import SESSION, DEFAULT_TIMEOUT

_BASE = "https://genius.com"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://genius.com/",
    "X-Requested-With": "XMLHttpRequest",
}
_PAGE_HEADERS = dict(_HEADERS)
_PAGE_HEADERS["Accept"] = "text/html,application/xhtml+xml"
_PAGE_HEADERS.pop("X-Requested-With", None)

_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_CACHE_TTL = 24 * 3600
_CACHE_MAX = 256
_CACHE_LOCK = Lock()

_TAG_RX = re.compile(r"<[^>]+>")
_BR_RX = re.compile(r"<br[^>]*>", re.I)
_DIV_OPEN_RX = re.compile(r"<div\b", re.I)
_DIV_CLOSE_RX = re.compile(r"</div>", re.I)
_LYRIC_START_RX = re.compile(
    r'<div[^>]*data-lyrics-container="true"[^>]*>',
    re.I,
)
_SECTION_RX = re.compile(r"^\s*\[[^\]]+\]\s*$")
_NORM_KEEP_RX = re.compile(r"[a-z0-9а-я]+")


def _extract_container_blocks(html_text: str) -> list[str]:
    """Глубинно-корректное извлечение содержимого всех `data-lyrics-container=true`
    `<div>`-блоков. Считаем баланс `<div>`/`</div>`, чтобы не обрезаться о
    вложенные `<div>`."""
    out: list[str] = []
    pos = 0
    text = html_text or ""
    while True:
        m = _LYRIC_START_RX.search(text, pos)
        if not m:
            break
        start = m.end()
        depth = 1
        i = start
        while depth > 0 and i < len(text):
            o = _DIV_OPEN_RX.search(text, i)
            c = _DIV_CLOSE_RX.search(text, i)
            if not c:
                break
            if o and o.start() < c.start():
                depth += 1
                i = o.end()
            else:
                depth -= 1
                i = c.end()
        out.append(text[start:i - len("</div>")])
        pos = i
    return out


def _norm(s: str) -> str:
    """Нормализация строки для сопоставления LRCLIB ↔ Genius:
    нижний регистр, без диакритики, только буквы/цифры."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return "".join(_NORM_KEEP_RX.findall(s))


def _search_song_url(artist: str, title: str) -> str | None:
    try:
        r = SESSION.get(
            f"{_BASE}/api/search/multi",
            params={"q": f"{artist} {title}"},
            headers=_HEADERS,
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    a_norm = _norm(artist)
    t_norm = _norm(title)
    best = None
    for sec in (data.get("response", {}) or {}).get("sections", []):
        for hit in sec.get("hits", []):
            if hit.get("type") != "song":
                continue
            res = hit.get("result") or {}
            url = res.get("url")
            if not url:
                continue
            ra = _norm(res.get("primary_artist", {}).get("name", ""))
            rt = _norm(res.get("title", ""))
            # Точное совпадение исполнителя ИЛИ сильное пересечение названий.
            score = 0
            if ra and (ra in a_norm or a_norm in ra):
                score += 2
            if rt and t_norm and (rt in t_norm or t_norm in rt):
                score += 2
            if score >= 2 and best is None:
                best = url
            if score >= 4:
                return url
    return best


def _extract_lines(html_text: str) -> list[str]:
    """Возвращает список строк lyrics-блоков: и `[Section]`-маркеры, и обычные
    строки текста — в том порядке, как на странице."""
    out: list[str] = []
    blocks = _extract_container_blocks(html_text or "")
    for b in blocks:
        b = _BR_RX.sub("\n", b)
        b = _TAG_RX.sub("", b)
        b = html_mod.unescape(b)
        for raw in b.splitlines():
            line = raw.strip()
            if not line:
                continue
            out.append(line)
    return out


def _fetch_song_lines(url: str) -> list[str]:
    try:
        r = SESSION.get(url, headers=_PAGE_HEADERS, timeout=DEFAULT_TIMEOUT)
        if r.status_code != 200:
            return []
        return _extract_lines(r.text)
    except Exception:
        return []


def fetch_sections(artist: str, title: str) -> list[tuple[str, list[str]]]:
    """Возвращает список `(norm_first_words, [section_label, ...])`:
    «перед строкой, нормализованное начало которой = norm_first_words,
    нужно вставить эти секции».

    norm_first_words — нормализованные первые ~20 символов первой строки
    куплета (стабильный «отпечаток» строки для матчинга с LRCLIB).
    """
    if not artist or not title:
        return []
    key = (_norm(artist)[:64], _norm(title)[:64])
    if not key[0] or not key[1]:
        return []
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit[0] < _CACHE_TTL):
            return hit[1]

    url = _search_song_url(artist, title)
    if not url:
        _store(key, [])
        return []
    lines = _fetch_song_lines(url)
    if not lines:
        _store(key, [])
        return []

    out: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    for ln in lines:
        if _SECTION_RX.match(ln):
            pending.append(ln.strip())
            continue
        # Genius иногда склеивает «Read More\xa0[Section]» — выцепим хвост.
        m = re.search(r"\[[^\]]+\]\s*$", ln)
        if m and ln[: m.start()].strip().lower().endswith("read more"):
            pending.append(m.group(0).strip())
            continue
        norm = _norm(ln)[:24]
        if not norm:
            continue
        if pending:
            out.append((norm, pending))
            pending = []
    if pending:
        # Хвостовые секции без следующей строки — отбросим, привязать некуда.
        pass
    _store(key, out)
    return out


def _store(key, value) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for k in list(_CACHE.keys())[: _CACHE_MAX // 10]:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time(), value)


def normalize_for_match(s: str) -> str:
    """Экспорт нормализатора для использования в lyrics.py."""
    return _norm(s)[:24]
