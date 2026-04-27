"""Резолвер полного аудиопотока через yt-dlp.

Каскад: SoundCloud → YouTube. Отсеивает 30-секундные превью / сниппеты по длительности.
При сетевых сбоях источник банится на короткое время, чтобы не вешать UI.

Дополнительно: предпочитает explicit/uncensored версии. Если каталог
(Deezer/iTunes) дал «чистое» название, мы сначала ищем по альтернативному
запросу с подсказкой `explicit` и пенализируем кандидаты с пометками
`clean / radio edit / censored / без мата` в заголовке.
"""
from __future__ import annotations

import re
import time

import yt_dlp

# curl_cffi с impersonate=chrome — единственный надёжный способ обойти
# TLS-fingerprint блокировку YouTube/SoundCloud на shared-хостинге.
# Стандартный urllib yt-dlp даёт SSL EOF при первичном handshake.
_IMPERSONATE = None
_IMPERSONATE_DIAG = ""
try:
    import sys as _sys, os as _os
    # На WSGI-хосте sys.path может НЕ содержать venv site-packages даже после
    # site.addsitedir в site.wsgi (mod_wsgi/passenger особенности). Force-add.
    _here = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _venv_sp = _os.path.join(_here, "venv", "lib", "python3.13", "site-packages")
    if _os.path.isdir(_venv_sp) and _venv_sp not in _sys.path:
        _sys.path.insert(0, _venv_sp)
    _IMPERSONATE_DIAG = (
        f"py={_sys.version_info[:3]} exe={_sys.executable} "
        f"venv_sp_in_path={_venv_sp in _sys.path}"
    )
    try:
        import curl_cffi as _cc
        _IMPERSONATE_DIAG += f" curl_cffi={getattr(_cc, '__version__', '?')}"
        from yt_dlp.networking.impersonate import ImpersonateTarget
        _IMPERSONATE = ImpersonateTarget("chrome")
    except Exception as _ce:
        _IMPERSONATE_DIAG += f" curl_cffi_err={_ce!r}"
except Exception as _ie:
    _IMPERSONATE_DIAG += f" outer_err={_ie!r}"


_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "format": "bestaudio/best",
    "extract_flat": False,
    "geo_bypass": True,
    "geo_bypass_country": "US",
    # source_address=0.0.0.0 несовместим с curl_cffi (segfault на shared host).
    # "source_address": "0.0.0.0",
    # На shared-хостинге сеть до youtube/soundcloud медленнее, чем локально.
    # 10с — достаточно для большинства треков; 25с давало timeout у YouTube → ban.
    "socket_timeout": 10,
    "retries": 0,
    "fragment_retries": 0,
    "extractor_retries": 0,
    "nocheckcertificate": True,
    # На shared-хостинге yt-dlp's стандартный urllib даёт SSL EOF на YT и SC
    # (TLS-fingerprint detection). curl_cffi с impersonate=chrome обходит
    # это, мимикрируя реальный браузерный handshake.
    "impersonate": _IMPERSONATE,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    },
    # YouTube без PoToken не отдаёт audio-форматы (только картинки),
    # поэтому YouTube-источник по факту не используется — мы полагаемся
    # на SoundCloud. Тем не менее оставляем android-клиент: иногда
    # «короткие» / некоторые ID отдают rate-limited fallback.
    "extractor_args": {
        # ios/android player_client отдают audio formats БЕЗ "Sign in to confirm
        # you're not a bot" и БЕЗ curl_cffi (mod_wsgi sub-interpreters не дают
        # грузить _cffi_backend → impersonate=chrome недоступен в worker'ах).
        # mweb — fallback на случай rate-limit. tv_embedded убран: возвращает
        # entries без formats (ytsearch15 PICK url=NO).
        "youtube": {"player_client": ["ios", "android", "mweb", "web"]},
        "youtubetab": {"skip": ["webpage"]},
    },
}

# Если impersonate недоступен (curl_cffi не загрузился в WSGI worker) —
# убираем ключ полностью, иначе yt-dlp падает с
# "Impersonate target chrome is not available".
if _IMPERSONATE is None:
    _BASE_OPTS.pop("impersonate", None)

