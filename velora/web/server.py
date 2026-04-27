"""Веб-приложение Velora Sound."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import text as _sqltext
from sqlalchemy.exc import IntegrityError

import phonenumbers

from velora import auth as vauth
from velora.api import deezer, itunes
from velora.api.lyrics import get_lyrics
from velora.api.resolver import resolve_stream
from velora.config import Config
from velora.db import (
    ArtistPref, AuthSession, Dislike, Follow, HistoryEntry, ImageBlob, Like,
    LoginCode, PageVisit, Playlist, PlaylistItem, TasteSnapshot, User,
    VerifyAttempt, WallPost, db,
)
from velora import moderation as _mod
from velora import taste as _taste

# Токен Telegram-бота. Берём из env, иначе используем токен,
# полученный пользователем (бот @stsag_bot).
TG_BOT_TOKEN = Config.TG_BOT_TOKEN

# Кеш аватарок артистов (id → URL). Заполняется лениво при показе каталога.
_ARTIST_PIC_CACHE: dict[str, str] = {}
# Фоновой исполнитель для прогрева кэша аватарок — общий, чтобы не плодить
# по 8 потоков на каждый запрос и не держать DB-коннекты Flask-Login во время
# медленных HTTP-запросов в Deezer.
_ARTIST_PIC_BG = ThreadPoolExecutor(max_workers=4, thread_name_prefix="artpic")
_ARTIST_PIC_INFLIGHT: set[str] = set()


def _looks_like_artist_picture(url: str) -> bool:
    """True, если URL похож на нормальный аватар артиста (Deezer CDN)."""
    if not url or not isinstance(url, str):
        return False
    # Deezer CDN-картинки артиста:  https://...dzcdn.net/images/artist/...
    if "dzcdn.net/images/artist/" in url:
        return True
    # Apple/iTunes mzstatic — тоже считаем валидной.
    if "mzstatic.com" in url and "Music" in url:
        return True
    # Любой явный http(s) URL допустим как fallback.
    return url.startswith("http://") or url.startswith("https://")


def _fetch_artist_picture(aid: str) -> str:
    if not aid:
        return ""
    if aid in _ARTIST_PIC_CACHE:
        return _ARTIST_PIC_CACHE[aid]
    try:
        info = deezer._get(f"/artist/{aid}")
        url = info.get("picture_xl") or info.get("picture_big") or info.get("picture_medium") or ""
    except Exception:
        url = ""
    _ARTIST_PIC_CACHE[aid] = url
    return url


def _enrich_artist_pictures(items: list[dict], *, sync: bool = False, timeout: float = 3.0) -> None:
    """Подставляет аватарки артистов из кэша; для отсутствующих запускает
    фоновую загрузку (не блокируя текущий запрос). Со следующей загрузки
    каталога картинки уже будут готовы.

    Если sync=True — ожидаем фоновые задачи до timeout сек, чтобы при первом
    же открытии страницы каталога аватарки уже были видны.

    Работает только для source="deezer".
    """
    pending: list[tuple[dict, str, "object"]] = []  # (item, aid, future|None)
    for it in items:
        if (it.get("source") or "deezer") != "deezer":
            continue
        aid = str(it.get("id") or "")
        if not aid:
            continue
        cur = it.get("image") or ""
        # Если в кэше уже есть готовая ссылка — подставляем сразу.
        cached = _ARTIST_PIC_CACHE.get(aid)
        if cached:
            it["image"] = cached
            continue
        # Если картинка уже валидная аватарка артиста — не трогаем.
        if "/artist/" in cur and _looks_like_artist_picture(cur):
            continue
        # Иначе запускаем (фоновую/sync) загрузку.
        if aid in _ARTIST_PIC_INFLIGHT and not sync:
            continue
        _ARTIST_PIC_INFLIGHT.add(aid)
        def _bg(a: str = aid) -> None:
            try:
                _fetch_artist_picture(a)
            finally:
                _ARTIST_PIC_INFLIGHT.discard(a)
        try:
            fut = _ARTIST_PIC_BG.submit(_bg)
        except Exception:
            _ARTIST_PIC_INFLIGHT.discard(aid)
            fut = None
        if sync:
            pending.append((it, aid, fut))
    # В sync-режиме ждём готовности каждой задачи (всё равно параллельно).
    if sync and pending:
        import time as _t
        deadline = _t.time() + timeout
        for it, aid, fut in pending:
            remain = max(0.05, deadline - _t.time())
            try:
                if fut is not None:
                    fut.result(timeout=remain)
            except Exception:
                pass
            cached = _ARTIST_PIC_CACHE.get(aid)
            if cached:
                it["image"] = cached


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
    app.config["SQLALCHEMY_DATABASE_URI"] = Config.database_uri()
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 МБ для аватаров
    # Поднимаем пул соединений: дефолтных 5+10 не хватает при параллельных
    # запросах с медленными внешними HTTP-вызовами (Flask-Login на каждый
    # запрос дёргает load_user → DB-коннект и держит до teardown).
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_size": 20,
        "max_overflow": 40,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }

    db.init_app(app)

    # Cache-busting для статики: добавляем ?v=<short-hash mtime> к app.js/style.css.
    # Жинья-хелпер: {{ asset_v('app.js') }} → "1730102211".
    def _asset_version(name: str) -> str:
        try:
            p = Path(app.static_folder or "static") / name
            return str(int(p.stat().st_mtime))
        except Exception:
            return "0"

    @app.context_processor
    def _inject_asset_v():
        return {"asset_v": _asset_version}

    login_manager = LoginManager(app)
    login_manager.login_view = None

    @login_manager.user_loader
    def load_user(uid: str):
        return db.session.get(User, int(uid))

    @login_manager.unauthorized_handler
    def _unauth():
        return jsonify({"error": "auth required"}), 401

    with app.app_context():
        try:
            db.create_all()
            _migrate_sqlite(app)
            _enable_sqlite_wal(app)
        except Exception as exc:
            app.logger.warning(f"DB init skipped: {exc}")

    # Telegram poller — единственный экземпляр на процесс.
    _start_telegram_poller(app)
    _autoset_site_url(app)

    _register_routes(app)
    return app


# ------------------------------------------------------------ migration
def _migrate_sqlite(app: Flask) -> None:
    """Безопасный ADD COLUMN для уже существующих SQLite-баз.

    Добавляет недостающие колонки в users, чтобы не уронить старые БД
    при апгрейде схемы (новые поля phone / phone_verified / email_verified).
    """
    if not str(db.engine.url).startswith("sqlite"):
        return
    cols_to_add = [
        ("users", "phone", "VARCHAR(32)"),
        ("users", "phone_verified", "BOOLEAN DEFAULT 0"),
        ("users", "email_verified", "BOOLEAN DEFAULT 0"),
        ("users", "banner", "TEXT"),
        ("users", "is_private", "BOOLEAN DEFAULT 0"),
        ("users", "privacy", "TEXT DEFAULT '{}'"),
        ("users", "uid", "VARCHAR(32)"),
        ("users", "tg_id", "BIGINT"),
        ("users", "tg_username", "VARCHAR(64)"),
        ("users", "tg_first_name", "VARCHAR(120)"),
        ("users", "tg_photo_url", "TEXT"),
        ("users", "google_id", "VARCHAR(64)"),
        ("users", "vk_id", "BIGINT"),
        ("users", "dob", "DATE"),
        ("users", "wall_enabled", "BOOLEAN DEFAULT 1"),
        ("history", "from_view", "VARCHAR(32) DEFAULT 'other'"),
        ("history", "play_count", "INTEGER DEFAULT 1"),
        ("wall_posts", "image_url", "TEXT"),
        ("wall_posts", "status", "VARCHAR(16) DEFAULT 'published'"),
        ("wall_posts", "moderation_reason", "TEXT"),
        ("wall_posts", "expires_at", "DATETIME"),
    ]
    with db.engine.begin() as conn:
        for table, col, decl in cols_to_add:
            try:
                rows = conn.execute(_sqltext(f"PRAGMA table_info({table})")).fetchall()
                existing = {r[1] for r in rows}
                if col not in existing:
                    conn.execute(_sqltext(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))
                    app.logger.info("migrated: %s.%s added", table, col)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning("migration %s.%s skipped: %s", table, col, exc)


_TG_POLLER: vauth.TelegramLoginBot | None = None


def _enable_sqlite_wal(app: Flask) -> None:
    """SQLite по умолчанию даёт `database is locked` при параллельных
    запросах (например, импорт + загрузка плейлистов). WAL-режим и
    увеличенный busy_timeout снимают большинство этих гонок."""
    if not str(db.engine.url).startswith("sqlite"):
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(_sqltext("PRAGMA journal_mode=WAL"))
            conn.execute(_sqltext("PRAGMA synchronous=NORMAL"))
            conn.execute(_sqltext("PRAGMA busy_timeout=15000"))
        # Прагмы — per-connection, поэтому ставим event-listener,
        # чтобы каждое новое подключение получало busy_timeout.
        from sqlalchemy import event as _sa_event

        @_sa_event.listens_for(db.engine, "connect")
        def _on_connect(dbapi_conn, _rec):  # noqa: ANN001
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA busy_timeout=15000")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
            except Exception:
                pass
        app.logger.info("sqlite: WAL enabled, busy_timeout=15s")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("sqlite WAL setup skipped: %s", exc)


def _start_telegram_poller(app: Flask) -> None:
    global _TG_POLLER
    if _TG_POLLER is not None or not TG_BOT_TOKEN:
        return

    # На passenger/спринтхосте сервер запускается в нескольких worker-процессах.
    # Поллинг getUpdates допустим только в одном — иначе Telegram возвращает 409.
    # Берём эксклюзивный лок на файл; не получилось → молча выходим.
    lock_path = os.path.join(app.instance_path, "tg_poller.lock")
    try:
        os.makedirs(app.instance_path, exist_ok=True)
        lock_fh = open(lock_path, "a+")
        try:
            import msvcrt  # type: ignore
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl  # type: ignore
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            app.logger.info("[VELORA TG] poller lock busy — другой процесс уже опрашивает")
            try: lock_fh.close()
            except Exception: pass
            return
        # Держим lock открытым на всё время работы процесса.
        app._velora_tg_lock = lock_fh  # type: ignore[attr-defined]
    except Exception as exc:
        app.logger.warning("poller lock skipped: %s", exc)

    def _session_factory():
        # Возвращаем scoped-session из flask-sqlalchemy. Поток заходит
        # в app_context при операции коммита.
        ctx = app.app_context()
        ctx.push()
        sess = db.session
        # Подсунем remove() который снимет контекст.
        original_remove = getattr(sess, "remove", None)

        def _remove():
            try:
                if callable(original_remove):
                    original_remove()
            finally:
                try:
                    ctx.pop()
                except Exception:
                    pass
        sess.remove = _remove  # type: ignore[attr-defined]
        return sess

    poller = vauth.TelegramLoginBot(
        TG_BOT_TOKEN, _session_factory, LoginCode, User,
        verify_attempt_model=VerifyAttempt,
        admin_ids=Config.TG_ADMIN_IDS,
        site_url=Config.SITE_URL,
    )
    # Подгрузка ранее «заклеймленных» админов (instance/admins.txt: tg_id в строке).
    try:
        admins_path = os.path.join(app.instance_path, "admins.txt")
        if os.path.exists(admins_path):
            extra: list[int] = []
            with open(admins_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.lstrip("-").isdigit():
                        extra.append(int(s))
            if extra:
                poller._admin_ids = tuple(set(list(poller._admin_ids) + extra))
                app.logger.info("[VELORA TG] loaded %d persisted admin(s): %s",
                                len(extra), extra)
    except Exception as exc:
        app.logger.warning("admin list load failed: %s", exc)
    # Если админов нет — генерим одноразовый claim-токен и пишем в лог.
    if not poller._admin_ids:
        claim = secrets.token_urlsafe(8)
        poller._admin_claim_token = claim
        app.logger.warning(
            "\n" + "=" * 60 +
            "\n[VELORA] Админ Telegram ещё не настроен.\n"
            "Чтобы стать админом — напишите боту:\n"
            f"    /claim_admin {claim}\n"
            "Или задайте VELORA_TG_ADMIN_IDS=<ваш tg_id> в окружении.\n"
            + "=" * 60
        )
    poller._admins_path = os.path.join(app.instance_path, "admins.txt")  # type: ignore[attr-defined]
    try:
        poller.start()
        _TG_POLLER = poller
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Telegram poller start failed: %s", exc)


def _autoset_site_url(app: Flask) -> None:
    """Автоматически подхватить базовый URL из первого внешнего запроса."""
    @app.before_request
    def _capture_site_url():
        try:
            if _TG_POLLER and not _TG_POLLER._site_url:
                host = (request.host_url or "").rstrip("/")
                if host and "127.0.0.1" not in host and "localhost" not in host:
                    _TG_POLLER._site_url = host
                    app.logger.info("[VELORA TG] site_url auto-set: %s", host)
        except Exception:
            pass


# ------------------------------------------------------------ helpers
def _merge_unique(*lists, key=lambda t: (t.title.lower(), t.artist.lower())):
    seen = set()
    out = []
    for lst in lists:
        for item in lst:
            k = key(item)
            if k in seen:
                continue
            seen.add(k)
            out.append(item)
    return out


def _kids_filter_dicts(items: list[dict]) -> list[dict]:
    """Если включён режим для детей — отфильтровать explicit-треки.
    Заодно подменяет «зацензуренные» названия и неправильных исполнителей
    на оригинальные (для известных треков из словаря).
    """
    from velora.api.resolver import apply_pair_override
    out = items
    if current_user.is_authenticated and getattr(current_user, "kids_mode", False):
        out = [t for t in out if not t.get("explicit")]
    for t in out:
        title = t.get("title")
        if title:
            new_artist, new_title = apply_pair_override(t.get("artist", ""), title)
            t["title"] = new_title
            # Меняем artist только если pair-override реально дал нового артиста.
            if new_artist and new_artist != t.get("artist", ""):
                t["artist"] = new_artist
    return out


def _kids_check_track(t: dict) -> bool:
    """True если трек разрешён к воспроизведению."""
    if current_user.is_authenticated and getattr(current_user, "kids_mode", False):
        return not bool(t.get("explicit"))
    return True


# ---- Tune-фильтры волны (live-настройка под занятие/характер/настроение) ----

def _is_cyrillic_text(s: str) -> bool:
    if not s:
        return False
    cyr = sum(1 for c in s if "а" <= c.lower() <= "я" or c.lower() == "ё")
    lat = sum(1 for c in s if "a" <= c.lower() <= "z")
    return cyr >= max(2, lat)


def _apply_wave_tune(items: list[dict], occupy: str, char: str, mood: str, lang: str) -> list[dict]:
    """Применяет live-настройки волны без перезагрузки страницы.

    occupy: focus|workout|chill|drive|sleep|party
    char:   energetic|calm|melodic|rhythmic|dark|bright
    mood:   happy|sad|angry|romantic|nostalgic|mixed
    lang:   ru|en|any
    """
    if not items:
        return items
    out = list(items)

    # 1) Язык — отбираем по cyrillic-эвристике (только если запрошено явно).
    if lang == "ru":
        ru = [t for t in out if _is_cyrillic_text(t.get("title", "")) or _is_cyrillic_text(t.get("artist", ""))]
        if ru:
            other = [t for t in out if t not in ru]
            out = ru + other  # русские вперёд, остальное хвостом
    elif lang == "en":
        en = [t for t in out if not _is_cyrillic_text(t.get("title", "")) and not _is_cyrillic_text(t.get("artist", ""))]
        if en:
            ru = [t for t in out if t not in en]
            out = en + ru

    # 2) Длительность — для sleep/focus короткие/спокойные вперёд; для workout/party — длиннее/энергичные.
    def dur(t):
        try:
            return int(t.get("duration") or 0)
        except (TypeError, ValueError):
            return 0

    if occupy == "sleep":
        out.sort(key=lambda t: -dur(t))  # длинные треки лучше для сна
    elif occupy in ("workout", "party"):
        out.sort(key=lambda t: (dur(t) < 150, -dur(t)))  # короткие и динамичные хвостом
    if occupy == "focus":
        out.sort(key=lambda t: -dur(t))

    # 3) режим sleep/focus — предпочтение длинным трекам, explicit НЕ фильтруем.

    # 4) char/mood — слабые сигналы; пока используем как seed для шаффла.
    import random
    seed_str = f"{occupy}|{char}|{mood}|{lang}"
    if seed_str.strip("|"):
        rnd = random.Random(hash(seed_str) & 0xFFFFFFFF)
        # Лёгкое перемешивание внутри окон по 6, чтобы каждое сочетание давало уникальный порядок.
        chunks = [out[i:i+6] for i in range(0, len(out), 6)]
        for c in chunks:
            rnd.shuffle(c)
        out = [t for c in chunks for t in c]

    return out


_TRANSLIT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh",
    "з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o",
    "п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts",
    "ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
}


def _translit_ru_en(s: str) -> str:
    out = []
    for ch in s.lower():
        out.append(_TRANSLIT.get(ch, ch))
    return "".join(out)


# Обратный транслит EN→RU для запросов вроде "morgen", "madkid" → "морген", "мадкид".
# Порядок важен: длинные диграфы первыми.
_TRANSLIT_BACK = [
    ("shch", "щ"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"),
    ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
    ("sh", "ш"), ("sch", "щ"), ("ck", "к"), ("ph", "ф"),
    ("a","а"),("b","б"),("c","к"),("d","д"),("e","е"),("f","ф"),
    ("g","г"),("h","х"),("i","и"),("j","дж"),("k","к"),("l","л"),
    ("m","м"),("n","н"),("o","о"),("p","п"),("q","к"),("r","р"),
    ("s","с"),("t","т"),("u","у"),("v","в"),("w","в"),("x","кс"),
    ("y","й"),("z","з"),
]


def _translit_en_ru(s: str) -> str:
    s = (s or "").lower()
    if not any(c.isascii() and c.isalpha() for c in s):
        return s
    for k, v in _TRANSLIT_BACK:
        s = s.replace(k, v)
    return s


# Известные артисты, у которых русский транслит «не звучит» (drake → dreyk),
# поэтому пишем алиасы вручную: ru_lower → en_canonical.
# Используется в _expand_search_queries: подменяем подстроку в запросе и
# добавляем как ещё один вариант поиска.
_ARTIST_TRANSLIT_ALIASES = {
    # International / English
    "дрейк": "drake", "эминем": "eminem",
    "канье вест": "kanye west",
    "канйе": "kanye west", "пост малон": "post malone", "пост мэлоун": "post malone",
    "тейлор свифт": "taylor swift", "ариана гранде": "ariana grande",
    "бейонсе": "beyonce", "бейонс": "beyonce",
    "рианна": "rihanna", "леди гага": "lady gaga", "билли айлиш": "billie eilish",
    "билли элиш": "billie eilish", "бруно марс": "bruno mars",
    "джастин бибер": "justin bieber", "уикнд": "the weeknd", "виикнд": "the weeknd",
    "виикэнд": "the weeknd", "уикэнд": "the weeknd", "зе уикнд": "the weeknd",
    "трэвис скотт": "travis scott", "тревис скотт": "travis scott",
    "кендрик ламар": "kendrick lamar",
    "снуп догг": "snoop dogg",
    "доктор дре": "dr dre", "доктор дрэ": "dr dre", "д-р дре": "dr dre",
    "тупак": "2pac", "ту пак": "2pac", "2пак": "2pac",
    "бигги": "notorious big", "ноториус": "notorious big",
    "джей зи": "jay z", "джей-зи": "jay z",
    "ник минаж": "nicki minaj", "ники минаж": "nicki minaj",
    "карди би": "cardi b", "доджа кэт": "doja cat",
    "дуа липа": "dua lipa", "оливия родриго": "olivia rodrigo",
    "ливай рулз": "lily allen", "лил пип": "lil peep", "лил уэйн": "lil wayne",
    "лил уэйн": "lil wayne", "лил уэйн": "lil wayne", "лил нас икс": "lil nas x",
    "лил нас": "lil nas x", "лил скай": "lil skies", "лил юти": "lil uzi vert",
    "лил узи": "lil uzi vert", "лил беби": "lil baby", "лил дёрк": "lil durk",
    "лил дурк": "lil durk", "лил йэти": "lil yachty", "лил йяти": "lil yachty",
    "ой джей да джус": "oj da juice", "плэйбой карти": "playboi carti",
    "плейбой карти": "playboi carti", "карти": "playboi carti",
    "тэйк ёр пик": "take a pick",
    "оззи осборн": "ozzy osbourne", "квин": "queen", "битлз": "the beatles",
    "битлс": "the beatles", "пинк флойд": "pink floyd", "металлика": "metallica",
    "металика": "metallica", "линкин парк": "linkin park", "линкин": "linkin park",
    "грин дей": "green day", "коулдплей": "coldplay", "колдплей": "coldplay",
    "ред хот чили пепперс": "red hot chili peppers", "рхчп": "red hot chili peppers",
    "рамштайн": "rammstein", "раммштайн": "rammstein",
    "ту дор синема клаб": "two door cinema club", "30 секунд ту марс": "30 seconds to mars",
    "тридцать секунд до марса": "30 seconds to mars",
    "имэджин драгонс": "imagine dragons", "имажин драгонс": "imagine dragons",
    "ариана": "ariana grande", "стинг": "sting", "адель": "adele",
    "сэм смит": "sam smith", "эд ширан": "ed sheeran", "эд шиеран": "ed sheeran",
    "эд ширан": "ed sheeran", "сия": "sia", "халси": "halsey",
    "майкл джексон": "michael jackson", "элвис": "elvis presley",
    "элвис пресли": "elvis presley", "мадонна": "madonna",
    "лана дель рей": "lana del rey", "лана дел рей": "lana del rey",
    "ласт ниг ин париж": "last night in paris", "тейк зэт": "take that",
    "брунo марс": "bruno mars",
    # Russian / CIS — обычно ищется как есть, но добавим латиницу для тех,
    # у кого Deezer индексирует по EN.
    "макан": "macan", "мияги": "miyagi", "эндшпиль": "endspiel", "эндшпил": "endspiel",
    "моргенштерн": "morgenshtern", "морген": "morgenshtern",
    "хаски": "husky", "оксимирон": "oxxxymiron", "ст": "st",
    "скриптонит": "skryptonite", "скриптонит": "skryptonite",
    "фараон": "pharaoh", "тимати": "timati", "лсп": "lsp",
    "грибы": "griby", "элджей": "eldzhey", "монеточка": "monetochka",
    "ноггано": "noggano", "каста": "kasta", "ассаи": "assai",
    "хаски": "husky", "мот": "mot", "стас михайлов": "stas mikhailov",
    "макс корж": "max korzh", "иван дорн": "ivan dorn", "владимир пресняков": "vladimir presnyakov",
    "цой": "tsoi", "виктор цой": "viktor tsoi", "кино": "kino",
    "ддт": "ddt", "наутилус": "nautilus pompilius", "сплин": "splean",
    "земфира": "zemfira", "пугачёва": "pugacheva", "пугачева": "pugacheva",
    "киркоров": "kirkorov",
}


def _apply_artist_aliases(q: str) -> list[str]:
    """Возвращает список доп. вариантов запроса, где ru-имя артиста заменено
    на канонический EN-вариант. Подмена только по ГРАНИЦАМ СЛОВ — иначе
    «морген» внутри «моргенштерн» испортил бы результат.
    """
    if not q:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Сортируем по убыванию длины ключа: длинные алиасы матчатся первыми
    # (чтобы «пост малон» match-нулось целиком, а не по «пост»).
    items = sorted(_ARTIST_TRANSLIT_ALIASES.items(), key=lambda kv: -len(kv[0]))
    for ru, en in items:
        # \b в Python re с UNICODE-флагом (по умолчанию) корректно работает
        # с кириллицей: внутри слова не сработает.
        rx = re.compile(r"(?<!\w)" + re.escape(ru) + r"(?!\w)", re.IGNORECASE | re.UNICODE)
        if rx.search(q):
            alt = rx.sub(en, q).lower()
            alt = re.sub(r"\s+", " ", alt).strip()
            if alt and alt not in seen and alt != q.lower():
                seen.add(alt); out.append(alt)
    return out


def _expand_search_queries(q: str) -> list[str]:
    """Возвращает список вариантов запроса: оригинал + транслит туда/обратно
    + reverse-алиасы для зацензуренных треков (без дубликатов)."""
    q = (q or "").strip()
    if not q:
        return []
    variants = [q]
    # Reverse-алиасы: если юзер ищет «В этой траве», добавим «В этой оу е» —
    # потому что в каталоге Deezer оригинал лежит под зацензуренным названием.
    try:
        from velora.api.resolver import search_aliases
        for alt in search_aliases(q):
            variants.append(alt)
    except Exception:
        pass
    has_cyr = any("\u0400" <= c <= "\u04FF" for c in q)
    has_lat = any("a" <= c.lower() <= "z" for c in q)
    if has_cyr:
        # Сначала — словарь известных артистов (дрейк → drake), потому что
        # авто-транслит даёт «dreyk», что не находится в Deezer.
        # Алиасы вставляем В НАЧАЛО списка вариантов (но после оригинального q),
        # чтобы английские результаты «Drake» шли раньше треков «про Дрейка».
        aliases = _apply_artist_aliases(q)
        for alt in aliases:
            # Вставляем сразу после q (индекс 1), сохраняя относительный порядок.
            variants.insert(1, alt)
        # После алиасов — порядок: alt1, alt2, ..., q, ...
        # Хотим: alt1, q, alt2 → но проще: alt сначала, потом q, остальные сзади.
        if aliases:
            variants = aliases + [q] + [v for v in variants if v not in aliases and v != q]
        t = _translit_ru_en(q)
        if t and t != q.lower():
            variants.append(t)
    if has_lat and not has_cyr:
        t = _translit_en_ru(q)
        if t and t != q.lower():
            variants.append(t)
    # Дубликаты убираем сохраняя порядок
    seen = set(); out = []
    for v in variants:
        k = v.lower()
        if k in seen:
            continue
        seen.add(k); out.append(v)
    return out


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    base = _translit_ru_en(s or "").lower()
    base = _SLUG_RE.sub("-", base).strip("-")
    return base[:60] or "item"


def _make_unique_user_slug(name: str) -> str:
    base = _slugify(name)
    cand = base
    i = 2
    while db.session.query(User.id).filter_by(slug=cand).first():
        cand = f"{base}-{i}"
        i += 1
    return cand


# ---- privacy / public profile helpers --------------------------------
_DEFAULT_PRIVACY = {
    "show_bio": True,
    "show_location": True,
    "show_website": True,
    "show_avatar": True,
    "show_banner": True,
    "show_stats": True,
    "show_playlists": True,
    "show_follows": True,
    "show_dob": False,   # дата рождения по умолчанию скрыта
    "show_wall": True,   # показывать ли стену посторонним
}


def _parse_privacy(raw):
    """Возвращает dict с privacy-флагами с дефолтами."""
    out = dict(_DEFAULT_PRIVACY)
    try:
        if raw:
            j = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(j, dict):
                for k, v in j.items():
                    if k in _DEFAULT_PRIVACY:
                        out[k] = bool(v)
    except Exception:
        pass
    return out


def _sanitize_privacy(d: dict) -> dict:
    out = {}
    for k in _DEFAULT_PRIVACY:
        if k in d:
            out[k] = bool(d[k])
    return out


def _resolve_user(slug_or_username: str):
    s = (slug_or_username or "").strip()
    if not s:
        return None
    u = db.session.query(User).filter_by(slug=s).first()
    if u:
        return u
    return db.session.query(User).filter_by(username=s).first()


def _make_unique_pl_slug(name: str) -> str:
    base = _slugify(name)
    cand = base
    i = 2
    while db.session.query(Playlist.id).filter_by(slug=cand).first():
        cand = f"{base}-{i}"
        i += 1
    return cand


def _serialize_playlist(p: Playlist, include_items: bool = True) -> dict:
    body = {
        "id": p.id,
        "slug": p.slug or str(p.id),
        "name": p.name,
        "description": p.description or "",
        "cover": p.cover or "",
        "pinned": bool(p.pinned),
        "is_public": bool(p.is_public),
        "owner_id": p.user_id,
        "count": len(p.items),
        "duration": sum((it.duration or 0) for it in p.items),
        "updated_at": (p.updated_at or p.created_at).isoformat() if (p.updated_at or p.created_at) else None,
    }
    if include_items:
        body["items"] = [_serialize_pitem(it) for it in p.items]
    return body


def _serialize_pitem(it: PlaylistItem) -> dict:
    return {
        "id": it.track_id,
        "title": it.title,
        "artist": it.artist,
        "album": it.album,
        "cover_big": it.cover,
        "cover_small": it.cover,
        "duration": it.duration,
        "source": it.source,
        "explicit": bool(it.explicit),
        "position": it.position,
        "row_id": it.id,
    }


_DATA_URL_RE = re.compile(r"^data:image/(png|apng|jpe?g|gif|webp|avif|bmp|x-icon|vnd\.microsoft\.icon|tiff?|heic|heif|jxl);base64,([A-Za-z0-9+/=]+)$")


def _validate_image(data_url: str) -> str | None:
    """Проверка изображения. Принимает либо data:URL, либо ссылку /api/img/<id>.
    Возвращает строку либо None если невалидно."""
    if not data_url:
        return None
    s = str(data_url).strip()
    # Ссылка на загруженный файл — пропускаем как есть.
    if s.startswith("/api/img/"):
        return s[:255]
    if not _DATA_URL_RE.match(s):
        return None
    if len(s) > 6_000_000:  # ~4.5 МБ после декодирования
        return None
    return s


def _parse_imported_lines(text: str) -> list[tuple[str, str]]:
    """Извлечь пары (artist, title) из текстовых строк.

    Поддерживаемые форматы:
      Артист — Название
      Артист - Название
      Артист – Название
      Название - Артист (если первая часть короче — fallback)
    """
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # очистить ведущую нумерацию "1. " / "1) "
        line = re.sub(r"^\s*\d{1,3}[\.\)]\s*", "", line)
        # Поддерживаем разные тире и юникод-вариации.
        for sep in (" — ", " – ", " - ", " − ", " ‒ ", " ― ", "—", "–", " - "):
            if sep in line:
                a, t = line.split(sep, 1)
                a, t = a.strip(" \t-—–•*"), t.strip(" \t-—–•*")
                if a and t:
                    out.append((a, t))
                    break
        else:
            # одиночная строка без разделителя — считать всем целым "title".
            # Лимит снят: фильтрация количества — на стороне импорта.
            if line:
                out.append(("", line.strip(" \t-—–•*")))
    return out


def _extract_text_from_upload(filename: str, blob: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".docx"):
        try:
            import zipfile
            from xml.etree import ElementTree as ET

            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                with z.open("word/document.xml") as f:
                    tree = ET.parse(f)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paras = []
            for p in tree.iter(f"{{{ns['w']}}}p"):
                text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t"))
                paras.append(text)
            return "\n".join(paras)
        except Exception:
            return ""
    # txt / csv / md / прочее
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


# ------------------------------------------------------------ routes
def _register_routes(app: Flask) -> None:

    # =====================================================================
    # SESSION VALIDATION: если AuthSession удалили/отозвали → принудительный logout
    # =====================================================================
    _LAST_SEEN_THROTTLE: dict[str, datetime] = {}

    @app.before_request
    def _validate_auth_session():
        # Дёшево: только если пользователь залогинен и есть sid в куке.
        if not current_user.is_authenticated:
            return None
        sid = session.get("sid")
        # Старые сессии без sid (до миграции) — простим один раз и выпишем sid.
        if not sid:
            try:
                row = AuthSession(
                    user_id=current_user.id,
                    sid=vauth.gen_session_id(),
                    ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
                    user_agent=request.headers.get("User-Agent", "")[:1024],
                    platform="Старая сессия",
                    browser="",
                    provider="legacy",
                    created_at=datetime.utcnow(),
                    last_seen=datetime.utcnow(),
                )
                db.session.add(row)
                db.session.commit()
                session["sid"] = row.sid
            except Exception:
                db.session.rollback()
            return None
        try:
            row = (
                db.session.query(AuthSession)
                .filter_by(sid=sid, user_id=current_user.id)
                .first()
            )
        except Exception:
            return None
        if not row or row.revoked:
            # Сессия принудительно завершена — выкидываем.
            session.pop("sid", None)
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify({"error": "session_revoked"}), 401
            return None
        # Throttle обновления last_seen — раз в минуту.
        now = datetime.utcnow()
        last = _LAST_SEEN_THROTTLE.get(sid)
        if not last or (now - last).total_seconds() > 60:
            _LAST_SEEN_THROTTLE[sid] = now
            try:
                row.last_seen = now
                db.session.commit()
            except Exception:
                db.session.rollback()
        return None

    @app.route("/")
    def index():
        # При самом первом заходе на сайт (нет cookie) — отправляем
        # пользователя на welcome-страницу. После просмотра/идентификации
        # ставится cookie, и эта проверка больше не срабатывает.
        if not request.cookies.get("velora_seen_preview"):
            return Response(
                status=302,
                headers={"Location": url_for("preview_page")},
            )
        return render_template("index.html")

    @app.route("/pages-prew")
    def preview_page():
        """Welcome-страница: показывается один раз на устройство.

        По прямой ссылке доступна всегда — это «спец-ссылка» для повторного
        просмотра. Сама страница на старте шлёт фингерпринт устройства;
        если такой уже есть в базе — показываем «уже видел, идём дальше»,
        но не редиректим автоматом (пользователь сам нажмёт «Слушать»).
        """
        return render_template("preview.html")

    @app.route("/api/preview/fingerprint", methods=["POST"])
    def api_preview_fingerprint():
        """Принимает клиентский фингерпринт, считает HMAC-SHA256 с солью,
        кладёт в preview_views если ещё нет. Ставит cookie на 10 лет.

        Тело: { "fp": "<json-сериализованный набор сигналов>" }
        Ответ: { "ok": True, "is_new": <bool>, "fp_id": "<hex>" }
        """
        import hashlib
        import hmac as _hmac
        from velora.db import PreviewView

        data = request.get_json(silent=True) or {}
        raw_fp = str(data.get("fp") or "").strip()
        if not raw_fp or len(raw_fp) > 4096:
            return jsonify({"ok": False, "error": "bad_fp"}), 400
        salt = (Config.PREVIEW_FP_SALT or "velora").encode("utf-8")
        fp_hash = _hmac.new(salt, raw_fp.encode("utf-8"), hashlib.sha256).hexdigest()

        is_new = False
        try:
            row = db.session.query(PreviewView).filter_by(fp_hash=fp_hash).first()
            if not row:
                is_new = True
                row = PreviewView(
                    fp_hash=fp_hash,
                    ip=_client_ip()[:64],
                    user_agent=(request.headers.get("User-Agent") or "")[:1024],
                )
                db.session.add(row)
                db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("preview fp save failed: %s", exc)

        resp = jsonify({"ok": True, "is_new": is_new, "fp_id": fp_hash[:12]})
        # Cookie на 10 лет — этого достаточно чтобы welcome больше не выскакивала.
        resp.set_cookie(
            "velora_seen_preview", "1",
            max_age=60 * 60 * 24 * 365 * 10,
            httponly=False, samesite="Lax",
        )
        return resp

    # Service Worker должен лежать в корне, чтобы scope покрывал весь сайт.
    @app.route("/sw.js")
    def sw_js():
        resp = send_from_directory(app.static_folder, "sw.js")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # Публичные ссылки на плейлисты — рендерим SPA-каркас, клиент возьмёт slug из URL
    # и откроет нужную вьюшку через /api/p/<slug>.
    @app.route("/p/<slug>")
    def public_playlist_page(slug: str):  # noqa: ARG001
        return render_template("index.html")

    @app.route("/u/<slug>")
    def public_user_page(slug: str):  # noqa: ARG001
        # SPA сама подтянет данные через /api/u/<slug>.
        return render_template("index.html")

    @app.errorhandler(404)
    def _not_found(_e):
        # Для XHR/JSON отдаём чистый JSON, для HTML — SPA-страницу 404 (внутри SPA
        # сам клиент покажет окно «Страница не найдена»).
        wants_json = (
            request.path.startswith("/api/")
            or "application/json" in (request.headers.get("Accept") or "")
        )
        if wants_json:
            return jsonify({"error": "not_found", "path": request.path}), 404
        return render_template("index.html"), 404

    # ----------- AUTH ----------------------------------------------------
    @app.route("/api/me")
    def api_me():
        if not current_user.is_authenticated:
            return jsonify({"authenticated": False})
        try:
            settings = json.loads(current_user.settings or "{}")
        except Exception:
            settings = {}
        return jsonify({
            "authenticated": True,
            "id": current_user.id,
            "username": current_user.username,
            "slug": current_user.slug or current_user.username,
            "email": current_user.email,
            "phone": getattr(current_user, "phone", None),
            "phone_verified": bool(getattr(current_user, "phone_verified", False)),
            "email_verified": bool(getattr(current_user, "email_verified", False)),
            "display_name": current_user.display_name or current_user.username,
            "bio": current_user.bio or "",
            "avatar": current_user.avatar or "",
            "cover": current_user.cover or "",
            "banner": current_user.banner or "",
            "is_private": bool(getattr(current_user, "is_private", False)),
            "privacy": _parse_privacy(getattr(current_user, "privacy", None)),
            "kids_mode": bool(current_user.kids_mode),
            "dob": current_user.dob.isoformat() if getattr(current_user, "dob", None) else "",
            "age": _calc_age(getattr(current_user, "dob", None)),
            "kids_mode_locked": bool(
                getattr(current_user, "dob", None)
                and (_calc_age(current_user.dob) or 99) < 18
            ),
            "wall_enabled": bool(getattr(current_user, "wall_enabled", True)),
            "location": current_user.location or "",
            "website": current_user.website or "",
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
            "settings": settings,
            # Новые поля Telegram-only регистрации.
            "uid": getattr(current_user, "uid", None) or "",
            "tg_id": getattr(current_user, "tg_id", None),
            "tg_username": getattr(current_user, "tg_username", None) or "",
            "tg_linked": bool(getattr(current_user, "tg_id", None)),
            "is_admin": _is_admin_user(current_user),
            "is_helper": _is_helper_user(current_user),
            "role": _user_role(current_user),
        })

    # ---- Admin / Helper -------------------------------------------------
    # Помощники хранятся отдельным файлом instance/helpers.txt (по аналогии
    # с admins.txt: tg_id в строке). Помощник видит предложки и может их
    # помечать как сделанные / отвечать. Назначать новых помощников может
    # только админ. Эта схема позволяет не добавлять колонку в БД и работает
    # на любом окружении (sqlite/mysql).
    _HELPERS_PATH = os.path.join(app.instance_path, "helpers.txt")

    def _read_id_file(path: str) -> set[int]:
        try:
            if not os.path.exists(path):
                return set()
            with open(path, "r", encoding="utf-8") as fh:
                out: set[int] = set()
                for line in fh:
                    s = line.strip()
                    if s.isdigit():
                        out.add(int(s))
                return out
        except Exception:
            return set()

    def _write_id_file(path: str, ids: set[int]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for x in sorted(ids):
                fh.write(f"{x}\n")
        os.replace(tmp, path)

    def _admin_tg_ids() -> set[int]:
        ids = set()
        if _TG_POLLER is not None:
            ids.update(int(x) for x in (_TG_POLLER._admin_ids or ()))
        ids.update(int(x) for x in (Config.TG_ADMIN_IDS or ()))
        ids.update(_read_id_file(os.path.join(app.instance_path, "admins.txt")))
        return ids

    def _helper_tg_ids() -> set[int]:
        return _read_id_file(_HELPERS_PATH)

    def _is_admin_user(user) -> bool:
        try:
            tg = int(getattr(user, "tg_id", 0) or 0)
        except Exception:
            return False
        return tg != 0 and tg in _admin_tg_ids()

    def _is_helper_user(user) -> bool:
        try:
            tg = int(getattr(user, "tg_id", 0) or 0)
        except Exception:
            return False
        if tg == 0:
            return False
        return tg in _helper_tg_ids() or tg in _admin_tg_ids()

    def _user_role(user) -> str:
        if _is_admin_user(user):
            return "admin"
        if _is_helper_user(user):
            return "helper"
        return "user"

    def _require_admin():
        if not current_user.is_authenticated:
            return jsonify(ok=False, error="auth"), 401
        if not _is_admin_user(current_user):
            return jsonify(ok=False, error="forbidden"), 403
        return None

    def _require_admin_or_helper():
        if not current_user.is_authenticated:
            return jsonify(ok=False, error="auth"), 401
        if not _is_helper_user(current_user):
            return jsonify(ok=False, error="forbidden"), 403
        return None

    @app.route("/api/admin/whoami")
    def api_admin_whoami():
        if not current_user.is_authenticated:
            return jsonify(authenticated=False, role="user")
        return jsonify(
            authenticated=True,
            role=_user_role(current_user),
            is_admin=_is_admin_user(current_user),
            is_helper=_is_helper_user(current_user),
            tg_id=getattr(current_user, "tg_id", None),
        )

    # ---- Suggestions (помощник + админ) ---------------------------------
    @app.route("/api/admin/suggestions")
    def api_admin_suggestions():
        gate = _require_admin_or_helper()
        if gate is not None:
            return gate
        only_open = (request.args.get("status") or "open") != "all"
        try:
            q = db.session.query(VerifyAttempt).filter_by(kind="suggestion")
            if only_open:
                q = q.filter_by(verified=False)
            rows = q.order_by(VerifyAttempt.id.desc()).limit(200).all()
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
        out = []
        for r in rows:
            try:
                payload = json.loads(r.extra or "{}") or {}
            except Exception:
                payload = {}
            out.append({
                "id": r.id,
                "from_tg_id": payload.get("tg_id") or payload.get("from_tg_id"),
                "from_tg_username": payload.get("tg_username") or payload.get("from_tg_username") or "",
                "from_first_name": payload.get("tg_first_name") or "",
                "text": payload.get("text") or "",
                "done": bool(r.verified),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return jsonify(ok=True, items=out)

    @app.route("/api/admin/suggestions/<int:sid>/close", methods=["POST"])
    def api_admin_suggestion_close(sid: int):
        gate = _require_admin_or_helper()
        if gate is not None:
            return gate
        row = db.session.query(VerifyAttempt).filter_by(
            id=sid, kind="suggestion").first()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        row.verified = True
        db.session.commit()
        return jsonify(ok=True)

    @app.route("/api/admin/suggestions/<int:sid>/reply", methods=["POST"])
    def api_admin_suggestion_reply(sid: int):
        gate = _require_admin_or_helper()
        if gate is not None:
            return gate
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
        if not text:
            return jsonify(ok=False, error="empty"), 400
        row = db.session.query(VerifyAttempt).filter_by(
            id=sid, kind="suggestion").first()
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        try:
            payload = json.loads(row.extra or "{}") or {}
        except Exception:
            payload = {}
        chat_id = payload.get("chat_id") or payload.get("tg_id")
        if not chat_id or _TG_POLLER is None:
            return jsonify(ok=False, error="no_chat_or_bot"), 400
        try:
            _TG_POLLER.send_message(
                int(chat_id),
                f"💬 <b>Ответ на вашу предложку #{sid}</b>\n\n{text}",
                parse_mode="HTML",
            )
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True)

    # ---- Helpers (только админ) -----------------------------------------
    @app.route("/api/admin/helpers")
    def api_admin_helpers_list():
        gate = _require_admin()
        if gate is not None:
            return gate
        ids = sorted(_helper_tg_ids())
        # дополним username из БД, если пользователь привязал TG
        users = (db.session.query(User)
                 .filter(User.tg_id.in_(ids)).all()) if ids else []
        by_tg = {int(u.tg_id): u for u in users}
        out = []
        for tg in ids:
            u = by_tg.get(tg)
            out.append({
                "tg_id": tg,
                "username": u.username if u else None,
                "display_name": (u.display_name or u.username) if u else None,
                "tg_username": (u.tg_username or "") if u else "",
            })
        return jsonify(ok=True, items=out)

    @app.route("/api/admin/helpers", methods=["POST"])
    def api_admin_helpers_add():
        gate = _require_admin()
        if gate is not None:
            return gate
        data = request.get_json(silent=True) or {}
        raw = str(data.get("tg_id") or data.get("username") or "").strip()
        if not raw:
            return jsonify(ok=False, error="empty"), 400
        tg_id = None
        if raw.isdigit():
            tg_id = int(raw)
        else:
            # Поиск по username / tg_username
            uname = raw.lstrip("@")
            u = (db.session.query(User)
                 .filter((User.username == uname) | (User.tg_username == uname))
                 .first())
            if u and u.tg_id:
                tg_id = int(u.tg_id)
        if not tg_id:
            return jsonify(ok=False, error="user_not_found"), 404
        ids = _helper_tg_ids()
        ids.add(tg_id)
        try:
            _write_id_file(_HELPERS_PATH, ids)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
        # уведомим в Telegram
        if _TG_POLLER is not None:
            try:
                _TG_POLLER.send_message(
                    tg_id,
                    "🛡 Вам выдана роль <b>помощника</b>.\n"
                    "Теперь вам доступна страница админки на сайте: "
                    "вы можете отвечать на предложки и закрывать их.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return jsonify(ok=True, tg_id=tg_id)

    @app.route("/api/admin/helpers/<int:tg_id>", methods=["DELETE"])
    def api_admin_helpers_remove(tg_id: int):
        gate = _require_admin()
        if gate is not None:
            return gate
        ids = _helper_tg_ids()
        if tg_id not in ids:
            return jsonify(ok=False, error="not_found"), 404
        ids.discard(tg_id)
        try:
            _write_id_file(_HELPERS_PATH, ids)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True)

    # ---- Поиск пользователей (админ) ------------------------------------
    @app.route("/api/admin/users")
    def api_admin_users():
        gate = _require_admin()
        if gate is not None:
            return gate
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify(ok=True, items=[])
        like = f"%{q}%"
        try:
            rows = (db.session.query(User)
                    .filter((User.username.ilike(like))
                            | (User.display_name.ilike(like))
                            | (User.tg_username.ilike(like)))
                    .order_by(User.id.desc()).limit(30).all())
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True, items=[{
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name or u.username,
            "tg_id": u.tg_id,
            "tg_username": u.tg_username or "",
            "role": _user_role(u),
        } for u in rows])

    # Страница SPA-админки. Тот же index.html, фронт сам откроет нужный экран.
    @app.route("/admin")
    def page_admin():
        return render_template("index.html")

    # ---- TG bot info -----------------------------------------------------
    @app.route("/api/auth/tg/bot")
    def api_tg_bot_info():
        _tg_username = os.environ.get("VELORA_TG_BOT_USERNAME", "") or Config.TG_BOT_USERNAME
        username = (_TG_POLLER.bot_username if _TG_POLLER else "") or _tg_username
        return jsonify({
            "username": username,
            "available": bool(username),
            "deep_link": f"https://t.me/{username}" if username else "",
        })

    # Авто-логин по индивидуальной ссылке из Telegram-бота (/site).
    # Бот создаёт VerifyAttempt(kind="tg_autologin"), пользователь жмёт ссылку
    # → мы валидируем токен, помечаем использованным, логиним и редиректим.
    @app.route("/auth/tg/auto")
    def auth_tg_autologin():
        token = (request.args.get("t") or "").strip()
        if not token or len(token) > 64:
            return redirect("/?auth_err=bad_token")
        row = (db.session.query(VerifyAttempt)
               .filter_by(kind="tg_autologin", target=token).first())
        if not row:
            return redirect("/?auth_err=not_found")
        if row.verified:
            return redirect("/?auth_err=used")
        if row.expires_at and row.expires_at < datetime.utcnow():
            return redirect("/?auth_err=expired")
        try:
            extra = json.loads(row.extra or "{}") or {}
        except Exception:
            extra = {}
        uid_ = extra.get("user_id")
        if not uid_:
            return redirect("/?auth_err=corrupt")
        u = db.session.get(User, int(uid_))
        if not u:
            return redirect("/?auth_err=user_gone")
        # Однократное использование.
        row.verified = True
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        _establish_session(u, provider="telegram")
        return redirect("/")

    # ---- TG social-button: intent (per-click уникальный токен) ----------
    # Браузер просит токен → редирект на t.me/<bot>?start=intent_<token>.
    # Бот, получая /start intent_<token>, либо сразу авто-логинит привязанного
    # юзера (и помечает intent verified+user_id), либо проводит регистрацию.
    # Браузер параллельно пуллит /api/auth/tg/intent/poll и логинится.
    @app.route("/api/auth/tg/intent", methods=["POST"])
    def api_tg_intent():
        _tg_username = os.environ.get("VELORA_TG_BOT_USERNAME", "") or Config.TG_BOT_USERNAME
        bot_username = (_TG_POLLER.bot_username if _TG_POLLER else "") or _tg_username
        if not bot_username:
            return jsonify({"error": "no_bot", "message": "Telegram-бот не настроен"}), 503
        token = secrets.token_urlsafe(18)
        try:
            row = VerifyAttempt(
                kind="tg_intent",
                target=token,
                phone_normalized="",
                extra=json.dumps({"ip": _client_ip()[:64]}, ensure_ascii=False),
                expires_at=datetime.utcnow() + timedelta(minutes=15),
                verified=False,
            )
            db.session.add(row)
            db.session.commit()
        except Exception as exc:
            log.exception("tg_intent create failed: %s", exc)
            db.session.rollback()
            return jsonify({"error": "db", "message": "Не удалось создать ссылку"}), 500
        # Используем param `intent_<token>` — `/start intent_<token>`.
        return jsonify({
            "token": token,
            "deep_link": f"https://t.me/{bot_username}?start=intent_{token}",
            "expires_in": 900,
        })

    @app.route("/api/auth/tg/intent/poll")
    def api_tg_intent_poll():
        token = (request.args.get("t") or "").strip()
        if not token:
            return jsonify({"error": "bad_token"}), 400
        row = (db.session.query(VerifyAttempt)
               .filter_by(kind="tg_intent", target=token).first())
        if not row:
            return jsonify({"status": "not_found"}), 404
        if row.expires_at and row.expires_at < datetime.utcnow():
            return jsonify({"status": "expired"}), 410
        if not row.verified:
            return jsonify({"status": "pending"})
        try:
            extra = json.loads(row.extra or "{}") or {}
        except Exception:
            extra = {}
        uid_ = extra.get("user_id")
        if not uid_:
            return jsonify({"status": "pending"})
        u = db.session.get(User, int(uid_))
        if not u:
            return jsonify({"status": "user_gone"}), 404
        # Однократно: удаляем токен после успеха.
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
        _establish_session(u, provider="telegram")
        return jsonify({"status": "ok", "id": u.id, "username": u.username})

    # ---- Telegram Login Widget (telegram.org/js/telegram-widget.js) -----
    # Браузер открывает официальный виджет Telegram, после Allow он
    # присылает нам подписанный payload (id, first_name, ..., hash).
    # Мы валидируем HMAC-SHA256 секретом SHA256(bot_token) и логиним юзера.
    @app.route("/api/auth/tg/widget", methods=["POST"])
    def api_tg_widget():
        import hmac as _hmac
        token = (Config.TG_BOT_TOKEN or "").strip()
        if not token:
            return jsonify({"error": "no_bot",
                            "message": "Telegram-бот не настроен"}), 503
        data = request.get_json(silent=True) or {}
        received_hash = str(data.get("hash") or "").strip().lower()
        if not received_hash or "id" not in data or "auth_date" not in data:
            return jsonify({"error": "bad_payload"}), 400
        # Поля для check-string — все, кроме hash, без пустых значений.
        fields = {str(k): str(v) for k, v in data.items()
                  if k != "hash" and v not in (None, "")}
        try:
            auth_date = int(fields["auth_date"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "bad_payload"}), 400
        # Не доверяем подписям старше суток.
        now_ts = int(datetime.utcnow().timestamp())
        if abs(now_ts - auth_date) > 86400:
            return jsonify({"error": "expired",
                            "message": "Сессия Telegram устарела"}), 410
        data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))
        secret_key = hashlib.sha256(token.encode("utf-8")).digest()
        expected = _hmac.new(secret_key, data_check.encode("utf-8"),
                             hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, received_hash):
            app.logger.warning("tg_widget bad signature")
            return jsonify({"error": "bad_signature"}), 403
        try:
            tg_id = int(fields["id"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "bad_payload"}), 400

        tg_username = (fields.get("username") or "").strip() or None
        tg_first = (fields.get("first_name") or "").strip() or None
        tg_last = (fields.get("last_name") or "").strip() or None
        tg_photo = (fields.get("photo_url") or "").strip() or None

        u = db.session.query(User).filter_by(tg_id=tg_id).first()
        is_new = False
        if not u:
            is_new = True
            uname = _make_unique_username_from_tg(tg_username, tg_first, tg_id)
            display = " ".join(x for x in (tg_first, tg_last) if x) \
                or tg_username or uname
            u = User(
                username=uname,
                display_name=display,
                tg_id=tg_id,
                tg_username=tg_username,
                tg_first_name=tg_first,
                tg_photo_url=tg_photo,
                avatar=tg_photo or None,
                slug=_make_unique_user_slug(uname),
                uid=secrets.token_hex(6),
                password_hash=f"tg:{secrets.token_hex(16)}",
            )
            db.session.add(u)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return jsonify({"error": "race",
                                "message": "Попробуйте ещё раз"}), 409
        else:
            u.tg_username = tg_username or u.tg_username
            u.tg_first_name = tg_first or u.tg_first_name
            if tg_photo:
                u.tg_photo_url = tg_photo
                if not u.avatar:
                    u.avatar = tg_photo
            if not u.uid:
                u.uid = secrets.token_hex(6)
            db.session.commit()

        _establish_session(u, provider="telegram")
        return jsonify({
            "ok": True,
            "id": u.id,
            "uid": u.uid,
            "username": u.username,
            "is_new": is_new,
        })


    # которого больше нет. Возвращаем пустой список для обратной совместимости.
    @app.route("/api/auth/countries")
    def api_countries():
        return jsonify([])

    # =====================================================================
    # TELEGRAM-only авторизация
    # =====================================================================
    # Гонимый ниже helper выдёргивает «человеческие» имена User-Agent.
    def _ua_summary(ua: str) -> tuple[str, str]:
        """Грубо разбирает UA → (platform, browser)."""
        ua = ua or ""
        u = ua.lower()
        plat = "Неизвестно"
        if "windows" in u:
            plat = "Windows"
        elif "android" in u:
            plat = "Android"
        elif "iphone" in u or "ipad" in u or "ipod" in u:
            plat = "iOS"
        elif "mac os x" in u or "macintosh" in u:
            plat = "macOS"
        elif "linux" in u:
            plat = "Linux"
        elif "cros" in u:
            plat = "ChromeOS"
        br = "Браузер"
        m = None
        for name, key in (("Edge", "edg/"), ("YaBrowser", "yabrowser/"), ("OPR", "opr/"),
                         ("Chrome", "chrome/"), ("Firefox", "firefox/"),
                         ("Safari", "version/")):
            i = u.find(key)
            if i >= 0:
                rest = u[i+len(key):]
                ver = rest.split(" ", 1)[0].split(";", 1)[0].split(".")[0]
                m = f"{name} {ver}"
                break
        if m:
            br = m
        return plat, br

    def _client_ip() -> str:
        # Уважительно к прокси (Cloudflare, Nginx).
        for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
            v = request.headers.get(h)
            if v:
                return v.split(",")[0].strip()
        return request.remote_addr or ""

    def _establish_session(user: User, provider: str = "telegram") -> AuthSession:
        """Создаёт строку AuthSession и кладёт sid в Flask cookie."""
        sid = vauth.gen_session_id()
        ua = request.headers.get("User-Agent", "")
        plat, br = _ua_summary(ua)
        ip = _client_ip()
        row = AuthSession(
            user_id=user.id,
            sid=sid,
            ip=ip[:64],
            user_agent=ua[:1024],
            platform=plat,
            browser=br,
            provider=provider,
            geo=None,
            created_at=datetime.utcnow(),
            last_seen=datetime.utcnow(),
            revoked=False,
        )
        db.session.add(row)
        db.session.commit()
        login_user(user, remember=True)
        session.permanent = True
        session["sid"] = sid
        return row

    def _make_unique_username_from_tg(tg_username: str | None, tg_first: str | None,
                                      tg_id: int) -> str:
        base = (tg_username or tg_first or f"tg{tg_id}").strip()
        base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE)
        base = base[:60].strip("_") or f"tg{tg_id}"
        cand = base
        i = 1
        while db.session.query(User).filter_by(username=cand).first() is not None:
            i += 1
            cand = f"{base}_{i}"
            if i > 999:
                cand = f"{base}_{secrets.token_hex(3)}"
                break
        return cand

    # ---- username & dob helpers -------------------------------------
    _USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{3,32}$")

    def _validate_username(raw: str, exclude_user_id: int | None = None) -> tuple[str | None, str | None]:
        """Возвращает (нормализованный_ник, None) или (None, error_msg)."""
        s = (raw or "").strip().lstrip("@")
        if not s:
            return None, "Имя пользователя не может быть пустым"
        if not _USERNAME_RE.match(s):
            return None, "Только латиница, цифры, _ . - (3–32 символа)"
        # Защищённые/служебные ники
        if s.lower() in {"me", "admin", "api", "static", "root", "support", "velora"}:
            return None, "Это имя зарезервировано"
        q = db.session.query(User).filter(db.func.lower(User.username) == s.lower())
        if exclude_user_id:
            q = q.filter(User.id != exclude_user_id)
        if q.first() is not None:
            return None, "Это имя уже занято"
        return s, None

    def _parse_dob(raw) -> tuple[object, str | None]:
        """raw: 'YYYY-MM-DD' или ''. Возвращает (date|None, error|None).

        Пустая строка == очистить (None, None).
        """
        if raw in (None, "", False):
            return None, None
        try:
            from datetime import date as _date
            y, m, d = str(raw).split("-")
            d_ = _date(int(y), int(m), int(d))
            today = _date.today()
            if d_ > today:
                return None, "Дата рождения в будущем"
            if d_.year < 1900:
                return None, "Дата рождения слишком ранняя"
            return d_, None
        except Exception:
            return None, "Неверный формат даты (ожидается YYYY-MM-DD)"

    def _calc_age(d) -> int | None:
        if not d:
            return None
        try:
            from datetime import date as _date
            today = _date.today()
            years = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
            return max(0, years)
        except Exception:
            return None

    # ---- OAuth helpers (Google) ---------------------------------------
    def _oauth_client_id(provider: str) -> str:
        if provider == "google":
            return os.environ.get("VELORA_OAUTH_GOOGLE_CLIENT_ID", "") or Config.OAUTH_GOOGLE_CLIENT_ID or ""
        return ""

    def _oauth_client_secret(provider: str) -> str:
        if provider == "google":
            return os.environ.get("VELORA_OAUTH_GOOGLE_CLIENT_SECRET", "") or Config.OAUTH_GOOGLE_CLIENT_SECRET or ""
        return ""

    def _oauth_make_unique_username(base: str) -> str:
        base = re.sub(r"[^\w.\-]+", "_", (base or "user").strip(), flags=re.UNICODE)
        base = base[:60].strip("_") or "user"
        cand = base
        i = 1
        while db.session.query(User).filter_by(username=cand).first() is not None:
            i += 1
            cand = f"{base}_{i}"
            if i > 999:
                cand = f"{base}_{secrets.token_hex(3)}"
                break
        return cand

    def _oauth_find_or_create_user(*, provider: str, ext_id: str, email: str | None,
                                   name: str | None, avatar: str | None) -> User:
        """Находит юзера по google_id → email; иначе создаёт нового."""
        u: User | None = None
        if provider == "google":
            u = db.session.query(User).filter_by(google_id=ext_id).first()
        # Привязка по email — если такой юзер уже есть.
        if not u and email:
            u = db.session.query(User).filter(db.func.lower(User.email) == email.lower()).first()
            if u and provider == "google" and not u.google_id:
                u.google_id = ext_id
        if u:
            if avatar and not u.avatar:
                u.avatar = avatar
            if name and not u.display_name:
                u.display_name = name
            if not u.uid:
                u.uid = secrets.token_hex(6)
            db.session.commit()
            return u
        # Создаём нового пользователя.
        base_name = (email.split("@")[0] if email else None) or name or f"{provider}_{ext_id}"
        uname = _oauth_make_unique_username(base_name)
        u = User(
            username=uname,
            display_name=name or uname,
            email=email,
            email_verified=bool(email),
            avatar=avatar,
            slug=_make_unique_user_slug(uname),
            uid=secrets.token_hex(6),
            password_hash=f"{provider}:{secrets.token_hex(16)}",
        )
        if provider == "google":
            u.google_id = ext_id
        db.session.add(u)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            u2 = db.session.query(User).filter_by(google_id=ext_id).first()
            if not u2 and email:
                u2 = db.session.query(User).filter(db.func.lower(User.email) == email.lower()).first()
            if not u2:
                raise
            return u2
        return u

    def _oauth_success_page() -> str:
        """Маленькая HTML-страница, закрывающая окно и возвращающая на главную."""
        return (
            "<!doctype html><meta charset='utf-8'><title>Готово</title>"
            "<style>body{background:#0d0d10;color:#eee;font-family:system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            ".box{text-align:center;max-width:420px;padding:24px}"
            "h1{font-size:20px;margin:0 0 12px} p{opacity:.7;margin:0 0 18px}"
            "a{color:#7c5cff}</style>"
            "<div class='box'><h1>Вход выполнен</h1>"
            "<p>Можно закрыть это окно и вернуться на сайт.</p>"
            "<a href='/'>Перейти на главную</a></div>"
            "<script>setTimeout(()=>{try{window.opener&&window.opener.location.reload();"
            "window.close();}catch(e){}location.href='/';},1200);</script>"
        )

    def _oauth_error_page(msg: str) -> tuple[str, int]:
        safe = (msg or "Неизвестная ошибка").replace("<", "&lt;").replace(">", "&gt;")
        return (
            "<!doctype html><meta charset='utf-8'><title>Ошибка входа</title>"
            "<style>body{background:#0d0d10;color:#eee;font-family:system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            ".box{text-align:center;max-width:480px;padding:24px}"
            "h1{font-size:20px;margin:0 0 12px;color:#ff6b6b} p{opacity:.85;margin:0 0 18px}"
            "a{color:#7c5cff}</style>"
            f"<div class='box'><h1>Не удалось войти</h1><p>{safe}</p>"
            "<a href='/'>Вернуться на главную</a></div>",
            400,
        )

    @app.route("/api/auth/tg/code", methods=["POST"])
    def api_tg_code():
        """Принимает 6-значный код, выданный ботом, и логинит/создаёт пользователя."""
        data = request.get_json(silent=True) or {}
        code = re.sub(r"\D", "", str(data.get("code") or ""))
        if len(code) != vauth.CODE_LEN:
            return jsonify({"error": "invalid_code", "message": "Введите 6-значный код"}), 400

        row = (
            db.session.query(LoginCode)
            .filter_by(code=code, used=False)
            .order_by(LoginCode.id.desc())
            .first()
        )
        if not row:
            return jsonify({"error": "not_found", "message": "Неверный код"}), 404
        if row.expires_at and row.expires_at < datetime.utcnow():
            return jsonify({"error": "expired", "message": "Код истёк, запросите новый"}), 410

        # Помечаем код использованным.
        row.used = True
        row.used_at = datetime.utcnow()
        row.used_ip = _client_ip()[:64]

        # Находим/создаём пользователя по tg_id.
        u = db.session.query(User).filter_by(tg_id=row.tg_id).first()
        is_new = False
        if not u:
            is_new = True
            uname = _make_unique_username_from_tg(row.tg_username, row.tg_first_name, row.tg_id)
            u = User(
                username=uname,
                display_name=row.tg_first_name or row.tg_username or uname,
                tg_id=row.tg_id,
                tg_username=row.tg_username,
                tg_first_name=row.tg_first_name,
                tg_photo_url=row.tg_photo_url,
                avatar=row.tg_photo_url or None,
                slug=_make_unique_user_slug(uname),
                uid=secrets.token_hex(6),
                # Старые БД могут иметь NOT NULL на password_hash → ставим заглушку.
                password_hash=f"tg:{secrets.token_hex(16)}",
            )
            db.session.add(u)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return jsonify({"error": "race", "message": "Попробуйте ещё раз"}), 409
        else:
            # Обновляем привязанные TG-данные на актуальные.
            u.tg_username = row.tg_username or u.tg_username
            u.tg_first_name = row.tg_first_name or u.tg_first_name
            if row.tg_photo_url:
                u.tg_photo_url = row.tg_photo_url
            if not u.uid:
                u.uid = secrets.token_hex(6)
            db.session.commit()

        _establish_session(u, provider="telegram")
        return jsonify({
            "ok": True,
            "id": u.id,
            "uid": u.uid,
            "username": u.username,
            "is_new": is_new,
        })

    # ---- PHONE auth (через TG share-contact) ---------------------------
    def _normalize_phone(raw: str) -> dict | None:
        """Нормализует ввод в E.164 + извлекает страну/код страны/национальный.

        Возвращает dict {e164, country, dial_code, national} или None.
        Чинит баги типа «+7+7999...»: сначала чистим всё кроме цифр и
        ведущего «+», потом парсим.
        """
        if not raw:
            return None
        s = str(raw).strip()
        # Убираем всё, кроме цифр и одного ведущего «+».
        digits = re.sub(r"\D", "", s)
        if not digits:
            return None
        # Если пользователь ввёл что-то начинающееся на «+» — считаем
        # цифры как E.164 без знака; иначе — попробуем как RU по умолчанию.
        candidates = [digits]
        if not s.startswith("+"):
            # 8XXXXXXXXXX → +7XXXXXXXXXX (популярный российский ввод)
            if digits.startswith("8") and len(digits) == 11:
                candidates.append("7" + digits[1:])
            # 9XXXXXXXXX (10 цифр) → +7XXXXXXXXXX
            if len(digits) == 10:
                candidates.append("7" + digits)
        # Бажный ввод вида «+7+7XXXXXXXXXX» → digits = «77XXXXXXXXXX».
        # Сдвигаем начало на 1..3 позиции и пробуем переспарсить.
        for shift in (1, 2, 3):
            if len(digits) > 10 + shift:
                candidates.append(digits[shift:])
        for cand in candidates:
            try:
                num = phonenumbers.parse("+" + cand, None)
            except phonenumbers.NumberParseException:
                continue
            if not phonenumbers.is_valid_number(num):
                continue
            return {
                "e164": phonenumbers.format_number(
                    num, phonenumbers.PhoneNumberFormat.E164
                ),
                "country": phonenumbers.region_code_for_number(num) or "",
                "dial_code": "+" + str(num.country_code),
                "national": str(num.national_number),
            }
        return None

    # ------- список стран для селектора -------
    # Русские названия для популярных регионов; для остальных используем ISO.
    _RU_COUNTRY_NAMES = {
        "RU": "Россия", "UA": "Украина", "BY": "Беларусь", "KZ": "Казахстан",
        "UZ": "Узбекистан", "KG": "Киргизия", "TJ": "Таджикистан",
        "TM": "Туркмения", "AM": "Армения", "AZ": "Азербайджан", "GE": "Грузия",
        "MD": "Молдова", "EE": "Эстония", "LV": "Латвия", "LT": "Литва",
        "US": "США", "CA": "Канада", "MX": "Мексика", "BR": "Бразилия",
        "AR": "Аргентина", "CL": "Чили", "CO": "Колумбия", "PE": "Перу",
        "VE": "Венесуэла", "CU": "Куба",
        "GB": "Великобритания", "IE": "Ирландия", "FR": "Франция",
        "DE": "Германия", "IT": "Италия", "ES": "Испания", "PT": "Португалия",
        "NL": "Нидерланды", "BE": "Бельгия", "LU": "Люксембург", "CH": "Швейцария",
        "AT": "Австрия", "PL": "Польша", "CZ": "Чехия", "SK": "Словакия",
        "HU": "Венгрия", "RO": "Румыния", "BG": "Болгария", "GR": "Греция",
        "SE": "Швеция", "NO": "Норвегия", "FI": "Финляндия", "DK": "Дания",
        "IS": "Исландия", "HR": "Хорватия", "SI": "Словения", "RS": "Сербия",
        "BA": "Босния и Герцеговина", "MK": "Северная Македония", "AL": "Албания",
        "ME": "Черногория", "TR": "Турция", "CY": "Кипр", "MT": "Мальта",
        "CN": "Китай", "JP": "Япония", "KR": "Южная Корея", "KP": "КНДР",
        "IN": "Индия", "PK": "Пакистан", "BD": "Бангладеш", "LK": "Шри-Ланка",
        "NP": "Непал", "MM": "Мьянма", "TH": "Таиланд", "VN": "Вьетнам",
        "LA": "Лаос", "KH": "Камбоджа", "MY": "Малайзия", "SG": "Сингапур",
        "ID": "Индонезия", "PH": "Филиппины", "TW": "Тайвань", "HK": "Гонконг",
        "MO": "Макао", "MN": "Монголия",
        "AE": "ОАЭ", "SA": "Саудовская Аравия", "QA": "Катар", "BH": "Бахрейн",
        "KW": "Кувейт", "OM": "Оман", "YE": "Йемен", "JO": "Иордания",
        "LB": "Ливан", "SY": "Сирия", "IQ": "Ирак", "IR": "Иран", "IL": "Израиль",
        "PS": "Палестина", "AF": "Афганистан",
        "EG": "Египет", "MA": "Марокко", "DZ": "Алжир", "TN": "Тунис",
        "LY": "Ливия", "SD": "Судан", "ET": "Эфиопия", "KE": "Кения",
        "TZ": "Танзания", "UG": "Уганда", "RW": "Руанда", "NG": "Нигерия",
        "GH": "Гана", "CI": "Кот-д’Ивуар", "SN": "Сенегал", "CM": "Камерун",
        "ZA": "ЮАР", "ZW": "Зимбабве", "ZM": "Замбия", "MZ": "Мозамбик",
        "AO": "Ангола", "NA": "Намибия", "BW": "Ботсвана", "MG": "Мадагаскар",
        "AU": "Австралия", "NZ": "Новая Зеландия", "FJ": "Фиджи",
        "PG": "Папуа — Новая Гвинея",
    }

    def _flag_emoji(iso2: str) -> str:
        try:
            iso2 = (iso2 or "").upper()
            if len(iso2) != 2 or not iso2.isalpha():
                return ""
            return chr(0x1F1E6 + ord(iso2[0]) - ord("A")) + chr(0x1F1E6 + ord(iso2[1]) - ord("A"))
        except Exception:
            return ""

    _COUNTRIES_CACHE = None

    @app.route("/api/auth/phone/countries")
    def api_phone_countries():
        """Возвращает полный список стран (ISO, dial_code, имя, флаг).

        Кэшируется на процесс. Сортировка — по русскому имени, потом ISO.
        """
        nonlocal_cache = getattr(app, "_velora_countries_cache", None)
        if nonlocal_cache is not None:
            return jsonify({"ok": True, "items": nonlocal_cache})
        items = []
        from phonenumbers.geocoder import country_name_for_number as _cnfn
        for iso in phonenumbers.SUPPORTED_REGIONS:
            try:
                dial = phonenumbers.country_code_for_region(iso)
            except Exception:
                continue
            if not dial:
                continue
            name = _RU_COUNTRY_NAMES.get(iso)
            if not name:
                # Fallback на английское имя из phonenumbers (для редких стран).
                try:
                    sample = phonenumbers.example_number(iso)
                    if sample is not None:
                        name = _cnfn(sample, "en") or iso
                    else:
                        name = iso
                except Exception:
                    name = iso
            items.append({
                "iso": iso,
                "dial_code": "+" + str(dial),
                "name": name,
                "flag": _flag_emoji(iso),
            })
        items.sort(key=lambda x: (x["name"].lower(), x["iso"]))
        app._velora_countries_cache = items
        return jsonify({"ok": True, "items": items})

    @app.route("/api/auth/oauth/<provider>/start")
    def api_oauth_start(provider):
        """Начало OAuth-входа: отдаём фронту authorize URL провайдера."""
        provider = (provider or "").lower()
        if provider != "google":
            return jsonify({"ok": False, "error": "unknown_provider"}), 404
        client_id = _oauth_client_id(provider)
        if not client_id:
            return jsonify({
                "ok": False,
                "error": "not_configured",
                "message": "Вход через Google ещё не настроен на этом сервере",
            }), 501
        from urllib.parse import urlencode
        redirect_uri = url_for("api_oauth_callback", provider=provider, _external=True)
        state = secrets.token_urlsafe(24)
        session[f"oauth_state_{provider}"] = state
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
            "state": state,
        })
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + params
        return jsonify({"ok": True, "url": url})

    @app.route("/api/auth/oauth/<provider>/callback")
    def api_oauth_callback(provider):
        """OAuth callback: обмениваем code на токен, забираем профиль, логиним."""
        provider = (provider or "").lower()
        if provider != "google":
            abort(404)
        err = request.args.get("error")
        if err:
            return _oauth_error_page(f"Провайдер вернул ошибку: {err}")
        code = request.args.get("code")
        state = request.args.get("state")
        expected_state = session.pop(f"oauth_state_{provider}", None)
        if not code:
            return _oauth_error_page("Не получен код авторизации")
        if not expected_state or state != expected_state:
            return _oauth_error_page("Сессия авторизации устарела, попробуйте ещё раз")
        client_id = _oauth_client_id(provider)
        client_secret = _oauth_client_secret(provider)
        if not client_id or not client_secret:
            return _oauth_error_page("Сервер не настроен для этого провайдера")
        redirect_uri = url_for("api_oauth_callback", provider=provider, _external=True)

        try:
            import requests as _rq
            tok = _rq.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                timeout=10,
            ).json()
            access_token = tok.get("access_token")
            if not access_token:
                return _oauth_error_page(f"Google: {tok.get('error_description') or tok.get('error') or 'no token'}")
            info = _rq.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            ).json()
            ext_id = str(info.get("sub") or "")
            email = info.get("email") or None
            name = info.get("name") or info.get("given_name") or None
            avatar = info.get("picture") or None
            if not ext_id:
                return _oauth_error_page("Google не вернул идентификатор пользователя")
            u = _oauth_find_or_create_user(
                provider="google", ext_id=ext_id, email=email, name=name, avatar=avatar,
            )
            _establish_session(u, provider="google")
            return _oauth_success_page()
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("oauth callback failed")
            return _oauth_error_page(f"Внутренняя ошибка: {exc}")

    @app.route("/api/auth/phone/normalize", methods=["POST"])
    def api_phone_normalize():
        """Принимает сырой ввод, отдаёт нормализованный E.164 + страну.

        Используется на фронте сразу при вводе, чтобы убрать «+7+7…» и
        автоматически подставить страну/маску.
        """
        data = request.get_json(silent=True) or {}
        norm = _normalize_phone(data.get("phone") or "")
        if not norm:
            return jsonify({"ok": False, "error": "invalid"}), 400
        return jsonify({"ok": True, **norm})

    @app.route("/api/auth/username/check")
    def api_username_check():
        """Проверка доступности ника `@…`.

        Используется при регистрации и при редактировании профиля.
        Возвращает {available: bool, message: str}.
        """
        raw = (request.args.get("u") or "").strip().lstrip("@")
        exclude_id = current_user.id if current_user.is_authenticated else None
        norm, err = _validate_username(raw, exclude_user_id=exclude_id)
        if err:
            return jsonify({"available": False, "username": norm or "", "message": err})
        return jsonify({"available": True, "username": norm, "message": "Свободно"})

    @app.route("/api/auth/phone/start", methods=["POST"])
    def api_phone_start():
        """Стартует phone-flow: создаёт VerifyAttempt(tg_link) + deep-link.

        Сервер сам определяет режим по факту наличия пользователя:
          - mode=register → бот попросит «Поделиться контактом»;
          - mode=login    → бот пришлёт 6-значный код, пользователь вводит его на сайте.
        """
        data = request.get_json(silent=True) or {}
        norm = _normalize_phone(data.get("phone") or "")
        if not norm:
            return jsonify({"error": "invalid_phone",
                            "message": "Неверный формат номера"}), 400
        requested_mode = (data.get("mode") or "").strip().lower()
        existing = db.session.query(User).filter_by(phone=norm["e164"]).first()
        # Валидация запрошенного режима.
        if requested_mode == "login":
            if not existing or not existing.tg_id:
                return jsonify({"error": "no_account",
                                "message": "Аккаунт с этим номером не найден. Сначала зарегистрируйтесь."}), 404
            mode = "login"
        elif requested_mode == "register":
            if existing and existing.tg_id:
                return jsonify({"error": "already_registered",
                                "message": "Аккаунт с этим номером уже существует. Войдите по коду."}), 409
            mode = "register"
        else:
            # Нет явного режима — авто-детект (на всякий случай).
            mode = "login" if (existing and existing.tg_id) else "register"
        token = secrets.token_urlsafe(12)
        # Удаляем старые незавершённые попытки этого же телефона.
        try:
            old = (db.session.query(VerifyAttempt)
                   .filter_by(kind="tg_link", phone_normalized=norm["e164"], verified=False)
                   .all())
            for o in old:
                db.session.delete(o)
            db.session.commit()
        except Exception:
            db.session.rollback()
        extra = {"mode": mode}
        if existing:
            extra["expected_tg_id"] = int(existing.tg_id) if existing.tg_id else None
            extra["user_id"] = existing.id
        # Черновик профиля для регистрации (username/display_name/dob).
        if mode == "register":
            draft_user = (data.get("username") or "").strip().lstrip("@")
            draft_dn = (data.get("display_name") or "").strip()[:120]
            draft_dob = (data.get("dob") or "").strip()
            if draft_user:
                # Проверяем формат и уникальность сразу.
                v_uname, err = _validate_username(draft_user)
                if err:
                    return jsonify({"ok": False, "error": "username", "message": err}), 400
                extra["req_username"] = v_uname
            if draft_dn:
                extra["req_display_name"] = draft_dn
            if draft_dob:
                d_, derr = _parse_dob(draft_dob)
                if derr:
                    return jsonify({"ok": False, "error": "dob", "message": derr}), 400
                extra["req_dob"] = d_.isoformat() if d_ else None
        row = VerifyAttempt(
            kind="tg_link",
            target=token,
            phone_normalized=norm["e164"],
            extra=json.dumps(extra, ensure_ascii=False),
            expires_at=datetime.utcnow() + timedelta(minutes=10),
            verified=False,
        )
        db.session.add(row)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "db"}), 500
        bot_username = (_TG_POLLER.bot_username if _TG_POLLER else "") or os.environ.get("VELORA_TG_BOT_USERNAME", "") or Config.TG_BOT_USERNAME
        deep_link = f"https://t.me/{bot_username}?start=link_{token}" if bot_username else ""
        return jsonify({
            "ok": True,
            "mode": mode,
            "token": token,
            "deep_link": deep_link,
            "bot_username": bot_username,
            **norm,
        })

    @app.route("/api/auth/phone/check")
    def api_phone_check():
        """Поллинг статуса phone-flow.

        Используется только в режиме register: ждём, пока пользователь
        поделился контактом → создаём User → логиним.
        В режиме login клиент сразу спрашивает код у пользователя и шлёт
        его в /api/auth/phone/code.
        """
        token = (request.args.get("token") or "").strip()
        if not token:
            return jsonify({"ok": False, "status": "missing"}), 400
        row = (db.session.query(VerifyAttempt)
               .filter_by(kind="tg_link", target=token).first())
        if not row:
            return jsonify({"ok": False, "status": "not_found"}), 404
        if row.expires_at and row.expires_at < datetime.utcnow():
            return jsonify({"ok": False, "status": "expired"}), 410
        try:
            extra = json.loads(row.extra or "{}") or {}
        except Exception:
            extra = {}
        mode = extra.get("mode") or "register"
        if mode == "login":
            # В login-режиме сюда вообще не должны лезть, но подскажем фронту.
            return jsonify({"ok": False, "status": "code_required",
                            "code_sent": bool(extra.get("code_sent"))})
        if not row.verified:
            return jsonify({"ok": False, "status": "pending"})
        # === register: подтверждено → создаём User ===
        tg_id = int(extra.get("tg_id") or 0)
        phone = extra.get("phone") or row.phone_normalized
        if not tg_id or not phone:
            return jsonify({"ok": False, "status": "broken"}), 500
        u = (db.session.query(User).filter_by(tg_id=tg_id).first()
             or db.session.query(User).filter_by(phone=phone).first())
        is_new = False
        if not u:
            is_new = True
            # Используем ник, указанный при регистрации, если он всё ещё свободен.
            req_uname = extra.get("req_username") or ""
            uname = None
            if req_uname:
                v_uname, err = _validate_username(req_uname)
                if not err:
                    uname = v_uname
            if not uname:
                uname = _make_unique_username_from_tg(
                    extra.get("tg_username"), extra.get("tg_first_name"), tg_id,
                )
            display_name = (extra.get("req_display_name")
                            or extra.get("tg_first_name")
                            or extra.get("tg_username")
                            or uname)
            dob_val = None
            kids_default = False
            req_dob = extra.get("req_dob") or ""
            if req_dob:
                d_, _err = _parse_dob(req_dob)
                if d_:
                    dob_val = d_
                    age = _calc_age(d_)
                    if age is not None and age < 18:
                        kids_default = True
            u = User(
                username=uname,
                display_name=display_name,
                tg_id=tg_id,
                tg_username=extra.get("tg_username"),
                tg_first_name=extra.get("tg_first_name"),
                tg_photo_url=extra.get("tg_photo_url"),
                avatar=extra.get("tg_photo_url") or None,
                phone=phone,
                phone_verified=True,
                slug=_make_unique_user_slug(uname),
                uid=secrets.token_hex(6),
                password_hash=f"tg:{secrets.token_hex(16)}",
                dob=dob_val,
                kids_mode=kids_default,
            )
            db.session.add(u)
        else:
            if not u.tg_id:
                u.tg_id = tg_id
            if not u.phone:
                u.phone = phone
            u.phone_verified = True
            if extra.get("tg_username") and not u.tg_username:
                u.tg_username = extra["tg_username"]
            if extra.get("tg_first_name") and not u.tg_first_name:
                u.tg_first_name = extra["tg_first_name"]
            if extra.get("tg_photo_url"):
                u.tg_photo_url = extra["tg_photo_url"]
            if not u.uid:
                u.uid = secrets.token_hex(6)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"ok": False, "status": "race"}), 409
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
        _establish_session(u, provider="telegram_phone")
        return jsonify({
            "ok": True, "status": "ok",
            "id": u.id, "uid": u.uid, "username": u.username, "is_new": is_new,
        })

    @app.route("/api/auth/phone/code", methods=["POST"])
    def api_phone_code():
        """Принимает 6-значный код, который бот прислал в login-режиме."""
        data = request.get_json(silent=True) or {}
        token = (data.get("token") or "").strip()
        code = re.sub(r"\D", "", str(data.get("code") or ""))
        if not token or len(code) != 6:
            return jsonify({"error": "invalid", "message": "Введите 6-значный код"}), 400
        row = (db.session.query(VerifyAttempt)
               .filter_by(kind="tg_link", target=token).first())
        if not row:
            return jsonify({"error": "not_found", "message": "Сессия не найдена"}), 404
        if row.expires_at and row.expires_at < datetime.utcnow():
            return jsonify({"error": "expired", "message": "Время истекло"}), 410
        try:
            extra = json.loads(row.extra or "{}") or {}
        except Exception:
            extra = {}
        if (extra.get("mode") or "register") != "login":
            return jsonify({"error": "wrong_mode"}), 400
        # Лимит попыток.
        attempts = int(row.attempts or 0)
        if attempts >= 5:
            return jsonify({"error": "too_many",
                            "message": "Слишком много попыток. Запросите новую ссылку."}), 429
        if (extra.get("code") or "") != code:
            row.attempts = attempts + 1
            db.session.commit()
            return jsonify({"error": "wrong_code", "message": "Неверный код"}), 400
        # Код верен → логиним юзера.
        uid = int(extra.get("user_id") or 0)
        u = db.session.query(User).get(uid) if uid else None
        if not u:
            phone = row.phone_normalized
            u = db.session.query(User).filter_by(phone=phone).first()
        if not u:
            return jsonify({"error": "user_gone"}), 404
        # Чистим использованную запись.
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
        _establish_session(u, provider="telegram_phone_code")
        return jsonify({
            "ok": True, "id": u.id, "uid": u.uid, "username": u.username,
        })

    # ---- Старые endpoints отключены (410 Gone) -------------------------
    # На production (velora-sound.ru) /api/auth/register и /api/auth/login
    # ВСЕГДА возвращают 410. На тестовом хосте (f0943065.xsph.ru или
    # localhost) при VELORA_DEV_AUTH=1 они работают как простой
    # identifier+password флоу — для отладки UI без Telegram.
    # Гейтим по request.host, потому что один uWSGI worker может
    # обслуживать оба домена с одним общим os.environ.
    _GONE = ("/api/auth/email/request", "/api/auth/email/verify",
             "/api/auth/email/finish", "/api/auth/phone/finish")

    # Whitelist хостов где dev-auth разрешён. Sprinthost workers могут
    # стартовать от разных .env, поэтому НЕ полагаемся на os.environ —
    # только на текущий request.host.
    _DEV_AUTH_HOSTS = {"f0943065.xsph.ru", "localhost", "127.0.0.1"}

    def _is_dev_auth_request() -> bool:
        host = (request.host or "").split(":")[0].lower()
        return host in _DEV_AUTH_HOSTS

    from werkzeug.security import generate_password_hash, check_password_hash

    @app.route("/api/auth/register", methods=["POST"])
    def api_dev_register():
        if not _is_dev_auth_request():
            return jsonify({"error": "gone",
                "message": "Этот способ входа отключён. Используйте Telegram-бота."}), 410
        data = request.get_json(silent=True) or {}
        ident = (data.get("identifier") or data.get("username") or "").strip()
        pwd = data.get("password") or ""
        if len(ident) < 3 or len(ident) > 64 or not ident.replace("_", "").isalnum():
            return jsonify({"error": "bad_identifier"}), 400
        if len(pwd) < 6:
            return jsonify({"error": "weak_password"}), 400
        if db.session.query(User).filter_by(username=ident).first():
            return jsonify({"error": "exists"}), 409
        u = User(
            username=ident,
            password_hash=generate_password_hash(pwd),
            created_at=datetime.utcnow(),
        )
        db.session.add(u)
        db.session.commit()
        login_user(u, remember=True)
        session.permanent = True
        _establish_session(u, provider="dev_password")
        return jsonify({"ok": True, "id": u.id, "uid": u.uid, "username": u.username})

    @app.route("/api/auth/login", methods=["POST"])
    def api_dev_login():
        if not _is_dev_auth_request():
            return jsonify({"error": "gone",
                "message": "Этот способ входа отключён. Используйте Telegram-бота."}), 410
        data = request.get_json(silent=True) or {}
        ident = (data.get("identifier") or data.get("username") or "").strip()
        pwd = data.get("password") or ""
        u = db.session.query(User).filter_by(username=ident).first()
        if not u or not u.password_hash or not check_password_hash(u.password_hash, pwd):
            return jsonify({"error": "bad_credentials"}), 401
        login_user(u, remember=True)
        session.permanent = True
        _establish_session(u, provider="dev_password")
        return jsonify({"ok": True, "id": u.id, "uid": u.uid, "username": u.username})

    for _path in _GONE:
        def _gone(_=None, _p=_path):
            return jsonify({
                "error": "gone",
                "message": "Этот способ входа отключён. Используйте Telegram-бота.",
            }), 410
        app.add_url_rule(_path, endpoint=f"_gone_{_path}", view_func=_gone,
                         methods=["GET", "POST"])

    @app.route("/api/auth/logout", methods=["POST"])
    def api_logout():
        sid = session.pop("sid", None)
        if sid:
            try:
                row = db.session.query(AuthSession).filter_by(sid=sid).first()
                if row:
                    row.revoked = True
                    row.revoked_at = datetime.utcnow()
                    db.session.commit()
            except Exception:
                db.session.rollback()
        logout_user()
        return jsonify({"ok": True})

    # =====================================================================
    # СЕССИИ ПОЛЬЗОВАТЕЛЯ (для UI «Активные сессии» в настройках)
    # =====================================================================
    @app.route("/api/me/sessions")
    @login_required
    def api_my_sessions():
        cur_sid = session.get("sid")
        rows = (
            db.session.query(AuthSession)
            .filter_by(user_id=current_user.id, revoked=False)
            .order_by(AuthSession.last_seen.desc())
            .all()
        )
        return jsonify({
            "current_sid": cur_sid,
            "sessions": [{
                "id": r.id,
                "platform": r.platform or "Неизвестно",
                "browser": r.browser or "",
                "provider": r.provider or "",
                "ip": r.ip or "",
                "geo": r.geo or "",
                "user_agent": r.user_agent or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                "current": (r.sid == cur_sid),
            } for r in rows],
        })

    @app.route("/api/me/sessions/<int:sid>", methods=["DELETE"])
    @login_required
    def api_revoke_session(sid: int):
        row = db.session.query(AuthSession).filter_by(id=sid, user_id=current_user.id).first()
        if not row:
            return jsonify({"error": "not_found"}), 404
        row.revoked = True
        row.revoked_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "current": (row.sid == session.get("sid"))})

    @app.route("/api/me/sessions/all", methods=["DELETE"])
    @login_required
    def api_revoke_all_sessions():
        cur_sid = session.get("sid")
        rows = (
            db.session.query(AuthSession)
            .filter_by(user_id=current_user.id, revoked=False)
            .all()
        )
        for r in rows:
            if r.sid == cur_sid:
                continue
            r.revoked = True
            r.revoked_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "kept_sid": cur_sid})

    # =====================================================================
    # ИЗОБРАЖЕНИЯ: upload (data:URL → файл) + раздача всем
    # =====================================================================
    _ALLOWED_MIMES = {
        "image/png": ".png",
        "image/apng": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/bmp": ".bmp",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/jxl": ".jxl",
        "image/tiff": ".tiff",
        "image/x-icon": ".ico",
        "image/vnd.microsoft.icon": ".ico",
    }
    # Общий лимит — 25 МБ (расчёт на GIF/анимации).
    # Для статичных картинок клиент сам ужимает до ~10 МБ.
    _MAX_IMAGE_BYTES = 25 * 1024 * 1024

    def _save_image_bytes(raw: bytes, mime: str, kind: str, user_id: int | None) -> ImageBlob:
        if mime not in _ALLOWED_MIMES:
            raise ValueError("unsupported_mime")
        if len(raw) > _MAX_IMAGE_BYTES:
            raise ValueError("too_large")
        sha = hashlib.sha256(raw).hexdigest()
        # Дедупликация: если уже есть файл с таким хэшем — возвращаем его.
        existing = db.session.query(ImageBlob).filter_by(sha256=sha).first()
        if existing:
            # Если это новая привязка (другой пользователь / другой kind), просто
            # вернём существующий — файл общий, доступ всем.
            return existing
        ext = _ALLOWED_MIMES[mime]
        Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        rel_path = f"uploads/{sha}{ext}"
        full_path = Config.UPLOAD_DIR / f"{sha}{ext}"
        try:
            full_path.write_bytes(raw)
        except OSError as exc:
            raise ValueError(f"write_failed: {exc}") from exc
        row = ImageBlob(
            user_id=user_id,
            kind=kind[:32] or "misc",
            mime=mime,
            sha256=sha,
            size=len(raw),
            path=rel_path,
        )
        db.session.add(row)
        db.session.commit()
        return row

    @app.route("/api/upload/image", methods=["POST"])
    @login_required
    def api_upload_image():
        """Загрузка картинки. Принимает либо multipart-файл, либо JSON с data: URL.

        Body (JSON): {"data_url": "data:image/png;base64,...", "kind": "avatar"}
        Body (multipart): file=<file>, kind=avatar
        """
        kind = "misc"
        raw: bytes | None = None
        mime: str | None = None
        if request.content_type and "multipart/form-data" in request.content_type:
            f = request.files.get("file")
            kind = (request.form.get("kind") or "misc").strip()
            if not f:
                return jsonify({"error": "no_file"}), 400
            raw = f.read()
            mime = (f.mimetype or "").lower()
        else:
            data = request.get_json(silent=True) or {}
            kind = (data.get("kind") or "misc").strip()
            url = data.get("data_url") or ""
            m = _DATA_URL_RE.match(url)
            if not m:
                return jsonify({"error": "invalid_data_url"}), 400
            # _DATA_URL_RE captures only subtype (e.g. "png", "jpeg").
            # Reconstruct full mime; нормализуем jpg→jpeg.
            sub = m.group(1).lower()
            if sub == "jpg":
                sub = "jpeg"
            mime = f"image/{sub}"
            try:
                raw = base64.b64decode(url.split(",", 1)[1])
            except Exception:
                return jsonify({"error": "bad_base64"}), 400
        try:
            row = _save_image_bytes(raw or b"", mime or "", kind, current_user.id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "id": row.id, "url": row.url, "mime": row.mime})

    @app.route("/api/img/<int:img_id>")
    def api_get_image(img_id: int):
        row = db.session.get(ImageBlob, img_id)
        if not row:
            abort(404)
        full_path = Path(__file__).resolve().parent.parent.parent / "instance" / row.path
        if not full_path.exists():
            abort(404)
        resp = send_file(str(full_path), mimetype=row.mime, conditional=True,
                        last_modified=row.created_at)
        # Картинки публичные; кэшируем агрессивно — путь иммутабельный (по sha).
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


    @app.route("/api/settings", methods=["GET", "POST"])
    @login_required
    def api_settings():
        if request.method == "GET":
            try:
                return jsonify(json.loads(current_user.settings or "{}"))
            except Exception:
                return jsonify({})
        data = request.get_json(silent=True) or {}
        # kids_mode хранится отдельным флагом для серверной фильтрации
        if "kids_mode" in data:
            current_user.kids_mode = bool(data.pop("kids_mode"))
        current_user.settings = json.dumps(data, ensure_ascii=False)
        db.session.commit()
        return jsonify({"ok": True})

    # ----------- PROFILE -------------------------------------------------
    @app.route("/api/profile", methods=["GET", "POST"])
    @login_required
    def api_profile():
        if request.method == "GET":
            return jsonify({
                "username": current_user.username,
                "slug": current_user.slug or current_user.username,
                "display_name": current_user.display_name or current_user.username,
                "email": current_user.email or "",
                "bio": current_user.bio or "",
                "avatar": current_user.avatar or "",
                "cover": current_user.cover or "",
                "location": current_user.location or "",
                "website": current_user.website or "",
                "kids_mode": bool(current_user.kids_mode),
                "dob": current_user.dob.isoformat() if getattr(current_user, "dob", None) else "",
                "age": _calc_age(getattr(current_user, "dob", None)),
                "kids_mode_locked": bool(
                    getattr(current_user, "dob", None)
                    and (_calc_age(current_user.dob) or 99) < 18
                ),
                "wall_enabled": bool(getattr(current_user, "wall_enabled", True)),
                "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
            })
        data = request.get_json(silent=True) or {}
        if "username" in data:
            new_uname, err = _validate_username(data.get("username") or "", exclude_user_id=current_user.id)
            if err:
                return jsonify({"ok": False, "error": "username", "message": err}), 400
            current_user.username = new_uname
            # Перегенерируем slug, если он совпадал с остаревшим ником.
            try:
                if not current_user.slug or current_user.slug == data.get("_old_username"):
                    current_user.slug = _make_unique_user_slug(new_uname)
            except Exception:
                pass
        if "dob" in data:
            d_, err = _parse_dob(data.get("dob"))
            if err:
                return jsonify({"ok": False, "error": "dob", "message": err}), 400
            current_user.dob = d_
            # Если < 18 — kids_mode включается принудительно.
            age = _calc_age(d_)
            if age is not None and age < 18:
                current_user.kids_mode = True
        if "display_name" in data:
            current_user.display_name = (data.get("display_name") or "").strip()[:120] or current_user.username
        if "bio" in data:
            current_user.bio = (data.get("bio") or "").strip()[:500]
        if "location" in data:
            current_user.location = (data.get("location") or "").strip()[:120] or None
        if "website" in data:
            current_user.website = (data.get("website") or "").strip()[:255] or None
        if "email" in data:
            email = (data.get("email") or "").strip() or None
            current_user.email = email
        if "avatar" in data:
            v = _validate_image(data.get("avatar") or "")
            current_user.avatar = v if data.get("avatar") else None
        if "cover" in data:
            v = _validate_image(data.get("cover") or "")
            current_user.cover = v if data.get("cover") else None
        if "banner" in data:
            v = _validate_image(data.get("banner") or "")
            current_user.banner = v if data.get("banner") else None
        if "is_private" in data:
            current_user.is_private = bool(data.get("is_private"))
        if "privacy" in data and isinstance(data.get("privacy"), dict):
            current_user.privacy = json.dumps(_sanitize_privacy(data.get("privacy")), ensure_ascii=False)
        if "kids_mode" in data:
            want = bool(data.get("kids_mode"))
            # Блокировка: если юзеру < 18 — выключить нельзя.
            age = _calc_age(getattr(current_user, "dob", None))
            if age is not None and age < 18 and not want:
                return jsonify({"ok": False, "error": "kids_locked",
                                "message": "Детский режим нельзя отключить до совершеннолетия"}), 403
            current_user.kids_mode = want
        if "wall_enabled" in data:
            current_user.wall_enabled = bool(data.get("wall_enabled"))
        db.session.commit()
        return jsonify({"ok": True})

    # ----------- PUBLIC USERS / FOLLOWS ---------------------------------
    @app.route("/api/u/<slug>")
    def api_public_user(slug: str):
        u = _resolve_user(slug)
        if not u:
            return jsonify({"error": "not_found"}), 404
        is_self = current_user.is_authenticated and current_user.id == u.id
        priv = _parse_privacy(u.privacy)
        # Проверка: подписан ли я на этого пользователя
        am_following = False
        if current_user.is_authenticated and not is_self:
            am_following = bool(
                db.session.query(Follow)
                .filter_by(follower_id=current_user.id, followee_id=u.id)
                .first()
            )
        followers_cnt = db.session.query(Follow).filter_by(followee_id=u.id).count()
        following_cnt = db.session.query(Follow).filter_by(follower_id=u.id).count()
        # Базовая «всегда видимая» инфа
        seed = (u.slug or u.username or str(u.id)).lower()
        result = {
            "id": u.id,
            "username": u.username,
            "slug": u.slug or u.username,
            "display_name": u.display_name or u.username,
            "is_private": bool(u.is_private),
            "is_self": is_self,
            "am_following": am_following,
            "followers": followers_cnt,
            "following": following_cnt,
            "seed": seed,  # клиент сам сгенерит градиент-заглушку
        }
        # Если приватный и это не я — ничего больше не отдаём
        if u.is_private and not is_self:
            result["bio"] = None
            result["avatar"] = None
            result["banner"] = None
            result["location"] = None
            result["website"] = None
            result["created_at"] = None
            result["stats"] = None
            result["playlists"] = []
            return jsonify(result)
        # Открытый профиль — учитываем privacy-флаги (для is_self отдаём всё)
        def show(key: str) -> bool:
            if is_self:
                return True
            return bool(priv.get(key, _DEFAULT_PRIVACY.get(key, True)))
        result["bio"] = (u.bio or "") if show("show_bio") else None
        result["avatar"] = (u.avatar or "") if show("show_avatar") else None
        result["banner"] = (u.banner or "") if show("show_banner") else None
        result["location"] = (u.location or "") if show("show_location") else None
        result["website"] = (u.website or "") if show("show_website") else None
        result["dob"] = (u.dob.isoformat() if u.dob else None) if show("show_dob") else None
        result["wall_visible"] = show("show_wall")
        result["wall_enabled"] = bool(getattr(u, "wall_enabled", True))
        result["created_at"] = u.created_at.isoformat() if u.created_at else None
        if show("show_stats"):
            likes_cnt = db.session.query(Like).filter_by(user_id=u.id).count()
            pls_cnt = db.session.query(Playlist).filter_by(user_id=u.id).count()
            result["stats"] = {"likes": likes_cnt, "playlists": pls_cnt}
        else:
            result["stats"] = None
        if show("show_playlists"):
            pls = (
                db.session.query(Playlist)
                .filter_by(user_id=u.id, is_public=True)
                .order_by(Playlist.updated_at.desc())
                .limit(20)
                .all()
            )
            result["playlists"] = [
                {"id": p.id, "name": p.name, "slug": p.slug, "cover": p.cover or "",
                 "count": len(p.items), "description": p.description or ""}
                for p in pls
            ]
        else:
            result["playlists"] = []
        return jsonify(result)

    @app.route("/api/u/<slug>/follow", methods=["POST", "DELETE"])
    @login_required
    def api_follow_user(slug: str):
        u = _resolve_user(slug)
        if not u:
            return jsonify({"error": "not_found"}), 404
        if u.id == current_user.id:
            return jsonify({"error": "self"}), 400
        existing = (
            db.session.query(Follow)
            .filter_by(follower_id=current_user.id, followee_id=u.id)
            .first()
        )
        if request.method == "POST":
            if not existing:
                db.session.add(Follow(follower_id=current_user.id, followee_id=u.id))
                try: db.session.commit()
                except IntegrityError: db.session.rollback()
            return jsonify({"ok": True, "following": True})
        # DELETE
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({"ok": True, "following": False})

    # --------------------------- WALL POSTS ------------------------------
    _WALL_TTL_CHOICES = (1, 6, 24, 72, 168, 24 * 30)  # часы: 1ч, 6ч, 1д, 3д, 7д, 30д
    _WALL_TTL_DEFAULT = 24 * 7  # неделя
    _WALL_RATE_LIMIT_SEC = 30   # минимум 30 сек между постами одного пользователя

    def _serialize_wall_post(p: WallPost, author: User | None) -> dict:
        a = author
        return {
            "id": p.id,
            "owner_id": p.owner_id,
            "author_id": p.author_id,
            "text": p.text or "",
            "image_url": p.image_url or "",
            "status": p.status or "published",
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "author": {
                "id": a.id if a else None,
                "username": a.username if a else "удалённый",
                "slug": (a.slug or a.username) if a else "",
                "display_name": (a.display_name or a.username) if a else "Аккаунт удалён",
                "avatar": (a.avatar or "") if a else "",
            },
            "can_delete": current_user.is_authenticated and (
                current_user.id == p.author_id or current_user.id == p.owner_id
            ),
        }

    def _wall_cleanup_expired() -> int:
        """Удаляет посты, у которых expires_at прошёл. Lazy GC: вызывается на GET."""
        try:
            now = datetime.utcnow()
            n = (db.session.query(WallPost)
                 .filter(WallPost.expires_at.isnot(None), WallPost.expires_at < now)
                 .delete(synchronize_session=False))
            if n:
                db.session.commit()
            return n
        except Exception:
            db.session.rollback()
            return 0

    @app.route("/api/u/<slug>/wall", methods=["GET", "POST"])
    def api_wall(slug: str):
        owner = _resolve_user(slug)
        if not owner:
            return jsonify({"error": "not_found"}), 404
        if request.method == "GET":
            _wall_cleanup_expired()
            # Респектируем приватность стены: show_wall.
            is_self = current_user.is_authenticated and current_user.id == owner.id
            priv = _parse_privacy(getattr(owner, "privacy", None))
            if not is_self and not priv.get("show_wall", True):
                return jsonify({
                    "ok": True, "owner_id": owner.id, "wall_enabled": False,
                    "can_post": False, "hidden": True, "posts": [],
                })
            limit = max(1, min(100, int(request.args.get("limit") or 50)))
            posts = (
                db.session.query(WallPost)
                .filter_by(owner_id=owner.id, status="published")
                .order_by(WallPost.created_at.desc())
                .limit(limit)
                .all()
            )
            authors = {a.id: a for a in db.session.query(User).filter(
                User.id.in_({p.author_id for p in posts})
            ).all()} if posts else {}
            return jsonify({
                "ok": True,
                "owner_id": owner.id,
                "wall_enabled": bool(getattr(owner, "wall_enabled", True)),
                "ttl_choices": list(_WALL_TTL_CHOICES),
                "ttl_default": _WALL_TTL_DEFAULT,
                "can_post": (
                    current_user.is_authenticated
                    and (
                        current_user.id == owner.id
                        or (
                            bool(getattr(owner, "wall_enabled", True))
                            and not (owner.is_private and owner.id != current_user.id)
                        )
                    )
                ),
                "posts": [_serialize_wall_post(p, authors.get(p.author_id)) for p in posts],
            })
        # POST
        if not current_user.is_authenticated:
            return jsonify({"error": "auth_required",
                            "message": "Войдите, чтобы писать."}), 401
        # Владелец стены может писать всегда — флаг wall_enabled ограничивает
        # только посторонних.
        is_owner = (current_user.id == owner.id)
        if not is_owner and not getattr(owner, "wall_enabled", True):
            return jsonify({"error": "wall_disabled",
                            "message": "Владелец отключил стену"}), 403
        if not is_owner and owner.is_private:
            return jsonify({"error": "private",
                            "message": "Профиль приватный — писать на стене нельзя"}), 403
        # Анти-спам: не чаще чем раз в N секунд.
        try:
            recent = (db.session.query(WallPost)
                      .filter_by(author_id=current_user.id)
                      .order_by(WallPost.created_at.desc()).first())
            if recent and recent.created_at:
                gap = (datetime.utcnow() - recent.created_at).total_seconds()
                if gap < _WALL_RATE_LIMIT_SEC:
                    return jsonify({
                        "error": "rate_limit",
                        "message": f"Слишком часто. Подождите {int(_WALL_RATE_LIMIT_SEC - gap)} сек.",
                    }), 429
        except Exception:
            pass
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if len(text) > 2000:
            text = text[:2000]
        # TTL: часы
        try:
            ttl_h = int(data.get("ttl_hours") or _WALL_TTL_DEFAULT)
        except Exception:
            ttl_h = _WALL_TTL_DEFAULT
        if ttl_h not in _WALL_TTL_CHOICES:
            # Принимаем любое значение в [1..720], для надёжности обрезаем.
            ttl_h = max(1, min(24 * 30, ttl_h))
        # Изображение (опционально). Принимаем data: URL ИЛИ /api/img/<id>.
        image_url = ""
        img_raw_payload = data.get("image_data_url") or ""
        img_existing = (data.get("image_url") or "").strip()
        if img_raw_payload:
            m = _DATA_URL_RE.match(img_raw_payload)
            if not m:
                app.logger.info("wall POST 400 image: bad data-url prefix=%r",
                                img_raw_payload[:60])
                return jsonify({
                    "error": "image",
                    "message": "Неподдерживаемый формат изображения. Разрешены PNG, JPEG, GIF, WEBP.",
                }), 400
            mime = m.group(1).lower()
            # Нормализуем: regex отдаёт только subtype (gif/png/...).
            # Старые/HEIC и пр. сохраняем как jpeg, чтобы _save_image_bytes принял.
            _SUBTYPE_TO_FULL = {
                "png": "image/png", "apng": "image/png",
                "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
                "avif": "image/avif",
            }
            full_mime = _SUBTYPE_TO_FULL.get(mime)
            if not full_mime:
                # Экзотика (bmp/tiff/heic/jxl): на нашем сервере не поддержано
                # для хранения, но клиент уже должен был сконвертировать в JPEG.
                return jsonify({
                    "error": "image",
                    "message": "Этот формат не поддерживается. Сохраните в JPEG/PNG/GIF/WebP.",
                }), 400
            try:
                raw = base64.b64decode(img_raw_payload.split(",", 1)[1])
            except Exception:
                return jsonify({"error": "image", "message": "Не удалось декодировать"}), 400
            if len(raw) > _MAX_IMAGE_BYTES:
                return jsonify({"error": "image",
                                "message": "Файл слишком большой (>25 МБ). Сжмите или выберите другое."}), 400
            ok, reason = _mod.check_image(raw, full_mime)
            if not ok:
                return jsonify({"error": "moderation", "message": reason}), 422
            try:
                row = _save_image_bytes(raw, full_mime, "wall", current_user.id)
                image_url = row.url
            except ValueError as exc:
                return jsonify({"error": "image", "message": str(exc)}), 400
        elif img_existing:
            v = _validate_image(img_existing)
            if not v:
                return jsonify({"error": "image", "message": "Невалидная ссылка"}), 400
            image_url = v
        # Должно быть хоть что-то — либо текст, либо картинка.
        if not text and not image_url:
            return jsonify({"error": "empty",
                            "message": "Напишите текст или прикрепите картинку."}), 400
        # Текстовая модерация.
        ok, reason = _mod.check_text(text)
        if not ok:
            return jsonify({"error": "moderation", "message": reason}), 422
        post = WallPost(
            owner_id=owner.id,
            author_id=current_user.id,
            text=text,
            image_url=image_url or None,
            status="published",
            expires_at=datetime.utcnow() + timedelta(hours=ttl_h),
        )
        db.session.add(post)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({"error": "db"}), 500
        return jsonify({"ok": True, "post": _serialize_wall_post(post, current_user)})

    @app.route("/api/wall/<int:post_id>", methods=["DELETE"])
    @login_required
    def api_wall_delete(post_id: int):
        post = db.session.get(WallPost, post_id)
        if not post:
            return jsonify({"error": "not_found"}), 404
        if current_user.id not in (post.author_id, post.owner_id):
            return jsonify({"error": "forbidden"}), 403
        db.session.delete(post)
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/me/follows")
    @login_required
    def api_my_follows():
        kind = (request.args.get("kind") or "following").strip()
        if kind == "followers":
            rows = (
                db.session.query(Follow, User)
                .join(User, User.id == Follow.follower_id)
                .filter(Follow.followee_id == current_user.id)
                .order_by(Follow.created_at.desc())
                .all()
            )
        else:
            rows = (
                db.session.query(Follow, User)
                .join(User, User.id == Follow.followee_id)
                .filter(Follow.follower_id == current_user.id)
                .order_by(Follow.created_at.desc())
                .all()
            )
        out = []
        for _f, u in rows:
            seed = (u.slug or u.username or str(u.id)).lower()
            # Уважаем приватность для аватарки
            priv = _parse_privacy(u.privacy)
            show_av = (not u.is_private) and priv.get("show_avatar", True)
            out.append({
                "id": u.id,
                "username": u.username,
                "slug": u.slug or u.username,
                "display_name": u.display_name or u.username,
                "avatar": (u.avatar or "") if show_av else "",
                "is_private": bool(u.is_private),
                "seed": seed,
            })
        return jsonify(out)

    # ----------- SEARCH --------------------------------------------------
    @app.route("/api/search/tracks")
    def api_search_tracks():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        sources = (request.args.get("sources") or "deezer,apple").split(",")
        # Генерируем варианты запроса: исходный + транслит туда/обратно
        # (морген → morgen, madkid → мадкид).
        queries = _expand_search_queries(q)
        results = []
        for query in queries:
            if "deezer" in sources:
                try: results.append(deezer.search_tracks(query, 40))
                except Exception: pass
            if "apple" in sources:
                try: results.append(itunes.search_tracks(query, 25))
                except Exception: pass
        merged = _merge_unique(*results)
        return jsonify(_kids_filter_dicts([asdict(t) for t in merged]))

    @app.route("/api/search/artists")
    def api_search_artists():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        # Те же варианты, что и для треков — мадкид/madkid дают одинаковый результат.
        queries = _expand_search_queries(q)
        results = []
        for query in queries:
            try:
                results.extend(deezer.search_artists(query, 25))
            except Exception:
                pass
        # Уникализация по id+source
        seen = set(); uniq = []
        for a in results:
            k = (getattr(a, "source", "deezer"), str(getattr(a, "id", "")))
            if k in seen:
                continue
            seen.add(k); uniq.append(a)
        return jsonify([asdict(a) for a in uniq[:25]])

    @app.route("/api/charts")
    def api_charts():
        try:
            tracks = deezer.get_charts(40)
        except Exception as e:
            app.logger.warning("charts unavailable: %s", e)
            tracks = []
        return jsonify(_kids_filter_dicts([asdict(t) for t in tracks]))

    @app.route("/api/artist/<artist_id>")
    def api_artist(artist_id: str):
        a = deezer.get_artist(artist_id)
        related = deezer.get_related_artists(artist_id, 12)
        body = asdict(a)
        body["related"] = related
        body["top_tracks"] = _kids_filter_dicts(body.get("top_tracks") or [])
        return jsonify(body)

    @app.route("/api/track/<source>/<track_id>")
    def api_track(source: str, track_id: str):
        # Полные данные трека: contributors (все артисты, включая фитов) + album_id.
        # Используется клиентом, чтобы досыпать данных к state.track после старта.
        if source != "deezer":
            return jsonify({"error": "unsupported_source"}), 400
        t = deezer.get_track(track_id)
        if not t:
            return jsonify({"error": "not_found"}), 404
        d = asdict(t)
        if d.get("title"):
            from velora.api.resolver import apply_pair_override
            new_a, new_t = apply_pair_override(d.get("artist", ""), d["title"])
            d["title"] = new_t
            if new_a and new_a != d.get("artist", ""):
                d["artist"] = new_a
        return jsonify(d)

    @app.route("/api/album/<album_id>")
    def api_album(album_id: str):
        # ?meta=1 — полный объект (метаданные + треки), иначе — массив треков
        # для обратной совместимости.
        if request.args.get("meta"):
            try:
                data = deezer.get_album(album_id)
            except Exception as e:
                app.logger.warning("album meta unavailable: %s", e)
                return jsonify({"error": "not_found"}), 404
            data["tracks"] = _kids_filter_dicts(data.get("tracks") or [])
            return jsonify(data)
        return jsonify(_kids_filter_dicts([asdict(t) for t in deezer.get_album_tracks(album_id)]))

    # ----------- LYRICS --------------------------------------------------
    @app.route("/api/lyrics")
    def api_lyrics():
        artist = request.args.get("artist", "").strip()
        title = request.args.get("title", "").strip()
        album = request.args.get("album", "").strip()
        try:
            duration = int(request.args.get("duration", 0))
        except ValueError:
            duration = 0
        empty = {"variants": {}, "primary": "", "languages": [], "source": "",
                 "lines": [], "synced": False, "plain": ""}
        if not artist or not title:
            return jsonify(empty)
        raw = get_lyrics(artist, title, album, duration) or {}
        variants = raw.get("variants") or {}
        if not variants:
            return jsonify(empty)

        def _build(v: dict) -> dict:
            """Превращает {synced:[(ms,t)], plain:str} в формат фронта.

            Если синхронизации нет — делаем «псевдо-синхронизацию»: равномерно
            раскидываем непустые строки по длительности трека. Это даёт
            подсветку по строкам даже там, где у lrclib нет .lrc-разметки.
            """
            synced_pairs = v.get("synced") or []
            plain = v.get("plain") or ""
            if synced_pairs:
                lines = [{"t": (ms or 0) / 1000.0, "text": text or "", "auto": False}
                         for ms, text in synced_pairs]
                return {"lines": lines, "synced": True, "plain": plain}
            # Нет synced → авто-распределение по длительности.
            raw_lines = [s for s in (plain.splitlines()) if s.strip()]
            if not raw_lines:
                return {"lines": [], "synced": False, "plain": plain}
            # Section-маркеры [Verse: Drake], [Hook] не участвуют в распределении —
            # им присваиваем таймстемп ближайшей следующей реплики.
            import re as _re
            _sec_rx = _re.compile(r"^\s*\[[^\]]+\]\s*$")
            real_lines = [s for s in raw_lines if not _sec_rx.match(s)]
            if duration and duration > 5 and real_lines:
                pad_start, pad_end = 1.5, 2.0
                usable = max(1.0, duration - pad_start - pad_end)
                step = usable / max(1, len(real_lines))
                # Идём по raw_lines: real-строкам выдаём очередной t,
                # section-маркерам — t следующей real-строки (или предыдущей, если хвост).
                ts: list[float] = []
                ri = 0
                last_t = pad_start
                for s in raw_lines:
                    if _sec_rx.match(s):
                        # Привязываем к таймстемпу следующей реплики.
                        ts.append(pad_start + ri * step)
                    else:
                        t = pad_start + ri * step
                        ts.append(t)
                        last_t = t
                        ri += 1
                lines = [{"t": ts[i], "text": s, "auto": not _sec_rx.match(s)}
                         for i, s in enumerate(raw_lines)]
            else:
                lines = [{"t": 0, "text": s, "auto": True} for s in raw_lines]
            return {"lines": lines, "synced": True, "plain": plain, "auto_synced": True}

        out_variants = {lang: _build(v) for lang, v in variants.items()}
        primary = raw.get("primary") or next(iter(out_variants.keys()), "")
        # Совместимость с существующим клиентом: сразу плоские поля для primary-языка.
        prim = out_variants.get(primary) or {"lines": [], "synced": False, "plain": ""}
        return jsonify({
            "variants": out_variants,
            "primary": primary,
            "languages": list(out_variants.keys()),
            "source": raw.get("source", "lrclib"),
            "lines": prim["lines"],
            "synced": prim["synced"],
            "plain": prim["plain"],
            "auto_synced": prim.get("auto_synced", False),
        })

    # ----------- LIKES & HISTORY ----------------------------------------
    @app.route("/api/likes", methods=["GET", "POST", "DELETE"])
    @login_required
    def api_likes():
        if request.method == "GET":
            rows = (
                db.session.query(Like)
                .filter_by(user_id=current_user.id)
                .order_by(Like.created_at.desc())
                .limit(500)
                .all()
            )
            items = [
                {
                    "id": r.track_id, "title": r.title, "artist": r.artist,
                    "album": r.album, "cover_big": r.cover, "cover_small": r.cover,
                    "duration": r.duration, "source": r.source,
                    "artist_id": r.artist_id, "explicit": bool(r.explicit),
                }
                for r in rows
            ]
            return jsonify(_kids_filter_dicts(items))
        data = request.get_json(silent=True) or {}
        track_id = str(data.get("id") or "")
        source = data.get("source") or "deezer"
        if not track_id:
            return jsonify({"error": "id required"}), 400
        existing = (
            db.session.query(Like)
            .filter_by(user_id=current_user.id, track_id=track_id, source=source)
            .first()
        )
        if request.method == "DELETE":
            if existing:
                db.session.delete(existing)
                db.session.commit()
            return jsonify({"liked": False})
        if existing:
            return jsonify({"liked": True})
        like = Like(
            user_id=current_user.id,
            track_id=track_id,
            artist_id=str(data.get("artist_id") or ""),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            cover=data.get("cover_big") or data.get("cover_small", ""),
            duration=int(data.get("duration") or 0),
            source=source,
            explicit=bool(data.get("explicit")),
        )
        db.session.add(like)
        db.session.commit()
        return jsonify({"liked": True})

    @app.route("/api/history", methods=["GET", "POST", "DELETE"])
    @login_required
    def api_history():
        if request.method == "DELETE":
            db.session.query(HistoryEntry).filter_by(user_id=current_user.id).delete()
            db.session.commit()
            return jsonify({"ok": True})
        if request.method == "GET":
            rows = (
                db.session.query(HistoryEntry)
                .filter_by(user_id=current_user.id)
                .order_by(HistoryEntry.played_at.desc())
                .limit(200)
                .all()
            )
            items = [
                {
                    "id": r.track_id, "title": r.title, "artist": r.artist,
                    "album": r.album, "cover_big": r.cover, "cover_small": r.cover,
                    "duration": r.duration, "source": r.source,
                    "artist_id": r.artist_id, "explicit": bool(r.explicit),
                    "played_at": r.played_at.isoformat() if r.played_at else None,
                    "from_view": r.from_view or "other",
                    "play_count": r.play_count or 1,
                }
                for r in rows
            ]
            return jsonify(_kids_filter_dicts(items))
        data = request.get_json(silent=True) or {}
        if not data.get("id"):
            return jsonify({"ok": False}), 400
        return _record_history(data)

    def _record_history(data: dict):
        """POST в историю с дедупом: если последняя запись та же, обновляем play_count
        и timestamp вместо создания нового ряда. Так история не превращается в спам
        одинаковых треков от ререндеров клиента."""
        track_id = str(data.get("id"))
        source = data.get("source", "deezer")
        from_view = (data.get("from_view") or "other")[:32]
        last = (
            db.session.query(HistoryEntry)
            .filter_by(user_id=current_user.id)
            .order_by(HistoryEntry.played_at.desc())
            .first()
        )
        now = datetime.utcnow()
        if last and last.track_id == track_id and last.source == source:
            # Тот же трек подряд — наращиваем счётчик, обновляем время.
            last.play_count = (last.play_count or 1) + 1
            last.played_at = now
            if from_view and from_view != "other":
                last.from_view = from_view
            db.session.commit()
            return jsonify({"ok": True, "merged": True})
        h = HistoryEntry(
            user_id=current_user.id,
            track_id=track_id,
            artist_id=str(data.get("artist_id") or ""),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            cover=data.get("cover_big") or data.get("cover_small", ""),
            duration=int(data.get("duration") or 0),
            source=source,
            explicit=bool(data.get("explicit")),
            from_view=from_view,
            play_count=1,
            played_at=now,
        )
        db.session.add(h)
        db.session.commit()
        return jsonify({"ok": True, "merged": False})

    # Алиас: клиент исторически POST'ит в /api/listen
    @app.route("/api/listen", methods=["POST"])
    @login_required
    def api_listen():
        data = request.get_json(silent=True) or {}
        if not data.get("id"):
            return jsonify({"ok": False}), 400
        return _record_history(data)

    # ----------- ВОЛНА ---------------------------------------------------
    @app.route("/api/wave")
    @login_required
    def api_wave():
        """Персональная волна.

        Алгоритм:
          1. Собираем «вес» каждого артиста из нескольких сигналов:
             - последние 80 лайков (×3, недавние ×4)
             - история прослушиваний за 60 дней (вес = play_count, недавние ×2)
             - явные ArtistPref(kind=like)  — ×20 (сильнейший сигнал)
          2. Берём топ-15 артистов по весу → делаем seed-set.
          3. Для каждого seed-артиста параллельно тянем related (по 6) и
             top_tracks (по 8). Related-артисты дают «расширение вкуса»,
             их top-1..2 идут в волну.
          4. Анти-повторы: исключаем дизлайкнутые треки и забаненных
             артистов; не более 2 треков на одного артиста; исключаем то,
             что было сыграно за последние 24 часа.
          5. Перемешиваем результат с лёгким смещением — самые релевантные
             (от seed-артистов) распределены по всей выдаче, а не пачкой.
        """
        from collections import Counter
        import random
        import math

        try:
            limit = max(10, min(int(request.args.get("limit") or 40), 80))
        except (TypeError, ValueError):
            limit = 40

        # Tune-параметры из настроек волны (приходят с фронта в live-режиме).
        tune_occupy = (request.args.get("occupy") or "").strip()
        tune_char = (request.args.get("char") or "").strip()
        tune_mood = (request.args.get("mood") or "").strip()
        tune_lang = (request.args.get("lang") or "").strip()

        # Серверный кэш волны на пользователя — иначе каждый заход на главную
        # дёргает 8+ deezer-эндпоинтов параллельно (медленно). Кэш 5 минут;
        # ?fresh=1 (или кнопка «Обновить») делает invalidate. Ключ кэша
        # учитывает все tune-параметры, чтобы переключение настроек давало
        # разный результат сразу же, без перезагрузки страницы.
        fresh = request.args.get("fresh") in ("1", "true")
        cache_attr = "_velora_wave_cache"
        if not hasattr(app, cache_attr):
            setattr(app, cache_attr, {})
        wave_cache: dict = getattr(app, cache_attr)
        ck = (current_user.id, limit, tune_occupy, tune_char, tune_mood, tune_lang)
        if not fresh:
            v = wave_cache.get(ck)
            if v and v[0] > datetime.utcnow():
                return jsonify(v[1])

        now = datetime.utcnow()

        likes = (
            db.session.query(Like).filter_by(user_id=current_user.id)
            .order_by(Like.created_at.desc()).limit(80).all()
        )
        prefs = (
            db.session.query(ArtistPref).filter_by(user_id=current_user.id).all()
        )
        liked_pref_ids = [p.artist_id for p in prefs if p.kind == "like" and p.artist_id]
        disliked_pref_ids = {p.artist_id for p in prefs if p.kind == "dislike" and p.artist_id}

        # История за 60 дней — главный сигнал «реальных» вкусов.
        since = now - timedelta(days=60)
        hist = (
            db.session.query(HistoryEntry)
            .filter(HistoryEntry.user_id == current_user.id,
                    HistoryEntry.played_at >= since,
                    HistoryEntry.artist_id.isnot(None))
            .order_by(HistoryEntry.played_at.desc())
            .limit(500)
            .all()
        )

        if not likes and not liked_pref_ids and not hist:
            return jsonify(_kids_filter_dicts([asdict(t) for t in deezer.get_charts(limit)]))

        # ---- 1. Веса артистов --------------------------------------------
        weights: Counter = Counter()
        for i, lk in enumerate(likes):
            if not lk.artist_id:
                continue
            weights[lk.artist_id] += 4 if i < 20 else 3
        for i, h in enumerate(hist):
            if not h.artist_id:
                continue
            base = max(1, int(h.play_count or 1))
            # Свежее (первые 50 записей) — двойной вес.
            mult = 2 if i < 50 else 1
            weights[h.artist_id] += base * mult
        for aid in liked_pref_ids:
            weights[aid] += 20

        # Дополнительный сигнал из снимка предпочтений (TasteSnapshot):
        # часто посещаемые артисты / альбомы дают доп. вес. Снимок
        # пересчитывается лениво (TTL ~2ч), поэтому стоимость низкая.
        try:
            snap = _taste.get_or_refresh_snapshot(current_user.id)
            for a in snap.get("frequent_artists", []) or []:
                aid = a.get("id")
                if aid:
                    weights[aid] += 3 * int(a.get("w") or 1)
            for al in snap.get("frequent_albums", []) or []:
                aid = al.get("artist_id") or ""
                if aid:
                    weights[aid] += int(al.get("w") or 1)
            # Артисты-дизлайки из снимка тоже учитываем.
            for a in snap.get("artists_dislike", []) or []:
                aid = a.get("id")
                if aid:
                    disliked_pref_ids.add(aid)
        except Exception:
            pass

        # Убираем дизлайкнутых артистов из seed-набора.
        for aid in disliked_pref_ids:
            weights.pop(aid, None)

        # ---- 2. Seed-артисты + related ----------------------------------
        # Уменьшено с 15 → 8 для скорости (8 артистов уже дают ~80 кандидатов).
        seeds = [aid for aid, _ in weights.most_common(8)]
        # Если seed мало — добиваем чартами (id артистов из чартов).
        if len(seeds) < 5:
            try:
                charts = deezer.get_charts(40)
                for t in charts:
                    aid = getattr(t, "artist_id", "") or ""
                    if aid and aid not in seeds and aid not in disliked_pref_ids:
                        seeds.append(aid)
                    if len(seeds) >= 8:
                        break
            except Exception:
                pass

        # Параллельно тянем related для каждого seed.
        related_pool: list[str] = []

        def _fetch_related(aid: str) -> list[str]:
            try:
                rel = deezer.get_related_artists(aid, 4)
                return [r["id"] for r in rel if r.get("id")]
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=12) as ex:
            for ids in ex.map(_fetch_related, seeds):
                related_pool.extend(ids)

        # Объединяем кандидатов: seed-артисты в начале (их вкус ближе),
        # затем related (расширение). Дедуп.
        candidate_artists: list[str] = []
        seen = set()
        for aid in seeds + related_pool:
            if aid and aid not in seen and aid not in disliked_pref_ids:
                seen.add(aid)
                candidate_artists.append(aid)

        # Чуть-чуть перемешаем середину/хвост, чтобы порядок не был статичным.
        if len(candidate_artists) > 6:
            tail = candidate_artists[6:]
            random.shuffle(tail)
            candidate_artists = candidate_artists[:6] + tail

        # ---- 3. Загружаем топ-треки артистов ----------------------------
        liked_track_ids = {lk.track_id for lk in likes}
        ds = db.session.query(Dislike).filter_by(user_id=current_user.id).all()
        banned_tracks = {d.track_id for d in ds if d.scope == "track" and d.track_id}
        banned_artists = {d.artist_id for d in ds if d.scope == "artist" and d.artist_id}
        banned_artists |= disliked_pref_ids

        # Что сыграно за последние 24 часа — не повторяем в волне.
        day_ago = now - timedelta(hours=24)
        recent_played = {
            r[0] for r in db.session.query(HistoryEntry.track_id)
            .filter(HistoryEntry.user_id == current_user.id,
                    HistoryEntry.played_at >= day_ago)
            .all() if r[0]
        }

        per_artist_cap = 2  # не более 2 треков от одного артиста
        wave: list = []
        per_artist: Counter = Counter()
        target = max(limit, 30)

        def _fetch_top(aid: str):
            try:
                return aid, deezer.get_top_tracks(aid, 6)
            except Exception:
                return aid, []

        # Параллельная загрузка топ-треков (12 потоков), порциями по 16 артистов
        # — чтобы не делать 50+ синхронных запросов сразу.
        idx = 0
        while idx < len(candidate_artists) and len(wave) < target:
            batch = candidate_artists[idx: idx + 16]
            idx += 16
            with ThreadPoolExecutor(max_workers=12) as ex:
                results = list(ex.map(_fetch_top, batch))
            # Перемешиваем порядок артистов внутри batch.
            random.shuffle(results)
            for aid, top in results:
                if aid in banned_artists:
                    continue
                # У каждого артиста берём чуть рандомизированный набор:
                # из топ-8 случайно выбираем 4 и сортируем по релевантности.
                pool = [t for t in top if t and t.id and t.id not in banned_tracks
                        and t.id not in liked_track_ids and t.id not in recent_played]
                if not pool:
                    continue
                pick = pool[:4]  # верхние 4 — лучшие хиты
                random.shuffle(pick)
                taken = 0
                for t in pick:
                    if per_artist[aid] >= per_artist_cap:
                        break
                    wave.append(t)
                    per_artist[aid] += 1
                    taken += 1
                    if len(wave) >= target:
                        break
                if len(wave) >= target:
                    break

        # ---- 4. Финальный шафл с приоритетом seed-артистов --------------
        seed_set = set(seeds)
        # Делим на «from seed» и «from related», в выдачу — чередуем.
        seed_tracks = [t for t in wave if t.artist_id in seed_set]
        rel_tracks = [t for t in wave if t.artist_id not in seed_set]
        random.shuffle(seed_tracks)
        random.shuffle(rel_tracks)
        merged: list = []
        # Соотношение примерно 60/40 в пользу seed.
        si = ri = 0
        while si < len(seed_tracks) or ri < len(rel_tracks):
            for _ in range(2):
                if si < len(seed_tracks):
                    merged.append(seed_tracks[si]); si += 1
            if ri < len(rel_tracks):
                merged.append(rel_tracks[ri]); ri += 1

        result = _kids_filter_dicts([asdict(t) for t in merged[:limit]])

        # ---- 5. Применяем tune-фильтры (occupy/char/mood/lang) -----------
        # Фильтры применяются ПОСТ-фактум: режут / переупорядочивают треки.
        # Это даёт live-эффект без пересборки seed-набора.
        result = _apply_wave_tune(result, tune_occupy, tune_char, tune_mood, tune_lang)

        # Сохраняем в кэш на 5 минут.
        wave_cache[ck] = (datetime.utcnow() + timedelta(minutes=5), result)
        return jsonify(result)

    # =================================================================
    # /api/visit — инкремент счётчика посещений (артист/альбом/плейлист).
    # =================================================================
    @app.route("/api/visit", methods=["POST"])
    @login_required
    def api_visit():
        data = request.get_json(silent=True) or {}
        kind = (data.get("kind") or "").strip()
        target_id = str(data.get("id") or "").strip()
        if kind not in {"artist", "album", "playlist", "track"} or not target_id:
            return jsonify({"error": "bad_request"}), 400
        _taste.record_visit(
            user_id=current_user.id,
            kind=kind,
            target_id=target_id,
            source=(data.get("source") or "deezer"),
            name=(data.get("name") or "")[:255],
            artist=(data.get("artist") or "")[:255],
            cover=(data.get("cover") or "")[:512],
        )
        return jsonify({"ok": True})

    # =================================================================
    # /api/taste/snapshot — отдаёт текущий снимок (для отладки/UI).
    # =================================================================
    @app.route("/api/taste/snapshot")
    @login_required
    def api_taste_snapshot():
        force = request.args.get("fresh") in ("1", "true")
        snap = _taste.get_or_refresh_snapshot(current_user.id, force=force)
        return jsonify(snap)

    # =================================================================
    # /api/taste/palette — палитра акцентных цветов под жанр пользователя.
    # =================================================================
    @app.route("/api/taste/palette")
    @login_required
    def api_taste_palette():
        try:
            return jsonify(_taste.compute_user_palette(current_user.id))
        except Exception as e:
            app.logger.warning("palette failed: %s", e)
            return jsonify({
                "genre": "",
                "genre_raw": "",
                "palette": {"accent": "#ffd84a", "accent2": "#ff5b9c", "accent3": "#b46bff"},
                "breakdown": {},
            })

    # =================================================================
    # /api/recommend/for-you — секция «Возможно вам понравится»,
    # построенная из снимка предпочтений.
    # =================================================================
    @app.route("/api/recommend/for-you")
    @login_required
    def api_for_you():
        try:
            limit = max(8, min(int(request.args.get("limit") or 30), 60))
        except (TypeError, ValueError):
            limit = 30

        snap = _taste.get_or_refresh_snapshot(current_user.id)
        seeds = list(_taste.weighted_artist_seeds(snap, top_n=10).keys())
        deny_tracks, deny_artists = _taste.denylist(snap)

        if not seeds:
            return jsonify(_kids_filter_dicts([asdict(t) for t in deezer.get_charts(limit)]))

        # Кандидаты: related артистов из seed (broaden taste) + сами seed.
        related: list[str] = []

        def _rel(aid: str) -> list[str]:
            try:
                return [r["id"] for r in deezer.get_related_artists(aid, 4) if r.get("id")]
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=10) as ex:
            for ids in ex.map(_rel, seeds):
                related.extend(ids)

        candidates: list[str] = []
        seen: set[str] = set()
        for aid in seeds + related:
            if aid and aid not in seen and aid not in deny_artists:
                seen.add(aid)
                candidates.append(aid)

        # Уже знакомые треки (лайкнутые / сыгранные за сутки) пропускаем —
        # секция должна открывать новое.
        liked = {t.get("id") for t in (snap.get("tracks_like") or []) if t.get("id")}
        recent = {
            r[0] for r in db.session.query(HistoryEntry.track_id)
            .filter(HistoryEntry.user_id == current_user.id,
                    HistoryEntry.played_at >= datetime.utcnow() - timedelta(hours=24))
            .all() if r[0]
        }
        skip = liked | recent | deny_tracks

        def _top(aid: str):
            try:
                return aid, deezer.get_top_tracks(aid, 5)
            except Exception:
                return aid, []

        result_tracks: list = []
        per_artist: dict = {}
        with ThreadPoolExecutor(max_workers=12) as ex:
            for aid, tops in ex.map(_top, candidates[:24]):
                if aid in deny_artists:
                    continue
                for t in tops:
                    if not t or not t.id or t.id in skip:
                        continue
                    if per_artist.get(aid, 0) >= 2:
                        break
                    result_tracks.append(t)
                    per_artist[aid] = per_artist.get(aid, 0) + 1
                    if len(result_tracks) >= limit:
                        break
                if len(result_tracks) >= limit:
                    break

        import random as _rnd
        _rnd.shuffle(result_tracks)
        return jsonify(_kids_filter_dicts([asdict(t) for t in result_tracks[:limit]]))

    # =================================================================
    # /api/discover/feed — стартовая лента поиска: персональные подборки,
    # рекомендуемые артисты, треки. Работает и для гостя (без login_required),
    # просто использует чарты/популярных артистов вместо истории.
    # =================================================================
    @app.route("/api/discover/feed")
    def api_discover_feed():
        """Возвращает {playlists:[...], artists:[...], tracks:[...]}.

        Алгоритм для зарегистрированных:
          1. Берём топ артистов из истории/лайков (как в /api/wave).
          2. Для топ-8 формируем плейлист «По вкусу: {Artist}» из 25 треков:
             top_tracks(self, 12) + top_tracks(related[0], 7) + top(related[1], 6).
          3. + плейлист «Чарты сейчас» (25 первых из чартов).
          4. + плейлист «Открытие недели» (по 2-3 трека от related artists).
          5. artists[]: топ-12 артистов из seeds + related (с фото).
          6. tracks[]: 25 треков, перемешанные из плейлистов 2-4.
        Для гостя: 10 плейлистов = 1 чарт + 9 «Жанровые миксы» (по seed-артистам из чартов).
        """
        from collections import Counter
        import random

        from velora.api import deezer

        try:
            playlist_count = max(3, min(int(request.args.get("playlists") or 10), 12))
        except (TypeError, ValueError):
            playlist_count = 10

        seeds: list[str] = []
        seed_names: dict[str, str] = {}
        seed_pics: dict[str, str] = {}
        is_guest = not getattr(current_user, "is_authenticated", False)

        if not is_guest:
            likes = (db.session.query(Like)
                     .filter_by(user_id=current_user.id)
                     .order_by(Like.created_at.desc()).limit(60).all())
            since = datetime.utcnow() - timedelta(days=90)
            hist = (db.session.query(HistoryEntry)
                    .filter(HistoryEntry.user_id == current_user.id,
                            HistoryEntry.played_at >= since,
                            HistoryEntry.artist_id.isnot(None))
                    .order_by(HistoryEntry.played_at.desc()).limit(400).all())
            prefs = db.session.query(ArtistPref).filter_by(user_id=current_user.id).all()
            disliked = {p.artist_id for p in prefs if p.kind == "dislike" and p.artist_id}
            liked_pref_ids = [p.artist_id for p in prefs if p.kind == "like" and p.artist_id]

            weights: Counter = Counter()
            for i, lk in enumerate(likes):
                if not lk.artist_id:
                    continue
                weights[lk.artist_id] += 4 if i < 20 else 2
                if lk.artist:
                    seed_names.setdefault(lk.artist_id, lk.artist)
            for i, h in enumerate(hist):
                if not h.artist_id:
                    continue
                weights[h.artist_id] += int(h.play_count or 1) * (2 if i < 50 else 1)
                if h.artist:
                    seed_names.setdefault(h.artist_id, h.artist)
            for aid in liked_pref_ids:
                weights[aid] += 20
            for aid in disliked:
                weights.pop(aid, None)

            seeds = [aid for aid, _ in weights.most_common(8)]

        # Если гость или нет истории — добиваем чартами.
        if len(seeds) < playlist_count - 2:
            try:
                charts = deezer.get_charts(50)
                for t in charts:
                    aid = getattr(t, "artist_id", "") or ""
                    if not aid or aid in seeds:
                        continue
                    seeds.append(aid)
                    if getattr(t, "artist", ""):
                        seed_names.setdefault(aid, t.artist)
                    if getattr(t, "cover_small", "") or getattr(t, "cover_big", ""):
                        seed_pics.setdefault(aid, t.cover_big or t.cover_small)
                    if len(seeds) >= playlist_count + 4:
                        break
            except Exception:
                pass

        # ---- Параллельная загрузка top + related для каждого seed ----
        def _fetch_artist_pack(aid: str):
            try:
                top = deezer.get_top_tracks(aid, 14)
                rel = deezer.get_related_artists(aid, 4)
                return aid, top, rel
            except Exception:
                return aid, [], []

        def _fetch_top(aid: str):
            try:
                return aid, deezer.get_top_tracks(aid, 8)
            except Exception:
                return aid, []

        packs: dict[str, tuple[list, list]] = {}
        related_top: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            for aid, top, rel in ex.map(_fetch_artist_pack, seeds[:playlist_count]):
                packs[aid] = (top, rel)
                if top and not seed_pics.get(aid):
                    seed_pics[aid] = getattr(top[0], "cover_big", "") or getattr(top[0], "cover_small", "")
                if top and not seed_names.get(aid):
                    seed_names[aid] = getattr(top[0], "artist", "") or "Артист"

        # related_pool — ID-ы со всех seed-related (для «Открытие» и подсказок артистов)
        related_pool: list[dict] = []
        related_seen: set[str] = set()
        for aid, (_top, rel) in packs.items():
            for r in rel:
                rid = r.get("id")
                if rid and rid not in related_seen and rid not in seeds:
                    related_seen.add(rid)
                    related_pool.append(r)

        # Для «Открытие недели» возьмём топ-12 related, по 2 трека.
        discover_seed_ids = [r["id"] for r in related_pool[:14]]
        with ThreadPoolExecutor(max_workers=10) as ex:
            for aid, top in ex.map(_fetch_top, discover_seed_ids):
                if top:
                    related_top[aid] = top

        # Чарты — для отдельного плейлиста.
        try:
            charts_tracks = deezer.get_charts(30)
        except Exception:
            charts_tracks = []

        # ---- Сборка плейлистов ----
        playlists: list[dict] = []

        def _pl(name: str, cover: str, tracks: list, key: str, subtitle: str = ""):
            tr = [asdict(t) for t in tracks if t and getattr(t, "id", None)]
            tr = _kids_filter_dicts(tr)[:25]
            if not tr:
                return
            playlists.append({
                "id": f"auto:{key}",
                "name": name,
                "subtitle": subtitle or f"{len(tr)} треков",
                "cover": cover or (tr[0].get("cover_big") or tr[0].get("cover_small") or ""),
                "tracks": tr,
                "auto": True,
            })

        # 1) Чарты
        _pl("Чарты сейчас", "", charts_tracks, "charts", "Самое популярное")

        # 2) По вкусу: {Artist}
        for aid in seeds[:playlist_count - 2]:
            top, rel = packs.get(aid, ([], []))
            if not top:
                continue
            mix: list = list(top[:12])
            # Добиваем релейтед-треками (по 7+6 от двух соседей).
            for ri, r in enumerate(rel[:2]):
                rid = r.get("id")
                if not rid:
                    continue
                rt = related_top.get(rid) or []
                take = 7 if ri == 0 else 6
                mix.extend(rt[:take])
            seen_ids: set = set()
            uniq = []
            for t in mix:
                tid = getattr(t, "id", None)
                if tid and tid not in seen_ids:
                    seen_ids.add(tid); uniq.append(t)
            random.shuffle(uniq)
            name = seed_names.get(aid, "Любимый артист")
            cover = seed_pics.get(aid) or ""
            _pl(f"По вкусу: {name}", cover, uniq, f"vibe-{aid}", f"Микс по {name}")
            if len(playlists) >= playlist_count - 1:
                break

        # 3) Открытие недели — по 2 трека от related artists.
        discover: list = []
        per_a: Counter = Counter()
        for r in related_pool[:30]:
            rid = r.get("id")
            if not rid:
                continue
            for t in (related_top.get(rid) or [])[:3]:
                if per_a[rid] >= 2:
                    break
                discover.append(t); per_a[rid] += 1
            if len(discover) >= 25:
                break
        random.shuffle(discover)
        _pl("Открытие недели", "", discover, "discover", "Новые имена под ваш вкус")

        # ---- Артисты для подсказки ----
        artists_out: list[dict] = []
        seen_a: set = set()
        # Сначала seed-артисты (фото из top-track).
        for aid in seeds:
            if aid in seen_a:
                continue
            seen_a.add(aid)
            artists_out.append({
                "id": aid,
                "name": seed_names.get(aid, ""),
                "image": seed_pics.get(aid, ""),
                "source": "deezer",
            })
        # Потом related.
        for r in related_pool:
            rid = r.get("id")
            if rid in seen_a:
                continue
            seen_a.add(rid)
            artists_out.append({
                "id": rid,
                "name": r.get("name", ""),
                "image": r.get("picture", ""),
                "source": "deezer",
            })
            if len(artists_out) >= 14:
                break

        # ---- «Просто треки»: микс из чартов + плейлистов ----
        tracks_pool: list = list(charts_tracks[:15])
        for pl in playlists[1:]:
            for t in pl["tracks"][:5]:
                tracks_pool.append(t)
        # Дедуп.
        seen_tids: set = set()
        tracks_out: list = []
        for t in tracks_pool:
            tid = t.get("id") if isinstance(t, dict) else getattr(t, "id", None)
            if not tid or tid in seen_tids:
                continue
            seen_tids.add(tid)
            tracks_out.append(t if isinstance(t, dict) else asdict(t))
        random.shuffle(tracks_out)

        return jsonify({
            "playlists": playlists[:playlist_count],
            "artists": artists_out,
            "tracks": _kids_filter_dicts(tracks_out)[:25],
            "guest": is_guest,
        })

    # ----------- ПЛЕЙЛИСТЫ -----------------------------------------------
    @app.route("/api/playlists", methods=["GET", "POST"])
    @login_required
    def api_playlists():
        if request.method == "GET":
            pls = (
                db.session.query(Playlist)
                .filter_by(user_id=current_user.id)
                .order_by(Playlist.pinned.desc(), Playlist.id.desc())
                .all()
            )
            return jsonify([_serialize_playlist(p, include_items=False) for p in pls])
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip() or "Новый плейлист"
        p = Playlist(user_id=current_user.id, name=name,
                     description=(data.get("description") or "").strip(),
                     slug=_make_unique_pl_slug(name))
        db.session.add(p)
        db.session.commit()
        return jsonify(_serialize_playlist(p))

    @app.route("/api/playlists/<int:pid>", methods=["GET", "DELETE", "PATCH"])
    @login_required
    def api_playlist_detail(pid: int):
        p = db.session.get(Playlist, pid)
        if not p or p.user_id != current_user.id:
            abort(404)
        if request.method == "DELETE":
            db.session.delete(p)
            db.session.commit()
            return jsonify({"ok": True})
        if request.method == "PATCH":
            data = request.get_json(silent=True) or {}
            if "name" in data:
                new_name = (data.get("name") or "").strip()[:120] or p.name
                if new_name != p.name:
                    p.name = new_name
                    if not p.slug:
                        p.slug = _make_unique_pl_slug(new_name)
            if "description" in data:
                p.description = (data.get("description") or "").strip()[:500]
            if "cover" in data:
                v = _validate_image(data.get("cover") or "")
                p.cover = v if data.get("cover") else None
            if "pinned" in data:
                p.pinned = bool(data.get("pinned"))
            if "is_public" in data:
                p.is_public = bool(data.get("is_public"))
                if p.is_public and not p.slug:
                    p.slug = _make_unique_pl_slug(p.name)
            db.session.commit()
            return jsonify(_serialize_playlist(p, include_items=False))
        body = _serialize_playlist(p)
        if current_user.is_authenticated and current_user.kids_mode:
            body["items"] = [it for it in body["items"] if not it.get("explicit")]
        return jsonify(body)

    @app.route("/api/playlists/<int:pid>/add", methods=["POST"])
    @login_required
    def api_playlist_add(pid: int):
        p = db.session.get(Playlist, pid)
        if not p or p.user_id != current_user.id:
            abort(404)
        data = request.get_json(silent=True) or {}
        # принимаем как один трек, так и массив треков
        tracks = data.get("tracks") if isinstance(data.get("tracks"), list) else [data]
        added = 0
        base_pos = len(p.items)
        for i, t in enumerate(tracks):
            if not t.get("id"):
                continue
            db.session.add(PlaylistItem(
                playlist_id=p.id,
                track_id=str(t.get("id")),
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                album=t.get("album", ""),
                cover=t.get("cover_big") or t.get("cover_small", ""),
                duration=int(t.get("duration") or 0),
                source=t.get("source", "deezer"),
                explicit=bool(t.get("explicit")),
                position=base_pos + i,
            ))
            added += 1
        db.session.commit()
        return jsonify({"ok": True, "added": added, "count": len(p.items)})

    @app.route("/api/playlists/<int:pid>/items/<int:row_id>", methods=["DELETE"])
    @login_required
    def api_playlist_remove(pid: int, row_id: int):
        p = db.session.get(Playlist, pid)
        if not p or p.user_id != current_user.id:
            abort(404)
        it = db.session.get(PlaylistItem, row_id)
        if not it or it.playlist_id != p.id:
            abort(404)
        db.session.delete(it)
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/playlists/<int:pid>/trailer")
    @login_required
    def api_playlist_trailer(pid: int):
        """Список треков для плеера-трейлера: первые до 10 разрешённых треков плейлиста."""
        p = db.session.get(Playlist, pid)
        if not p or p.user_id != current_user.id:
            abort(404)
        items = [_serialize_pitem(it) for it in p.items[:10]]
        if current_user.kids_mode:
            items = [t for t in items if not t.get("explicit")]
        return jsonify({"items": items, "snippet_seconds": 25})

    # ----------- ИМПОРТ --------------------------------------------------
    # Память job'ов импорта (ключ — uuid). Хранит словарь со счётчиками
    # и последней обработанной строкой. Очищается через 30 минут после finish.
    # Под uWSGI работает несколько воркеров → словарь в памяти не виден из
    # соседнего процесса (отсюда был 404 на /api/import/status). Поэтому
    # дублируем состояние на диск под Config.UPLOAD_DIR/import_jobs/<id>.json.
    if not hasattr(app, "_velora_import_jobs"):
        app._velora_import_jobs = {}

    def _jobs_dir() -> Path:
        d = Config.UPLOAD_DIR / "import_jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _job_path(job_id: str) -> Path:
        # ограничиваем символы — только hex/uuid из api_import_file.
        safe = re.sub(r"[^0-9a-fA-F]", "", job_id)[:64]
        return _jobs_dir() / f"{safe}.json"

    def _job_save(job_id: str, job: dict) -> None:
        try:
            p = _job_path(job_id)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:
            app.logger.warning("import job save failed %s: %s", job_id, exc)

    def _job_load(job_id: str) -> dict | None:
        try:
            p = _job_path(job_id)
            if not p.exists():
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            app.logger.warning("import job load failed %s: %s", job_id, exc)
            return None

    def _job_delete(job_id: str) -> None:
        try:
            _job_path(job_id).unlink(missing_ok=True)
        except Exception:
            pass

    def _job_cleanup_old() -> None:
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        try:
            for fp in _jobs_dir().glob("*.json"):
                try:
                    if datetime.utcfromtimestamp(fp.stat().st_mtime) < cutoff:
                        fp.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _import_score(req_artist: str, req_title: str, hit_artist: str, hit_title: str) -> float:
        """Грубая оценка соответствия (req artist/title) ↔ найденного трека.

        Возвращает значение 0..3. Token-set overlap по обеим частям.
        Артист весит вдвое: «John Lennon — Imagine» != «Some Cover — Imagine».
        Если запрос без артиста — оцениваем только заголовок.
        """
        def _toks(s: str) -> set[str]:
            s = re.sub(r"[^\w\s]+", " ", (s or "").lower(), flags=re.UNICODE)
            return {w for w in s.split() if len(w) > 1}
        qt, qa = _toks(req_title), _toks(req_artist)
        ht, ha = _toks(hit_title), _toks(hit_artist)
        if not qt:
            return 0.0
        title_ov = len(qt & ht) / max(len(qt), 1)
        artist_ov = (len(qa & ha) / max(len(qa), 1)) if qa else 1.0
        # Бонус, если запрос целиком — подмножество найденного.
        if qt and qt.issubset(ht):
            title_ov = max(title_ov, 1.0)
        return artist_ov * 2.0 + title_ov  # макс 3.0

    def _import_resolve_one(req_artist: str, req_title: str, kids_mode: bool) -> Track | None:
        """Ищет лучший Deezer-кандидат для пары (artist, title).

        Делает несколько запросов и берёт top-5 каждого, выбирает максимум
        по `_import_score`. Возвращает None, если score < 1.5 (порог
        «достаточной похожести»).
        """
        # Нормализация: вырезаем всё, что обычно не совпадает между источниками
        # (Spotify vs Deezer): «(feat. X)», «(Remastered 2011)», «[Explicit]»,
        # «- Single Version», «- 2014 Mix» и т.п.
        def _strip_brackets(s: str) -> str:
            markers = (
                "feat", "ft.", "ft ", "remaster", "remix", "version", "edit",
                "deluxe", "bonus", "anniversary", "explicit", "live",
                "mono", "stereo", "instrumental", "acoustic", "demo",
                "single", "album", "radio",
            )
            def _ok(content: str) -> bool:
                low = content.lower()
                if any(m in low for m in markers):
                    return False
                if re.search(r"\b(19|20)\d{2}\b", low):  # год
                    return False
                return True
            # удаляем содержимое скобок с маркерами
            s = re.sub(r"[\(\[][^\(\)\[\]]*[\)\]]",
                       lambda m: m.group(0) if _ok(m.group(0)[1:-1]) else " ", s)
            # удаляем хвост вида " - <что угодно с маркером>"
            parts = re.split(r"\s+[-–—]\s+", s)
            kept = [parts[0]] + [p for p in parts[1:]
                                 if not any(m in p.lower() for m in markers)]
            s = " - ".join(kept)
            return re.sub(r"\s+", " ", s).strip(" -–—")
        req_artist = (req_artist or "").strip()
        req_title = (req_title or "").strip()
        norm_title = _strip_brackets(req_title)
        norm_artist = _strip_brackets(req_artist)
        queries: list[str] = []
        seen_q: set[str] = set()
        def _add(q: str) -> None:
            q = (q or "").strip()
            if q and q.lower() not in seen_q:
                seen_q.add(q.lower())
                queries.append(q)
        if req_artist and req_title:
            _add(f"{req_artist} {req_title}")
            _add(f'artist:"{req_artist}" track:"{req_title}"')
            _add(f"{norm_artist} {norm_title}")
            _add(norm_title)
            _add(req_title)
        elif req_title:
            _add(req_title)
            _add(norm_title)
        if not queries:
            return None
        best: Track | None = None
        best_score = -1.0
        for q in queries:
            try:
                hits = deezer.search_tracks(q, 5)
            except Exception:
                hits = []
            for t in hits:
                if kids_mode and t.explicit:
                    continue
                s = _import_score(req_artist, req_title, t.artist, t.title)
                if s > best_score:
                    best_score = s
                    best = t
            # Если уже нашли уверенное совпадение — не тратим квоту deezer на остальные q.
            if best_score >= 2.5:
                break
        # Пороги: с артистом ≥1.2 (мягче — feat./remaster в названии часто
        # снижают title overlap, мы не хотим выкидывать треки из-за этого).
        # Без артиста (qa пуст) — artist_ov=1.0, нужен title overlap ≥0.4 → score ≥1.8.
        # Раньше пороги 1.5/2.5 давали 20%+ skipped на больших импортах.
        threshold = 1.2 if req_artist else 1.8
        if best is not None and best_score >= threshold:
            return best
        # ---- Fallback через iTunes: берём «правильное» написание
        # (Apple очень терпим к опечаткам/транслиту) и снова ищем в Deezer.
        try:
            from velora.api import itunes as _it
            it_hits = _it.search_tracks(
                (norm_artist + " " + norm_title).strip() or norm_title or req_title, 3
            )
        except Exception:
            it_hits = []
        for it in it_hits:
            if kids_mode and it.explicit:
                continue
            try:
                hits = deezer.search_tracks(f"{it.artist} {it.title}", 5)
            except Exception:
                hits = []
            for t in hits:
                if kids_mode and t.explicit:
                    continue
                s = _import_score(req_artist or it.artist, req_title or it.title,
                                  t.artist, t.title)
                if s > best_score:
                    best_score = s
                    best = t
            if best_score >= 2.5:
                break
        if best is not None and best_score >= threshold:
            return best
        return None

    def _import_run(job_id: str, user_id: int, kids_mode: bool, pairs: list[tuple[str, str]],
                    playlist_name: str, source_name: str) -> None:
        """Фоновая обработка импорта. Запускается из отдельного потока."""
        with app.app_context():
            job = app._velora_import_jobs[job_id]
            try:
                p = Playlist(user_id=user_id, name=playlist_name[:120],
                             description="Импортировано из " + source_name)
                db.session.add(p)
                db.session.commit()
                job["playlist_id"] = p.id
                _job_save(job_id, job)
                added = 0
                skipped = 0
                for i, (artist, title) in enumerate(pairs):
                    # cancel может прийти из другого воркера — подхватываем флаг с диска.
                    on_disk = _job_load(job_id) or {}
                    if on_disk.get("cancelled") or job.get("cancelled"):
                        job["cancelled"] = True
                        break
                    job["index"] = i + 1
                    job["current"] = (artist + " — " + title).strip(" —") if artist else title
                    t = None
                    try:
                        t = _import_resolve_one(artist, title, kids_mode)
                    except Exception as exc:
                        app.logger.warning("import resolve err: %s", exc)
                    if t is None:
                        skipped += 1
                        job["skipped"] = skipped
                        # Сохраним первые 50 пропущенных строк для UI.
                        if len(job["skipped_lines"]) < 50:
                            job["skipped_lines"].append(job["current"])
                        _job_save(job_id, job)
                        continue
                    db.session.add(PlaylistItem(
                        playlist_id=p.id,
                        track_id=t.id, title=t.title, artist=t.artist, album=t.album,
                        cover=t.cover_big or t.cover_small,
                        duration=t.duration, source=t.source, explicit=t.explicit,
                        position=i,
                    ))
                    added += 1
                    job["added"] = added
                    if added % 25 == 0:
                        try: db.session.commit()
                        except Exception: db.session.rollback()
                    _job_save(job_id, job)
                try: db.session.commit()
                except Exception: db.session.rollback()
                job["status"] = "done"
            except Exception as exc:
                app.logger.exception("import job %s crashed: %s", job_id, exc)
                job["status"] = "error"
                job["error"] = str(exc)
            finally:
                job["finished_at"] = datetime.utcnow().isoformat()
                _job_save(job_id, job)

    @app.route("/api/import/file", methods=["POST"])
    @login_required
    def api_import_file():
        """Запускает фоновый импорт из текстового файла. Возвращает job_id;
        прогресс получать через /api/import/status?id=…"""
        import uuid as _uuid
        import threading
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "file required"}), 400
        text = _extract_text_from_upload(f.filename or "", f.read())
        pairs = _parse_imported_lines(text)[:5000]
        if not pairs:
            return jsonify({"error": "no parsable lines"}), 400
        playlist_name = (request.form.get("name") or f.filename or "Импортированные треки").rsplit(".", 1)[0]
        job_id = _uuid.uuid4().hex
        app._velora_import_jobs[job_id] = {
            "user_id": current_user.id,
            "status": "running",
            "total": len(pairs),
            "index": 0,
            "added": 0,
            "skipped": 0,
            "skipped_lines": [],
            "current": "",
            "playlist_id": None,
            "started_at": datetime.utcnow().isoformat(),
            "cancelled": False,
        }
        _job_save(job_id, app._velora_import_jobs[job_id])
        # Чистим завершённые старше 30 минут (защита от утечки).
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        for jid in list(app._velora_import_jobs.keys()):
            j = app._velora_import_jobs[jid]
            fin = j.get("finished_at")
            if fin:
                try:
                    if datetime.fromisoformat(fin) < cutoff:
                        del app._velora_import_jobs[jid]
                except Exception:
                    pass
        _job_cleanup_old()
        threading.Thread(
            target=_import_run, daemon=True,
            args=(job_id, current_user.id, current_user.kids_mode, pairs,
                  playlist_name, f.filename or "файла"),
        ).start()
        return jsonify({"ok": True, "job_id": job_id, "total": len(pairs)})

    @app.route("/api/import/status")
    @login_required
    def api_import_status():
        jid = (request.args.get("id") or "").strip()
        # Другой воркер uWSGI мог запустить импорт — читаем с диска.
        j = app._velora_import_jobs.get(jid) or _job_load(jid)
        if not j or j.get("user_id") != current_user.id:
            return jsonify({"error": "not found"}), 404
        return jsonify({k: v for k, v in j.items() if k != "user_id"})

    @app.route("/api/import/cancel", methods=["POST"])
    @login_required
    def api_import_cancel():
        data = request.get_json(silent=True) or {}
        jid = (data.get("id") or "").strip()
        j = app._velora_import_jobs.get(jid) or _job_load(jid)
        if not j or j.get("user_id") != current_user.id:
            return jsonify({"error": "not found"}), 404
        j["cancelled"] = True
        if jid in app._velora_import_jobs:
            app._velora_import_jobs[jid]["cancelled"] = True
        _job_save(jid, j)
        return jsonify({"ok": True})

    # ----------- РЕЗОЛВЕР / СТРИМ ---------------------------------------
    @app.route("/api/resolve")
    def api_resolve():
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify({"url": None})
        return jsonify({"url": resolve_stream(q)})

    # ----------- ДИЗЛАЙКИ ------------------------------------------------
    @app.route("/api/dislikes", methods=["GET", "POST", "DELETE"])
    @login_required
    def api_dislikes():
        if request.method == "GET":
            rows = (
                db.session.query(Dislike)
                .filter_by(user_id=current_user.id)
                .order_by(Dislike.created_at.desc())
                .limit(500)
                .all()
            )
            return jsonify([{
                "id": r.id,
                "track_id": r.track_id, "artist_id": r.artist_id,
                "title": r.title, "artist": r.artist, "cover": r.cover,
                "source": r.source, "scope": r.scope,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows])
        data = request.get_json(silent=True) or {}
        scope = data.get("scope") or "track"
        track_id = str(data.get("id") or data.get("track_id") or "")
        artist_id = str(data.get("artist_id") or "")
        source = data.get("source") or "deezer"
        if request.method == "DELETE":
            row_id = data.get("row_id")
            if row_id:
                d = db.session.get(Dislike, int(row_id))
                if d and d.user_id == current_user.id:
                    db.session.delete(d); db.session.commit()
            else:
                q = db.session.query(Dislike).filter_by(user_id=current_user.id, source=source, scope=scope)
                if scope == "track" and track_id:
                    q = q.filter_by(track_id=track_id)
                elif scope == "artist" and artist_id:
                    q = q.filter_by(artist_id=artist_id)
                q.delete(); db.session.commit()
            return jsonify({"ok": True})
        if scope == "track" and not track_id:
            return jsonify({"error": "id required"}), 400
        if scope == "artist" and not artist_id:
            return jsonify({"error": "artist_id required"}), 400
        d = Dislike(
            user_id=current_user.id,
            track_id=track_id or None, artist_id=artist_id or None,
            title=data.get("title", ""), artist=data.get("artist", ""),
            cover=data.get("cover_big") or data.get("cover_small") or data.get("cover", ""),
            source=source, scope=scope,
        )
        db.session.add(d); db.session.commit()
        return jsonify({"ok": True, "id": d.id})

    # ----------- ПРЕДПОЧТЕНИЯ АРТИСТОВ -----------------------------------
    def _artist_pref_dict(p: ArtistPref) -> dict:
        return {
            "artist_id": p.artist_id,
            "source": p.source,
            "name": p.name or "",
            "image": p.image or "",
            "kind": p.kind,
        }

    @app.route("/api/artists/preferences", methods=["GET", "POST"])
    @login_required
    def api_artist_preferences():
        if request.method == "GET":
            rows = (
                db.session.query(ArtistPref)
                .filter_by(user_id=current_user.id)
                .order_by(ArtistPref.updated_at.desc())
                .all()
            )
            return jsonify([_artist_pref_dict(r) for r in rows])

        data = request.get_json(silent=True) or {}
        artist_id = str(data.get("artist_id") or "").strip()
        source = (data.get("source") or "deezer").strip() or "deezer"
        kind = data.get("kind")  # "like" | "dislike" | None (снять)
        if not artist_id:
            return jsonify({"error": "artist_id required"}), 400
        if kind not in (None, "like", "dislike"):
            return jsonify({"error": "kind must be like|dislike|null"}), 400

        existing = (
            db.session.query(ArtistPref)
            .filter_by(user_id=current_user.id, artist_id=artist_id, source=source)
            .first()
        )
        # Снятие предпочтения.
        if kind is None:
            if existing:
                db.session.delete(existing)
                db.session.commit()
            return jsonify({"ok": True, "kind": None})

        name = (data.get("name") or "").strip()[:255]
        image = (data.get("image") or "").strip()[:512]
        if existing:
            existing.kind = kind
            if name:
                existing.name = name
            if image:
                existing.image = image
            existing.updated_at = datetime.utcnow()
        else:
            existing = ArtistPref(
                user_id=current_user.id,
                artist_id=artist_id,
                source=source,
                name=name,
                image=image,
                kind=kind,
            )
            db.session.add(existing)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = (
                db.session.query(ArtistPref)
                .filter_by(user_id=current_user.id, artist_id=artist_id, source=source)
                .first()
            )
            if existing:
                existing.kind = kind
                db.session.commit()
        return jsonify({"ok": True, "pref": _artist_pref_dict(existing) if existing else None})

    @app.route("/api/artists/preferences/<artist_id>", methods=["DELETE"])
    @login_required
    def api_artist_preferences_delete(artist_id: str):
        source = (request.args.get("source") or "deezer").strip() or "deezer"
        q = db.session.query(ArtistPref).filter_by(
            user_id=current_user.id, artist_id=str(artist_id), source=source
        )
        q.delete()
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/artists/catalog")
    @login_required
    def api_artists_catalog():
        """Большой список артистов для страницы предпочтений.

        Источники объединяются (без дублей по (source, artist_id)) в порядке:
          1. Существующие лайк/дизлайк предпочтения (всегда сверху).
          2. Артисты лайкнутых треков (по убыванию числа лайков).
          3. Артисты из истории прослушиваний.
          4. Артисты из чартов Deezer (общий каталог).
          5. Поиск по Deezer, если задан ?q=.
        """
        from collections import OrderedDict

        q = (request.args.get("q") or "").strip()
        try:
            limit = max(1, min(int(request.args.get("limit") or 60), 200))
        except (TypeError, ValueError):
            limit = 60
        try:
            offset = max(0, int(request.args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0

        # Текущие предпочтения для аннотации.
        prefs = (
            db.session.query(ArtistPref).filter_by(user_id=current_user.id).all()
        )
        pref_map = {(p.source or "deezer", p.artist_id): p.kind for p in prefs}

        # OrderedDict (source, id) -> {id, source, name, image, fans, source_kind}
        ordered: "OrderedDict[tuple[str, str], dict]" = OrderedDict()

        def _push(item: dict, src_kind: str) -> None:
            aid = str(item.get("id") or "")
            src = (item.get("source") or "deezer")
            if not aid:
                return
            key = (src, aid)
            if key in ordered:
                return
            ordered[key] = {
                "id": aid,
                "source": src,
                "name": item.get("name") or "",
                "image": (item.get("image") or item.get("picture_big")
                          or item.get("picture_medium") or item.get("picture_small") or ""),
                "fans": int(item.get("fans") or 0),
                "source_kind": src_kind,
                "kind": pref_map.get(key),
            }

        # 1. Существующие предпочтения.
        for p in prefs:
            _push({
                "id": p.artist_id, "source": p.source or "deezer",
                "name": p.name or "", "image": p.image or "",
            }, "preference")

        # 2. Лайкнутые треки.
        like_rows = (
            db.session.query(Like.artist_id, Like.artist, Like.cover, Like.source,
                             db.func.count(Like.id).label("cnt"))
            .filter(Like.user_id == current_user.id, Like.artist_id.isnot(None))
            .group_by(Like.artist_id, Like.artist, Like.cover, Like.source)
            .order_by(db.text("cnt DESC"))
            .limit(120)
            .all()
        )
        for r in like_rows:
            _push({
                "id": r.artist_id, "source": r.source or "deezer",
                "name": r.artist or "", "image": r.cover or "",
            }, "likes")

        # 3. История.
        hist_rows = (
            db.session.query(HistoryEntry.artist_id, HistoryEntry.artist,
                             HistoryEntry.cover, HistoryEntry.source,
                             db.func.count(HistoryEntry.id).label("cnt"))
            .filter(HistoryEntry.user_id == current_user.id,
                    HistoryEntry.artist_id.isnot(None))
            .group_by(HistoryEntry.artist_id, HistoryEntry.artist,
                      HistoryEntry.cover, HistoryEntry.source)
            .order_by(db.text("cnt DESC"))
            .limit(120)
            .all()
        )
        for r in hist_rows:
            _push({
                "id": r.artist_id, "source": r.source or "deezer",
                "name": r.artist or "", "image": r.cover or "",
            }, "history")

        # 4. Поиск по Deezer (если задан q) — выводим первыми результатами.
        if q:
            try:
                found = deezer.search_artists(q, 30)
                # Поднимаем найденные артисты НАВЕРХ (новый OrderedDict).
                hits: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
                for a in found:
                    item = {
                        "id": a.id, "source": a.source or "deezer",
                        "name": a.name, "image": a.picture_big or a.picture_small or "",
                        "fans": a.fans,
                    }
                    aid = str(item["id"])
                    if not aid:
                        continue
                    key = (item["source"], aid)
                    if key in hits:
                        continue
                    hits[key] = {
                        "id": aid, "source": item["source"],
                        "name": item["name"], "image": item["image"],
                        "fans": item["fans"], "source_kind": "search",
                        "kind": pref_map.get(key),
                    }
                # Префиксуем результаты поиска, далее — остальное (без дублей).
                for key, item in ordered.items():
                    if key not in hits:
                        hits[key] = item
                ordered = hits
            except Exception:
                pass

        # 5. Каталог Deezer (чарты) — добавляем в хвост, если не задан поиск.
        # Тянем сильно «впрок», чтобы кнопка «Показать ещё» имела что показывать.
        if not q and len(ordered) < (offset + limit) * 3:
            try:
                charts = deezer.get_charts(300)
                for t in charts:
                    aid = str(getattr(t, "artist_id", "") or "")
                    if not aid:
                        continue
                    _push({
                        "id": aid, "source": getattr(t, "source", "deezer") or "deezer",
                        "name": getattr(t, "artist", ""),
                        "image": getattr(t, "album_cover", ""),
                    }, "charts")
                    if len(ordered) >= (offset + limit) * 3:
                        break
            except Exception:
                pass

        # Если поиск задан, фильтруем серверной стороной по подстроке (на случай,
        # если в каталоге уже есть подходящие но deezer не сматчил).
        items = list(ordered.values())
        if q:
            ql = q.lower()
            items = [it for it in items if ql in (it.get("name") or "").lower()]

        total = len(items)
        page = items[offset: offset + limit]
        # Дозагружаем нормальные аватарки артистов из Deezer для текущей страницы
        # синхронно — иначе на первом открытии у новых артистов нет картинок.
        _enrich_artist_pictures(page, sync=True, timeout=2.5)
        return jsonify({
            "items": page,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total,
        })

    # ----------- ПУБЛИЧНЫЕ -----------------------------------------------
    @app.route("/api/p/<slug>")
    def api_public_playlist(slug: str):
        p = db.session.query(Playlist).filter_by(slug=slug).first()
        if not p:
            try:
                p = db.session.get(Playlist, int(slug))
            except (TypeError, ValueError):
                p = None
        if not p:
            abort(404)
        if not p.is_public and (not current_user.is_authenticated or p.user_id != current_user.id):
            return jsonify({"error": "private"}), 403
        owner = db.session.get(User, p.user_id)
        body = _serialize_playlist(p)
        body["owner"] = {
            "username": owner.username if owner else "",
            "slug": (owner.slug or owner.username) if owner else "",
            "display_name": (owner.display_name or owner.username) if owner else "",
            "avatar": (owner.avatar or "") if owner else "",
        }
        return jsonify(body)

    @app.route("/p/<slug>")
    def page_public_playlist(slug: str):
        return render_template("index.html")

    # ---------- Внешний GitHub Actions резолвер ----------
    # Sprinthost-IP в чёрных списках YouTube/SoundCloud, поэтому локальный
    # yt-dlp часто промахивается. Очередь промахов забирается раз в 10 минут
    # из GitHub Actions (бесплатные runner'ы, чистый IP), резолвится и
    # пушится обратно в кэш через эти два эндпоинта.
    def _resolver_token_ok() -> bool:
        expected = (os.environ.get("RESOLVE_PUSH_TOKEN") or "").strip()
        if not expected:
            return False
        got = (request.headers.get("X-Resolve-Token") or request.args.get("token") or "").strip()
        # Защита от timing-атаки.
        return secrets.compare_digest(got, expected)

    @app.route("/api/resolve/queue", methods=["GET"])
    def api_resolve_queue():
        if not _resolver_token_ok():
            return Response("forbidden", status=403)
        try:
            from velora.api.resolver import queue_pop_batch
            try:
                limit = int(request.args.get("limit") or 20)
            except ValueError:
                limit = 20
            return jsonify({"items": queue_pop_batch(limit)})
        except Exception as e:
            app.logger.exception("queue err: %s", e)
            return jsonify({"items": [], "error": str(e)}), 500

    @app.route("/api/resolve/push", methods=["POST"])
    def api_resolve_push():
        if not _resolver_token_ok():
            return Response("forbidden", status=403)
        try:
            from velora.api.resolver import cache_put_external
            data = request.get_json(silent=True) or {}
            items = data.get("items") or []
            if not isinstance(items, list):
                return jsonify({"accepted": 0, "error": "items must be list"}), 400
            accepted = 0
            for it in items[:200]:
                if not isinstance(it, dict):
                    continue
                q = (it.get("q") or "").strip()
                url = (it.get("url") or "").strip()
                if not q or not url:
                    continue
                try:
                    dur = int(it.get("duration") or 0)
                except (TypeError, ValueError):
                    dur = 0
                quality = (it.get("quality") or "hi").strip().lower()
                if cache_put_external(q, dur, quality, url):
                    accepted += 1
            return jsonify({"accepted": accepted})
        except Exception as e:
            app.logger.exception("push err: %s", e)
            return jsonify({"accepted": 0, "error": str(e)}), 500

    @app.route("/api/stream", methods=["GET", "HEAD"])
    def api_stream():
        q = request.args.get("q", "").strip()
        if not q:
            return Response("missing q", status=400)
        # Серверная блокировка для kids_mode (если клиент пометил трек как explicit)
        if current_user.is_authenticated and current_user.kids_mode and request.args.get("explicit") == "1":
            return Response("blocked: kids mode", status=451)
        try:
            target_dur = int(request.args.get("duration") or 0)
        except ValueError:
            target_dur = 0
        # quality=low — режим оффлайн-загрузки: резолвер выберет opus@<=96
        # из имеющихся форматов (без перекодирования; экономия ~50%).
        quality = (request.args.get("quality") or "hi").strip().lower()
        if quality not in ("hi", "low"):
            quality = "hi"
        preview = (request.args.get("preview") or "").strip()
        upstream_kind = "full"  # full | preview
        # HEAD-запрос (используется prefetch'ем для определения X-Velora-Source).
        # Не вызываем yt-dlp для HEAD: возвращаем «обещанный» источник на основе того,
        # пойдём ли мы фолбэком в preview. Иначе HEAD периодически падает в 500
        # (yt-dlp бросает исключение, или upstream недоступен) и засоряет консоль.
        is_head = (request.method == "HEAD")
        try:
            # Раньше гостям сразу отдавали 30-сек Deezer-превью — но это
            # давало ощущение «весь сайт = цензурированные сниппеты».
            # Теперь все слушают полные треки (yt-dlp/SC), preview остаётся
            # только как аварийный фоллбек, если все источники упали.
            if is_head:
                # Для HEAD не дёргаем yt-dlp — это только дешёвый probe.
                upstream = preview if preview.startswith("http") else ""
                upstream_kind = "preview" if upstream else "full"
                if not upstream:
                    # Без preview HEAD не имеет смысла — отдаём пустой 200, чтобы не было 500.
                    h = {"X-Velora-Source": "unknown",
                         "Access-Control-Allow-Origin": "*",
                         "Access-Control-Expose-Headers": "X-Velora-Source"}
                    return Response("", status=200, headers=h)
            else:
                # Никаких ранних preview-shortcut'ов: пользователь хочет ПОЛНЫЕ
                # треки без цензуры. Если резолвер падает — отдаём 503,
                # клиент скипнет на следующий (см. audio 'error' handler).
                # Preview принимается ТОЛЬКО если клиент явно попросил ?fallback=preview=1.
                allow_preview_fb = request.args.get("fallback") == "preview"
                try:
                    upstream = resolve_stream(q, target_dur, quality=quality)
                except Exception as e:
                    app.logger.warning("resolve_stream failed for %r: %s", q, e)
                    upstream = None
                if not upstream:
                    if allow_preview_fb and preview.startswith("http"):
                        upstream = preview
                        upstream_kind = "preview"
                    else:
                        # Сообщаем клиенту: сервис временно не смог достать
                        # полный аудио. Audio.onerror ловит это и переходит дальше.
                        h = {"Retry-After": "5",
                             "X-Velora-Source": "unavailable",
                             "Access-Control-Allow-Origin": "*",
                             "Access-Control-Expose-Headers": "X-Velora-Source"}
                        return Response("resolve failed", status=503, headers=h)
        except Exception as e:
            app.logger.exception("api_stream resolve crashed: %s", e)
            h = {"Retry-After": "5",
                 "X-Velora-Source": "unavailable",
                 "Access-Control-Allow-Origin": "*",
                 "Access-Control-Expose-Headers": "X-Velora-Source"}
            return Response("resolve failed", status=503, headers=h)
        # Проксируем поток same-origin: иначе WebAudio (EQ) делает media CORS-tainted
        # и звук пропадает на cross-origin CDN-источниках (SoundCloud и т.п.).
        import requests as _rq
        from velora.api.http import SESSION as _S
        fwd_headers = {}
        rng = request.headers.get("Range")
        if rng:
            fwd_headers["Range"] = rng
        fwd_headers["User-Agent"] = _S.headers.get("User-Agent", "Mozilla/5.0")
        # Несколько попыток прокси: cf-media.sndcdn.com иногда отваливается с read-timeout.
        # 302 на upstream категорически нельзя — после createMediaElementSource (EQ)
        # cross-origin CDN превращает <audio> в tainted-источник и звук пропадает.
        up = None
        last_err = None
        for attempt in range(2):
            try:
                up = _S.get(upstream, headers=fwd_headers, stream=True, timeout=20, allow_redirects=True)
                break
            except Exception as e:
                last_err = e
                up = None
        if up is None:
            # Пробуем превью, если ещё не пробовали — это same-origin прокси, сохраняет звук с EQ.
            if upstream_kind != "preview" and preview.startswith("http"):
                try:
                    up = _S.get(preview, headers=fwd_headers, stream=True, timeout=15, allow_redirects=True)
                    upstream_kind = "preview"
                except Exception as e:
                    last_err = e
                    up = None
        if up is None:
            app.logger.warning("stream proxy fail: %s", last_err)
            return Response("upstream unavailable", status=502)
        resp_headers = {}
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges", "Cache-Control", "ETag", "Last-Modified"):
            v = up.headers.get(h)
            if v:
                resp_headers[h] = v
        resp_headers.setdefault("Accept-Ranges", "bytes")
        resp_headers["Access-Control-Allow-Origin"] = "*"
        resp_headers["X-Velora-Source"] = upstream_kind
        resp_headers["Access-Control-Expose-Headers"] = "X-Velora-Source, Content-Range, Accept-Ranges"
        def _gen():
            try:
                for chunk in up.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                try: up.close()
                except Exception: pass
        return Response(_gen(), status=up.status_code, headers=resp_headers)

    @app.route("/api/prewarm", methods=["POST"])
    def api_prewarm():
        """Прогревает резолвер-кэш для списка треков (моментальный старт
        следующих в очереди). Принимает JSON {tracks:[{q,duration}, ...]}.
        Запускает фоновые потоки — клиенту отвечаем сразу.
        """
        data = request.get_json(silent=True) or {}
        tracks = data.get("tracks") or []
        if not isinstance(tracks, list) or not tracks:
            return jsonify({"ok": True, "queued": 0})
        # Берём максимум 5 треков, чтобы не положить worker'ов.
        tracks = tracks[:5]
        import threading as _th
        from velora.api.resolver import resolve_stream as _rs
        def _warm(q, dur):
            try:
                _rs(q, int(dur or 0))
            except Exception:
                pass
        for t in tracks:
            q = (t.get("q") or "").strip()
            if not q:
                continue
            try:
                dur = int(t.get("duration") or 0)
            except Exception:
                dur = 0
            _th.Thread(target=_warm, args=(q, dur), daemon=True).start()
        return jsonify({"ok": True, "queued": len(tracks)})


app = create_app()


def run_web(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    app.run(host=host, port=port, debug=debug, threaded=True)
