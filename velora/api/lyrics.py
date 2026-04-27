"""LRCLIB — открытое API синхронизированных текстов песен (без ключей).

https://lrclib.net/docs

Стратегия: пробуем несколько вариантов запроса, прежде чем сказать
«текста нет». Это сильно повышает покрытие, особенно для треков с
суффиксами (feat ...), (prod ...), (Clean), (Remix) и т.п.
"""
from __future__ import annotations

import re
import time
from threading import Lock

from velora.api.http import SESSION, DEFAULT_TIMEOUT

BASE = "https://lrclib.net/api"

# Простой in-memory кэш: текст песни редко меняется → 24ч TTL хватает.
_CACHE: dict[tuple[str, str, str, int], tuple[float, dict]] = {}
_CACHE_TTL = 24 * 3600
_CACHE_MAX = 512
_CACHE_LOCK = Lock()

# Скобки, которые мешают поиску текста: (feat. X), [prod by Y], (Clean), (Remix), etc.
_PAREN_NOISE_RX = re.compile(
    r"\s*[\(\[][^\)\]]*?(?:feat\.?|ft\.?|prod\.?|remix|version|edit|mix|"
    r"clean|censored|edited|radio|explicit|original|оригинал|цензур|без\s*мата|remaster\w*)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_DASH_RX = re.compile(r"\s+[-–—]\s+(?:remix|edit|version|mix|clean|radio.*|explicit.*)$", re.IGNORECASE)


def _clean_title_for_search(title: str) -> str:
    """Снимает шумные скобки и суффиксы, не трогая основное название."""
    out = _PAREN_NOISE_RX.sub("", title or "")
    out = _TRAILING_DASH_RX.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip() or title


def _split_artists(artist: str) -> list[str]:
    """'A & B feat. C, D' → ['A', 'B', 'C', 'D'] — для перебора главного исполнителя."""
    if not artist:
        return []
    parts = re.split(r"\s*(?:,|&|feat\.?|ft\.?|x|×|и|and)\s*", artist, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


_NORM_RX = re.compile(r"[^a-z0-9а-яё]+")


def _norm_for_match(s: str) -> str:
    """Жёсткая нормализация для сравнения артистов/названий: только буквы+цифры,
    нижний регистр. 'Drake (feat. Future)' → 'drakefeatfuture'."""
    return _NORM_RX.sub("", (s or "").lower())


def _cache_key(artist: str, title: str, album: str, duration: int) -> tuple[str, str, str, int]:
    return (artist.strip().lower(), title.strip().lower(), (album or "").strip().lower(), int(duration or 0))


def _try_lrclib_get(artist: str, title: str, album: str = "", duration: int = 0) -> dict | None:
    """Точный get. None если 404 или ошибка."""
    if not artist or not title:
        return None
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    try:
        r = SESSION.get(f"{BASE}/get", params=params, timeout=DEFAULT_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() or None
    except Exception:
        return None


def _try_lrclib_search(artist: str, title: str) -> dict | None:
    """Search-эндпоинт: возвращает первый результат (или None)."""
    if not title:
        return None
    arr = _lrclib_search_all(artist, title)
    return arr[0] if arr else None


def _lrclib_search_all(artist: str, title: str) -> list[dict]:
    """Search-эндпоинт: возвращает ВСЕ результаты (массив).

    Нужно, чтобы выбрать варианты на разных языках (английский оригинал
    и русский перевод, например — оба часто лежат в lrclib параллельно).
    """
    if not title:
        return []
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    try:
        r = SESSION.get(f"{BASE}/search", params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json() or []
    except Exception:
        return []


# Доля кириллических букв, выше которой считаем текст «русским».
_CYR_RX = re.compile(r"[А-Яа-яЁё]")
_LAT_RX = re.compile(r"[A-Za-z]")


def _detect_lang(text: str) -> str:
    """ru | en | other — простая эвристика по доле кириллицы/латиницы."""
    if not text:
        return "other"
    cyr = len(_CYR_RX.findall(text))
    lat = len(_LAT_RX.findall(text))
    total = cyr + lat
    if total < 8:
        return "other"
    if cyr / total > 0.4:
        return "ru"
    if lat / total > 0.6:
        return "en"
    return "other"


def get_lyrics(artist: str, title: str, album: str = "", duration: int = 0) -> dict:
    """Возвращает словарь с ОДНОЙ или НЕСКОЛЬКИМИ языковыми версиями текста:

        {
            "primary": "ru" | "en" | "other",
            "variants": {
                "ru": {"synced": [(ms, text)], "plain": "..."},
                "en": {"synced": [...], "plain": "..."},
            },
            "source": "lrclib",
        }

    Параллельно делает get + search и собирает ВСЕ найденные варианты,
    группируя их по детектированному языку. Это позволяет фронту дать
    пользователю выбор «RU/EN», если для трека нашлись обе версии.
    """
    key = _cache_key(artist, title, album, duration)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    title_clean = _clean_title_for_search(title)
    artists = _split_artists(artist) or [artist]
    main_artist = artists[0] if artists else artist

    # Список «задач»: каждая возвращает list[dict] (search) или [dict] (get).
    tasks: list[tuple[str, tuple]] = []
    seen_args: set[tuple] = set()

    def add(kind: str, *args):
        sig = (kind,) + tuple(str(a or "").strip().lower() for a in args)
        if sig in seen_args:
            return
        seen_args.add(sig)
        tasks.append((kind, args))

    add("get", artist, title, album, duration)
    if title_clean and title_clean.lower() != title.lower():
        add("get", artist, title_clean, album, duration)
    add("get", main_artist, title_clean or title, "", 0)
    for a in artists[:2]:
        add("search_all", a, title_clean or title)
    add("search_all", "", title_clean or title)
    # Если в названии есть пометка censored/clean/explicit — ищем ТАКЖЕ
    # explicit-версию того же трека (LRCLIB обычно хранит explicit-версию
    # с оригинальным текстом, а у clean-версии в meta = пустая строка/звёздочки).
    title_lower = (title or "").lower()
    if any(kw in title_lower for kw in ("clean", "censored", "edited", "цензур", "без мата", "radio edit")):
        # Пытаемся найти явный «explicit/original» вариант того же трека.
        base_q = title_clean or title
        for marker in ("explicit", "original"):
            add("search_all", main_artist, f"{base_q} {marker}")
        # И просто без всяких суффиксов — возможно explicit уже под чистым именем.
        if main_artist:
            add("search_all", main_artist, base_q)
    # Доп. запросы на «перевод/russian/english» добавляем ТОЛЬКО если заранее
    # видно, что трек, скорее всего, имеет вторую языковую версию (название
    # содержит латиницу или кириллицу — не оба сразу). Это убирает 9 лишних
    # сетевых запросов в общем случае и ускоряет /api/lyrics в ~2 раза.
    has_cyr = any("а" <= c.lower() <= "я" for c in (title + " " + artist))
    has_lat = any("a" <= c.lower() <= "z" for c in (title + " " + artist))
    if has_cyr ^ has_lat:  # только один из алфавитов → перевод имеет смысл искать
        base_q = title_clean or title
        for marker in ("перевод" if has_lat else "english",):
            add("search_all", "", f"{base_q} {marker}")
            if main_artist:
                add("search_all", main_artist, f"{base_q} {marker}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Параллельно с LRCLIB запускаем Genius — нужен только для section-меток
    # (`[Verse 1: Drake]`), которые LRCLIB вообще не хранит. Поток заработает
    # сразу, и к моменту мерджа результат уже будет готов.
    genius_future = None
    try:
        from velora.api import genius as _genius
        _genius_executor = ThreadPoolExecutor(max_workers=1)
        genius_future = _genius_executor.submit(_genius.fetch_sections, artist, title)
    except Exception:
        _genius_executor = None
        genius_future = None
        _genius = None

    def run(kind, args):
        try:
            if kind == "get":
                d = _try_lrclib_get(*args)
                return [d] if d else []
            return _lrclib_search_all(*args)
        except Exception:
            return []

    # ---- Скоринг кандидатов: чтобы НЕ цеплять чужие тексты ----
    # Главные сигналы (по убыванию важности):
    #   1) совпадение длительности с реальным треком (рассинхрон бьёт по UX),
    #   2) совпадение исполнителя,
    #   3) совпадение названия,
    #   4) наличие syncedLyrics,
    #   5) длина текста (на равных).
    expected_artist_norm = _norm_for_match(artist)
    expected_artists_split = {_norm_for_match(a) for a in artists if a}
    expected_title_norm = _norm_for_match(title_clean or title)

    def _score_candidate(d: dict) -> tuple:
        text = d.get("plainLyrics") or d.get("syncedLyrics") or ""
        # Длительность
        cand_dur = int(d.get("duration") or 0)
        dur_score = 0
        if duration and cand_dur:
            diff = abs(cand_dur - int(duration))
            if diff <= 2:
                dur_score = 4
            elif diff <= 5:
                dur_score = 3
            elif diff <= 10:
                dur_score = 1
            elif diff <= 15:
                dur_score = -2
            else:
                # Разница >15с почти гарантирует, что LRC принадлежит
                # другой версии трека (ремикс/clip/extended). На таком LRC
                # таймстемпы расходятся с аудио — лирика «бежит вперёд» или
                # отстаёт. Жёсткий штраф вынудит брать plain или другую запись.
                dur_score = -10
        # Артист
        cand_artist = _norm_for_match(d.get("artistName") or "")
        art_score = 0
        if cand_artist and (expected_artist_norm or expected_artists_split):
            if cand_artist == expected_artist_norm:
                art_score = 3
            elif any(a and (a in cand_artist or cand_artist in a) for a in expected_artists_split):
                art_score = 2
            elif expected_artist_norm and (
                expected_artist_norm in cand_artist or cand_artist in expected_artist_norm
            ):
                art_score = 1
            else:
                art_score = -2  # совсем чужой артист
        # Название
        cand_title = _norm_for_match(d.get("trackName") or d.get("name") or "")
        ttl_score = 0
        if cand_title and expected_title_norm:
            if cand_title == expected_title_norm:
                ttl_score = 3
            elif expected_title_norm in cand_title or cand_title in expected_title_norm:
                ttl_score = 1
            else:
                ttl_score = -2
        synced_score = 2 if d.get("syncedLyrics") else 0
        len_score = min(len(text) // 400, 3)
        # Кортеж сравнения: суммарный вес + tie-breakers
        total = dur_score + art_score + ttl_score + synced_score + len_score
        return (total, synced_score, dur_score, art_score, ttl_score, len_score)

    # Все candidate-ы; группируем по языку, в каждой группе — лучший по скору.
    by_lang: dict[str, dict] = {}
    by_lang_score: dict[str, tuple] = {}

    def _consider(d: dict):
        text = d.get("plainLyrics") or d.get("syncedLyrics") or ""
        if not text:
            return
        score = _score_candidate(d)
        # Жёсткий фильтр: если суммарный скор глубоко отрицательный — это чужой
        # трек (например, та же «Jealous», но Labrinth, а не 9mice). Отбрасываем.
        if score[0] < -3:
            return
        # Дополнительный страж: если ни артист, ни название не совпали хотя бы
        # частично — это почти наверняка не та песня. Часто бывает на цензурных
        # ('Clean'/'Censored') треках, где LRCLIB возвращает совпадение по
        # одному только trackName из чужого исполнителя.
        # _score_candidate возвращает (total, synced_score, dur_score, art_score, ttl_score, len_score)
        art_score = score[3]
        ttl_score = score[4]
        if art_score <= 0 and ttl_score <= 0:
            return
        if art_score <= -2 or ttl_score <= -2:
            return
        # Если LRC принадлежит другой длительности — синхронизация поедет.
        # Лучше выбросить syncedLyrics и оставить только plain (статичный
        # текст без караоке-подсветки), чем показывать «бегущие» строки.
        dur_score = score[2]
        if dur_score <= -10 and d.get("syncedLyrics"):
            d = dict(d)
            d.pop("syncedLyrics", None)
        lang = _detect_lang(text)
        cur_score = by_lang_score.get(lang)
        if cur_score is None or score > cur_score:
            by_lang[lang] = d
            by_lang_score[lang] = score

    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as ex:
        futures = [ex.submit(run, k, a) for k, a in tasks]
        try:
            t_started = time.time()
            for fut in as_completed(futures, timeout=DEFAULT_TIMEOUT + 2):
                arr = fut.result() or []
                for d in arr[:5]:  # не больше 5 кандидатов с одного запроса
                    if isinstance(d, dict):
                        _consider(d)
                # Ранний выход: если уже есть и ru, и en — хватит
                # (с приоритетом на synced; если оба synced — точно стоп).
                if "ru" in by_lang and "en" in by_lang:
                    if by_lang["ru"].get("syncedLyrics") and by_lang["en"].get("syncedLyrics"):
                        break
                # Если уже есть synced-вариант с УВЕРЕННЫМ скором (артист+название
                # совпали, длительность близка) и прошло >1.5с — выходим. Высокий
                # порог нужен, чтобы не зацепить «однофамильца» (например, Labrinth
                # Jealous вместо 9mice Jealous).
                if (time.time() - t_started) > 1.5 and by_lang_score:
                    best = max(by_lang_score.values())
                    if best[0] >= 8 and best[1] >= 2:  # total>=8 + synced есть
                        break
        except Exception:
            pass

    if not by_lang:
        result = {"primary": "other", "variants": {}, "source": "lrclib"}
        _store(key, result)
        return result

    variants: dict[str, dict] = {}
    for lang, d in by_lang.items():
        synced_raw = d.get("syncedLyrics") or ""
        plain = d.get("plainLyrics") or ""
        variants[lang] = {
            "synced": _parse_lrc(synced_raw),
            "plain": plain,
        }

    # Подмешиваем section-маркеры из Genius (LRCLIB их не отдаёт).
    # Делаем только для variant с synced — иначе бессмысленно.
    try:
        sections = []
        if genius_future is not None and _genius is not None:
            try:
                sections = genius_future.result(timeout=2.5) or []
            except Exception:
                sections = []
            finally:
                if _genius_executor is not None:
                    _genius_executor.shutdown(wait=False)
        if sections and _genius is not None:
            for v in variants.values():
                if v["synced"]:
                    v["synced"] = _merge_sections(v["synced"], sections, _genius.normalize_for_match)
    except Exception:
        pass

    # Primary: предпочитаем язык с синхронизацией; при равенстве — ru > en > other.
    def _score(lang: str) -> tuple:
        v = variants[lang]
        return (1 if v["synced"] else 0, {"ru": 2, "en": 1}.get(lang, 0))
    primary = max(variants.keys(), key=_score)

    result = {
        "primary": primary,
        "variants": variants,
        "source": "lrclib",
    }
    _store(key, result)
    return result


def _store(key, value: dict) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # Простой FIFO — удаляем ~10% самых старых.
            for k in list(_CACHE.keys())[: _CACHE_MAX // 10]:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time(), value)


def _parse_lrc(text: str) -> list[tuple[int, str]]:
    """Парсит синхронизированный LRC. Сохраняет section-маркеры
    `[Verse 1: Drake]`, `[Hook]`, `[Куплет 2: Сява]` — они идут отдельной
    строкой БЕЗ временной метки и обычно стоят перед таймкодом следующей
    строки. Привязываем их к таймстемпу следующей строки (или текущей,
    если стоят в одной строке с ней).

    Поддерживает стандартный LRC-тэг `[offset:±NNN]` (миллисекунды).
    Положительный offset = текст должен отображаться раньше → вычитаем
    из каждого таймстемпа. Без этого многие LRC «бегут вперёд» аудио.
    """
    out: list[tuple[int, str]] = []
    pending_sections: list[str] = []
    offset_ms = 0
    # Сначала пройдёмся быстрым проходом, чтобы найти offset (он обычно
    # в шапке файла, но стандарт не запрещает в середине).
    for raw in (text or "").splitlines():
        s = raw.strip().lower()
        if s.startswith("[offset:"):
            try:
                val = s[len("[offset:"):].rstrip("]").strip().replace("+", "")
                offset_ms = int(val)
            except (ValueError, IndexError):
                offset_ms = 0
            break
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("["):
            # Бывает: текст без таймкода в синхро-LRC (редко). Кладём с -1.
            out.append((-1, line))
            continue
        # Случай 1: чистая section-метка вида `[Verse: Drake]` без таймкода.
        # mm:ss обязательно содержит цифру и двоеточие; если их нет —
        # это section-маркер.
        try:
            ts_end = line.index("]")
        except ValueError:
            continue
        ts = line[1:ts_end]
        rest = line[ts_end + 1 :].strip()
        # Эвристика: «mm:ss[.xx]» — содержит ':' и обе части цифровые.
        is_time = False
        if ":" in ts:
            mm, sub = ts.split(":", 1)
            ss = sub.split(".", 1)[0]
            if mm.isdigit() and ss.isdigit():
                is_time = True
        if not is_time:
            # Стандартные LRC-метаданные ([ar:], [ti:], [al:], [offset:],
            # [length:], [by:], [re:], [ve:]) — игнорируем, это не текст.
            head = ts.split(":", 1)[0].lower() if ":" in ts else ts.lower()
            if head in {"ar", "ti", "al", "offset", "length", "by", "re", "ve", "au", "la"}:
                continue
            # Section-маркер без таймстемпа → копим, привяжем к следующей строке.
            pending_sections.append(f"[{ts}]")
            # Если после ] идёт ещё текст в той же строке — пишем как обычную строку
            # с таймстемпом -1 (нечасто, fallback).
            if rest:
                out.append((-1, rest))
            continue
        # Таймстемп валидный → парсим.
        try:
            mm, sub = ts.split(":", 1)
            if "." in sub:
                ss, ms = sub.split(".", 1)
                ms = (ms + "00")[:3]
            else:
                ss, ms = sub, "0"
            ms_total = int(mm) * 60_000 + int(ss) * 1000 + int(ms.ljust(3, "0"))
        except (ValueError, IndexError):
            continue
        # Применяем LRC offset (см. шапку функции).
        if offset_ms:
            ms_total = max(0, ms_total - offset_ms)
        # Случай 2: после таймстемпа сразу идёт `[Verse: ...]` встроенно.
        is_inline_section = False
        if rest.startswith("[") and rest.endswith("]") and "]" in rest:
            inner = rest[1:rest.index("]")]
            # это section, если внутри НЕТ времени (mm:ss)
            if not (":" in inner and inner.split(":", 1)[0].isdigit()):
                is_inline_section = True
        if is_inline_section:
            pending_sections.append(rest)
            continue
        # Сначала выливаем накопленные секции с этим таймстемпом.
        for sec in pending_sections:
            out.append((ms_total, sec))
        pending_sections = []
        out.append((ms_total, rest))
    # Хвостовые секции без следующей строки — оставим как есть с -1.
    for sec in pending_sections:
        out.append((-1, sec))
    return out


def _merge_sections(synced, sections, normalizer):
    """Вставляет section-маркеры из Genius (`[Verse 1: Drake]`, `[Hook]`,
    `[Куплет 2: Сява]`) в LRCLIB synced-список.

    `synced` — список (t_ms, text) от LRCLIB.
    `sections` — список (norm_first_chars, [section_label, ...]) от Genius:
       «перед строкой, нормализованное начало которой == norm_first_chars,
       нужно вставить эти секции».

    Идём по synced последовательно и поддерживаем указатель в sections.
    Если строка нормализованно совпадает с текущим ключом sections —
    вставляем секции с тем же таймстемпом и продвигаемся.
    """
    if not sections:
        return synced
    out: list[tuple[int, str]] = []
    sec_iter = iter(sections)
    cur_sec = next(sec_iter, None)
    for ts, text in synced:
        if not text or text.startswith("["):
            out.append((ts, text))
            continue
        if cur_sec is not None:
            target_norm, labels = cur_sec
            line_norm = normalizer(text)
            # Достаточно совпадения первых ~8 нормализованных символов —
            # вариации пунктуации/регистра не мешают.
            if (
                target_norm
                and line_norm
                and (
                    target_norm.startswith(line_norm[:8])
                    or line_norm.startswith(target_norm[:8])
                )
            ):
                for lab in labels:
                    out.append((ts, lab))
                cur_sec = next(sec_iter, None)
        out.append((ts, text))
    return out