_SOURCES: list[tuple[str, str]] = [
    # YouTube первым: на SoundCloud SSL EOF на shared-хостинге (TLS-fingerprint,
    # без impersonate он там не работает). Пул 15 — больше шансов обойти
    # отдельные age-gated/private видео и найти оригинал.
    ("ytsearch15", "YouTube"),
    ("scsearch15", "SoundCloud"),
]

_MIN_DURATION = 60
# TTL 6 часов: googlevideo / sndcdn URL живут 6-12ч. В памяти плюс на диске.
_TTL = 6 * 3600
_CACHE: dict[tuple, tuple[float, str]] = {}

# Persistent disk cache (переживает перезапуск workers)
import json as _json
import os as _os_mod
_CACHE_FILE = _os_mod.path.join(
    _os_mod.path.dirname(_os_mod.path.dirname(_os_mod.path.dirname(_os_mod.path.abspath(__file__)))),
    "instance", "_resolver_cache.json",
)
_CACHE_LAST_SAVE = 0.0

def _cache_load() -> None:
    global _CACHE
    try:
        if not _os_mod.path.isfile(_CACHE_FILE):
            return
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = _json.load(f) or {}
        now = time.time()
        loaded = 0
        for k, v in raw.items():
            try:
                parts = k.split("\x1f")
                if len(parts) != 3:
                    continue
                key = (parts[0], int(parts[1]), parts[2])
                ts, url = v[0], v[1]
                if now - ts < _TTL:
                    _CACHE[key] = (float(ts), str(url))
                    loaded += 1
            except Exception:
                continue
        _flog(f"CACHE loaded {loaded} entries from disk")
    except Exception as exc:
        _flog(f"CACHE load err: {exc}")

def _cache_save(force: bool = False) -> None:
    global _CACHE_LAST_SAVE
    now = time.time()
    if not force and now - _CACHE_LAST_SAVE < 30:
        return
    try:
        _os_mod.makedirs(_os_mod.path.dirname(_CACHE_FILE), exist_ok=True)
        out = {}
        for k, v in _CACHE.items():
            try:
                key_s = f"{k[0]}\x1f{k[1]}\x1f{k[2]}"
                out[key_s] = [v[0], v[1]]
            except Exception:
                continue
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(out, f, ensure_ascii=False)
        _os_mod.replace(tmp, _CACHE_FILE)
        _CACHE_LAST_SAVE = now
    except Exception as exc:
        _flog(f"CACHE save err: {exc}")


# ---- Очередь промахов для внешнего GitHub Actions резолвера ---------------
# Когда локальный yt-dlp на Sprinthost не смог достать поток (IP/TLS-блок),
# складываем (q, duration, quality) в очередь. Внешний worker (раз в 10 мин
# через GitHub Actions, IP не в бане YT) забирает её через /api/resolve/queue,
# резолвит и пушит готовые URL обратно через /api/resolve/push.
_QUEUE_FILE = _os_mod.path.join(
    _os_mod.path.dirname(_CACHE_FILE), "_resolve_queue.json",
)
_QUEUE_TTL = 24 * 3600  # запись живёт сутки, потом удаляется как протухшая
_QUEUE_MAX = 500


def _queue_load() -> dict:
    try:
        if not _os_mod.path.isfile(_QUEUE_FILE):
            return {}
        with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
            return _json.load(f) or {}
    except Exception:
        return {}


def _queue_save(data: dict) -> None:
    try:
        _os_mod.makedirs(_os_mod.path.dirname(_QUEUE_FILE), exist_ok=True)
        tmp = _QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
        _os_mod.replace(tmp, _QUEUE_FILE)
    except Exception as exc:
        _flog(f"QUEUE save err: {exc}")


def queue_add(q: str, target_duration: int = 0, quality: str = "hi") -> None:
    """Регистрирует промах резолва — будет подхвачено внешним worker'ом."""
    if not q:
        return
    try:
        data = _queue_load()
        key = f"{q}\x1f{int(target_duration or 0)}\x1f{quality}"
        rec = data.get(key) or {}
        now = time.time()
        rec.update({
            "q": q,
            "duration": int(target_duration or 0),
            "quality": quality,
            "ts": now,
            "fails": int(rec.get("fails", 0)) + 1,
        })
        data[key] = rec
        # Чистим протухшие/обрезаем по размеру (оставляем самые свежие).
        cutoff = now - _QUEUE_TTL
        data = {k: v for k, v in data.items() if float(v.get("ts", 0)) >= cutoff}
        if len(data) > _QUEUE_MAX:
            items = sorted(data.items(), key=lambda kv: float(kv[1].get("ts", 0)), reverse=True)
            data = dict(items[:_QUEUE_MAX])
        _queue_save(data)
    except Exception as exc:
        _flog(f"QUEUE add err: {exc}")


def queue_pop_batch(limit: int = 20) -> list[dict]:
    """Возвращает до `limit` самых свежих записей очереди (без удаления).
    Удаление происходит только при успешном push'е через cache_put_external.
    """
    data = _queue_load()
    items = sorted(data.values(), key=lambda v: float(v.get("ts", 0)), reverse=True)
    return items[: max(1, min(int(limit or 20), 100))]


def cache_put_external(q: str, target_duration: int, quality: str, url: str) -> bool:
    """Кладёт готовый URL (от внешнего резолвера) в основной кэш и убирает
    запись из очереди промахов. Возвращает True, если URL принят.
    """
    if not q or not url or not url.startswith("http"):
        return False
    base = apply_query_override(q.strip())
    key = (base, int(target_duration or 0), quality if quality in ("hi", "low") else "hi")
    _CACHE[key] = (time.time(), url)
    try:
        _cache_save(force=True)
    except Exception:
        pass
    # удалить из очереди (по исходному q + по нормализованному base)
    try:
        data = _queue_load()
        changed = False
        for q_try in (q, base):
            qkey = f"{q_try}\x1f{int(target_duration or 0)}\x1f{quality}"
            if qkey in data:
                del data[qkey]
                changed = True
        if changed:
            _queue_save(data)
    except Exception:
        pass
    return True


# Если источник падает несколько раз подряд — на короткое время банится,
# чтобы пользователь не ждал по 10+ секунд каждый клик.
_SOURCE_BAN: dict[str, float] = {}
_SOURCE_FAILS: dict[str, int] = {}
# Короткий бан, чтобы быстро возвращаться к работе после временных
# SSL EOF / age-gate ошибок. Раньше было 90s/2 fails — слишком агрессивно
# (один SSL hiccup → клиент 90с не может слушать треки).
_BAN_TTL = 25
_FAIL_THRESHOLD = 4

# ---- Anti-censor ---------------------------------------------------
# Слова в title кандидата, которые означают «чистая» / зацензуренная версия.
_BAD_MARKERS = (
    "clean", "radio edit", "radio version", "radio mix", "censored",
    "edited", "no swearing", "без мата", "цензур", "детская версия",
    "без мата", "for radio",
)
# Маркеры каверов / мэшапов / переделок — БОЛЬШОЙ штраф, чтобы yt-dlp
# никогда не выбрал чужое исполнение оригинального трека.
_COVER_MARKERS = (
    "cover", "кавер", "mashup", "мэшап", "мешап", "remix", "ремикс",
    "rework", "tribute", "trubute", "karaoke", "караоке", "minus",
    "минус", "instrumental", "инструментал", "live", "лайв",
    "acoustic", "акустика", "под гитару", "guitar version", "piano version",
    "school", "школьник", "girl version", "версия от", "by ",
    "от группы", "перепел", "перепела", "spedup", "sped up", "slowed",
    "nightcore", "найткор", "ai cover", "ai-cover",
)
# Слова, которые означают «оригинал/explicit» — таким даём бонус.
_GOOD_MARKERS = (
    "explicit", "uncensored", "original", "оригинал", "без цензур",
    "explicit version",
)

# Известные «зацензуренные» названия → оригинал. Подмена применяется
# только если артист совпадает (для безопасности). Ключ — (artist_lower, title_lower).
_TITLE_OVERRIDES: dict[tuple[str, str], str] = {
    ("сява", "в этой оу е"): "В этой траве",
    ("сява", "меня вставляет ритм"): "Меня вставляет дым",
    # сюда легко дописывать новые: ("артист lower", "что в каталоге lower"): "оригинальное название"
}

# Полная замена пары (artist, title) → (real_artist, real_title).
# Используется когда каталог приписал трек чужому артисту (репост / кавер / cover-version).
# Применяется И в выдаче треков (UI), И в q-запросе резолвера, чтобы yt-dlp
# искал оригинал нужного исполнителя, а не каверы.
_PAIR_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    ("серега пират", "в этой оу е"): ("Сява", "В этой траве"),
    ("серёга пират", "в этой оу е"): ("Сява", "В этой траве"),
    ("серега пират", "в этой траве"): ("Сява", "В этой траве"),
    ("серёга пират", "в этой траве"): ("Сява", "В этой траве"),
    ("серега пират", "меня вставляет ритм"): ("Сява", "Меня вставляет дым"),
    ("серёга пират", "меня вставляет ритм"): ("Сява", "Меня вставляет дым"),
    ("серега пират", "меня вставляет дым"): ("Сява", "Меня вставляет дым"),
    ("серёга пират", "меня вставляет дым"): ("Сява", "Меня вставляет дым"),
}

# Глобальная замена артиста: если каталог приписывает все треки одного
# исполнителя другому имени (антицензурный аккаунт-перезалив), заменяем
# артиста ВСЕГДА. Ключ — каталожное имя в lower, значение — оригинал.
# Используется когда лень/невозможно перечислять каждый трек.
_ARTIST_ALIASES: dict[str, str] = {
    "серега пират": "Сява",
    "серёга пират": "Сява",
}

# Подмена прямо в строке query (артист+название склеены). Применяется
# регуляркой по подстроке — для случаев когда клиент шлёт q='Сява - В этой оу е'.
_QUERY_OVERRIDES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), repl) for p, repl in [
        (r"\bв\s*этой\s*оу\s*е\b", "В этой траве"),
        (r"\bменя\s+вставляет\s+ритм\b", "Меня вставляет дым"),
        # Если в q затесался Серега Пират (не оригинальный исполнитель)
        # — заменяем на Сяву, чтобы yt-dlp нашёл оригинал, а не каверы.
        (r"\bсер[её]га\s+пират\b", "Сява"),
    ]
]


def apply_pair_override(artist: str, title: str) -> tuple[str, str]:
    """Полная замена (артист, название) → правильная пара. Если не найдено —
    возвращает исходного артиста и (возможно) очищенное название.
    """
    if not title:
        return artist, title
    a_low = (artist or "").strip().lower()
    t_low = title.strip().lower()
    pair = _PAIR_OVERRIDES.get((a_low, t_low))
    if pair:
        return pair
    # Глобальный artist-alias: подменяем артиста и чистим название от цензуры.
    if a_low in _ARTIST_ALIASES:
        new_artist = _ARTIST_ALIASES[a_low]
        # Сначала ищем точное название в TITLE_OVERRIDES под НОВЫМ артистом
        # (вдруг там тоже подмена, типа «оу е → траве»).
        new_title = _TITLE_OVERRIDES.get((new_artist.lower(), t_low), title)
        # Если ничего не нашли — снимаем явные маркеры цензуры из названия.
        if new_title == title:
            new_title = _strip_clean_markers(title)
        return new_artist, new_title
    # Иначе — обычная подмена только title (или авто-чистка маркеров цензуры).
    return artist, apply_title_override(artist, title)


def search_aliases(query: str) -> list[str]:
    """Возвращает альтернативные варианты поискового запроса.

    Используется когда пользователь ищет ОРИГИНАЛЬНОЕ название, а в
    каталоге трек лежит под зацензуренным («В этой траве» → надо ещё
    спросить «В этой оу е»). Без этого Deezer отдаст пусто.
    """
    if not query:
        return []
    q_low = query.lower()
    out: list[str] = []
    # 1. Reverse pair-override: ("серега пират", "в этой оу е") → ("Сява", "В этой траве").
    #    Если юзер вводит «Сява В этой траве» — добавляем «Серега Пират В этой оу е».
    seen = set()
    for (cat_artist, cat_title), (real_artist, real_title) in _PAIR_OVERRIDES.items():
        ra_low = real_artist.lower()
        rt_low = real_title.lower()
        # Случай 1: юзер ввёл оригинал целиком (артист + название).
        if ra_low in q_low and rt_low in q_low:
            alt = query.lower().replace(rt_low, cat_title).replace(ra_low, cat_artist)
            if alt not in seen:
                seen.add(alt); out.append(alt)
        # Случай 2: юзер ввёл только оригинальное название (без артиста).
        elif rt_low in q_low and ra_low not in q_low:
            alt = query.lower().replace(rt_low, cat_title)
            if alt not in seen:
                seen.add(alt); out.append(alt)
    # 2. Reverse title-override (когда подмена касалась только названия).
    for (orig_artist, censored_title), original_title in _TITLE_OVERRIDES.items():
        ot_low = original_title.lower()
        if ot_low in q_low and censored_title not in q_low:
            alt = query.lower().replace(ot_low, censored_title)
            if alt not in seen:
                seen.add(alt); out.append(alt)
    # 3. Reverse artist-alias: ищет «Сява ...» → также добавит «Серега Пират ...».
    #    Так все треки залитые под другим именем тоже находятся в каталоге.
    for cat_artist_low, real_artist in _ARTIST_ALIASES.items():
        ra_low = real_artist.lower()
        if ra_low in q_low and cat_artist_low not in q_low:
            alt = query.lower().replace(ra_low, cat_artist_low)
            if alt not in seen:
                seen.add(alt); out.append(alt)
    return out


def apply_title_override(artist: str, title: str) -> str:
    """Если каталог дал зацензуренное название — вернёт оригинал.
    Сначала смотрит в ручной словарь, иначе авто-снимает маркеры цензуры
    (Clean / Radio Edit / Censored / [Edited] и т.п.) из конца названия.
    """
    if not title:
        return title
    if artist:
        key = (artist.strip().lower(), title.strip().lower())
        if key in _TITLE_OVERRIDES:
            return _TITLE_OVERRIDES[key]
        # Если у нас есть pair-override — применим его title.
        pair = _PAIR_OVERRIDES.get(key)
        if pair:
            return pair[1]
    return _strip_clean_markers(title)


# Регулярки для авто-снятия пометок «чистая версия» из названия трека.
# Срабатывают и на скобки, и на квадратные скобки, и на тире-суффикс.
_CLEAN_TAG_RX = re.compile(
    r"\s*[\(\[\-]\s*(?:"
    r"clean(?:\s*version)?|radio\s*(?:edit|version|mix)|censored|edited|"
    r"no\s*swearing|без\s*мата|цензур\w*|детская\s*версия|for\s*radio|"
    r"clean\s*radio\s*edit"
    r")\s*[\)\]]?\s*$",
    re.IGNORECASE,
)


def _strip_clean_markers(title: str) -> str:
    """Удаляет суффиксы вроде '(Clean)', '[Radio Edit]', '- Censored'."""
    out = title
    # Применяем несколько раз — если суффиксов несколько подряд.
    for _ in range(3):
        new = _CLEAN_TAG_RX.sub("", out).rstrip(" -—–")
        if new == out:
            break
        out = new
    return out.strip() or title


def apply_query_override(query: str) -> str:
    """Подменяет известные зацензуренные подстроки прямо в q-запросе."""
    if not query:
        return query
    out = query
    for rx, repl in _QUERY_OVERRIDES:
        out = rx.sub(repl, out)
    return out


def _entry_score(entry: dict, target_duration: int = 0) -> float:
    """Чем больше — тем лучше кандидат.

    ВАЖНО: длительность — доминирующий критерий. Если разница > 15 сек,
    кандидат получает огромный штраф (это почти наверняка другой трек).
    Anti-censor бонус/штраф мелкий и работает только как тай-брейкер
    между похожими по длительности кандидатами.
    """
    title = (entry.get("title") or "").lower()
    score = 0.0
    dur = int(entry.get("duration") or 0)
    if target_duration and dur:
        diff = abs(dur - target_duration)
        if diff <= 3:
            score += 100  # почти точное совпадение — почти наверняка тот трек
        elif diff <= 8:
            score += 60
        elif diff <= 15:
            score += 20
        else:
            # Чужой трек: -10 за каждую секунду сверх 15.
            score -= (diff - 15) * 10
    elif dur:
        # Без таргета — отсекаем превью, но не растягиваем на час+.
        score += min(10, dur / 60)
        if dur > 900:  # 15 минут — скорее лонгмикс/час непрерывной
            score -= 30
    # Anti-censor — серьёзный приоритет. Если кандидат отмечен как clean/
    # censored/radio-edit, а есть «обычный» вариант той же длительности —
    # он должен победить. Поэтому пенальти достаточно большое, чтобы
    # перебить duration-bonus 100 у точного совпадения с цензурой,
    # когда рядом есть кандидат без пометок (тот получит 100, цензурный — 100-60 = 40).
    for m in _BAD_MARKERS:
        if m in title:
            score -= 60
            break
    for m in _GOOD_MARKERS:
        if m in title:
            score += 25
            break
    # Anti-cover — БОЛЬШОЙ штраф. Лучше совсем не сыграть, чем включить
    # кавер/мэшап/ремикс/караоке/инструментал вместо оригинала.
    for m in _COVER_MARKERS:
        if m in title:
            score -= 80
    # Просмотры/лайки — самый слабый тай-брейкер.
    score += min(3, (entry.get("view_count") or 0) / 5_000_000)
    return score


def _format_url(entry: dict, quality: str = "hi") -> str | None:
    """Выбирает URL формата.

    quality:
      - "hi"   — старое поведение: максимальный abr (для онлайн-плеера).
      - "low"  — для оффлайн-загрузки. Сначала пробует opus с abr<=96
                 (это «прозрачное» качество — ухом неотличимо от исходника
                 на наушниках, но 30–50% от размера). Если такого нет —
                 любой формат с abr<=128. Иначе минимальный из доступных.
    """
    url = entry.get("url")
    formats = entry.get("formats", []) or []
    # Сначала ищем прогрессивный (не HLS) — m3u8 нельзя скачать целиком
    # одним файлом, поэтому он непригоден для оффлайн-загрузки.
    progressive = []
    hls = []
    for fmt in formats:
        acodec = fmt.get("acodec")
        if not acodec or acodec == "none":
            continue
        proto = (fmt.get("protocol") or "").lower()
        ext = (fmt.get("ext") or "").lower()
        is_hls = "m3u8" in proto or ext == "m3u8"
        (hls if is_hls else progressive).append(fmt)
    pool = progressive or hls
    if not pool:
        return url
    if quality == "low":
        # Уровни приоритета: (codec_match, abr_cap)
        for codec_pref, abr_cap in (("opus", 96), ("opus", 128), (None, 96), (None, 128)):
            best = None
            for fmt in pool:
                acodec = (fmt.get("acodec") or "").lower()
                abr = fmt.get("abr") or 0
                if codec_pref and codec_pref not in acodec:
                    continue
                if abr and abr > abr_cap:
                    continue
                # Среди подходящих — берём максимальный abr (близкий к потолку).
                if best is None or (abr or 0) > (best.get("abr") or 0):
                    best = fmt
            if best is not None:
                return best["url"]
        # Ничего не подошло под пороги — берём САМЫЙ ЛЁГКИЙ доступный
        # (минимальный abr), всё равно меньше, чем «бест».
        cheapest = None
        for fmt in pool:
            abr = fmt.get("abr") or 0
            if cheapest is None or (abr and abr < (cheapest.get("abr") or 1e9)):
                cheapest = fmt
        return (cheapest or pool[0])["url"]
    # quality == "hi" — максимальный abr.
    best = None
    for fmt in pool:
        if best is None or (fmt.get("abr") or 0) > (best.get("abr") or 0):
            best = fmt
    return best["url"] if best else url


def _pick_best_entry_ex(info: dict, target_duration: int = 0, quality: str = "hi") -> tuple[str | None, bool]:
    """Возвращает (url, only_clean). only_clean=True означает, что среди
    подходящих по длительности кандидатов ВСЕ — цензурные. В таком случае
    вызывающий может попробовать другой запрос (`+ ' explicit'`)."""
    entries = info.get("entries") if "entries" in info else [info]
    candidates = []
    for entry in entries or []:
        if not entry:
            continue
        dur = int(entry.get("duration") or 0)
        if dur and dur < _MIN_DURATION:
            continue
        # Жёстко отсекаем заведомо «не наш» трек: если длительность отличается
        # больше чем на 25 секунд — это почти наверняка другая песня.
        if target_duration and dur and abs(dur - target_duration) > 25:
            continue
        candidates.append(entry)
    if not candidates:
        return None, False

    def _is_clean(e: dict) -> bool:
        t = (e.get("title") or "").lower()
        return any(m in t for m in _BAD_MARKERS)

    non_clean = [e for e in candidates if not _is_clean(e)]
    only_clean = not non_clean
    pool = non_clean if non_clean else candidates
    pool.sort(key=lambda e: -_entry_score(e, target_duration))
    for entry in pool:
        u = _format_url(entry, quality=quality)
        if u:
            return u, only_clean
    return None, only_clean


def _pick_best_entry(info: dict, target_duration: int = 0) -> str | None:
    url, _ = _pick_best_entry_ex(info, target_duration)
    return url


def _flog(msg: str) -> None:
    """Файловый лог для отладки на shared-host (WSGI stderr недоступен)."""
    try:
        import os, time as _t
        from pathlib import Path as _P
        # Пишем в instance/ — там всегда есть write-доступ.
        _root = _P(__file__).resolve().parent.parent.parent
        _dir = _root / "instance"
        _dir.mkdir(parents=True, exist_ok=True)
        with open(_dir / "resolver.log", "a") as _lf:
            _lf.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _try_source_ex(query: str, default_search: str, target_duration: int = 0, quality: str = "hi") -> tuple[str | None, bool]:
    opts = dict(_BASE_OPTS)
    opts["default_search"] = default_search
    _flog(f"TRY {default_search!r} q={query!r}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if not info:
                _flog(f"NO_INFO {default_search}")
                return None, False
            url, only_clean = _pick_best_entry_ex(info, target_duration, quality=quality)
            _flog(f"PICK {default_search} url={'YES' if url else 'NO'} only_clean={only_clean}")
            return url, only_clean
    except Exception as exc:
        msg = f"FAIL {default_search}: {type(exc).__name__}: {exc}"
        print(f"[resolver] {msg}", flush=True)
        _flog(msg)
        return None, False


def _try_source(query: str, default_search: str, target_duration: int = 0) -> str | None:
    url, _ = _try_source_ex(query, default_search, target_duration)
    return url


def resolve_stream(query: str, target_duration: int = 0, quality: str = "hi") -> str | None:
    base = apply_query_override(query.strip())
    if not base:
        return None
    _flog(f"RESOLVE q={base!r} dur={target_duration} q={quality} diag={_IMPERSONATE_DIAG}")
    key = (base, int(target_duration or 0), quality)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _TTL:
        return cached[1]

    # ---- Piped public API: ВЫКЛЮЧЕНО ---------------------------------
    # На апрель 2026 публичные Piped-инстансы массово недоступны (10 из 11
    # отдают 502/503/timeout/DNS-fail), YouTube их жёстко давит. Включать
    # как первый источник вредно — добавляет до 6с latency перед каждым
    # запросом. Файл velora/api/piped.py оставлен для будущего собственного
    # инстанса/VPS-варианта; включить можно через PIPED_ENABLE=1.
    import os as _envos
    if _envos.environ.get("PIPED_ENABLE") == "1":
        try:
            from .piped import search_stream as _piped_search
            piped_url = _piped_search(base, target_duration=int(target_duration or 0))
            if piped_url:
                _flog(f"PICK PIPED q={base!r} url=YES")
                _CACHE[key] = (now, piped_url)
                _cache_save()
                return piped_url
            _flog("PIPED miss")
        except Exception as exc:  # noqa: BLE001
            _flog(f"PIPED err: {type(exc).__name__}: {exc}")

    # ---- БЫСТРЫЙ ПУТЬ: SoundCloud public API (~1 сек) ---------------
    # Минует yt-dlp (медленный + блочится TLS-fingerprint).
    # При сетевой ошибке/блокировке — модуль сам ставит cool-down 5мин,
    # так что повторного штрафа за латентность не будет.
    try:
        from .soundcloud import search_stream as _sc_search
        sc_url = _sc_search(base, target_duration=int(target_duration or 0))
        if sc_url:
            _flog(f"PICK SC-API q={base!r} url=YES")
            _CACHE[key] = (now, sc_url)
            _cache_save()
            return sc_url
        _flog("SC-API miss")
    except Exception as exc:  # noqa: BLE001
        _flog(f"SC-API err: {type(exc).__name__}: {exc}")

    # Explicit-запрос ПЕРВЫМ — чтобы сразу получать оригинальные версии
    # без цензуры/bleep'ов. На проде с медленным yt-dlp оставляем ТОЛЬКО
    # один query: anti-censor логика в _entry_score (П-60/+25) уже отфильтровывает
    # clean-версии. 3 запроса × 2 источника × 10с timeout = до 60с ожидания.
    queries = [base]
    fallback_url: str | None = None
    for q_idx, q in enumerate(queries):
        for src, name in _SOURCES:
            if _SOURCE_BAN.get(name, 0) > now:
                print(f"[resolver] {name} banned, skip", flush=True)
                continue
            print(f"[resolver] trying {name}: {q!r}", flush=True)
            url, only_clean = _try_source_ex(q, src, target_duration, quality=quality)
            if url:
                print(f"[resolver] OK via {name} (only_clean={only_clean})", flush=True)
                _SOURCE_FAILS[name] = 0
                if not only_clean:
                    _CACHE[key] = (now, url)
                    _cache_save()
                    return url
                # Запомним как fallback, продолжим искать «грязный» вариант.
                if fallback_url is None:
                    fallback_url = url
                continue
            _SOURCE_FAILS[name] = _SOURCE_FAILS.get(name, 0) + 1
            if _SOURCE_FAILS[name] >= _FAIL_THRESHOLD:
                _SOURCE_BAN[name] = now + _BAN_TTL
                _SOURCE_FAILS[name] = 0
                print(f"[resolver] {name} banned for {_BAN_TTL}s", flush=True)
    if fallback_url:
        print(f"[resolver] fallback to clean-only URL for {base!r}", flush=True)
        _CACHE[key] = (now, fallback_url)
        _cache_save()
        return fallback_url
    print(f"[resolver] all sources failed for {base!r}", flush=True)
    # Регистрируем промах в очереди для внешнего GitHub Actions резолвера.
    try:
        queue_add(base, int(target_duration or 0), quality)
    except Exception:
        pass
    return None


def invalidate_cache(query: str | None = None, target_duration: int = 0) -> int:
    """Сбрасывает резолвер-кэш. Если query указан — только для него.
    Возвращает число удалённых записей.
    """
    global _CACHE
    if query is None:
        n = len(_CACHE)
        _CACHE = {}
        return n
    base = apply_query_override(query.strip())
    n = 0
    for k in [k for k in _CACHE if k[0] == base and k[1] == int(target_duration or 0)]:
        del _CACHE[k]
        n += 1
    return n


# Загружаем persistent cache при импорте модуля (после того как все функции
# определены — _flog/_CACHE доступны по имени).
try:
    _cache_load()
except Exception:
    pass
