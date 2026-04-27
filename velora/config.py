"""Конфигурация Velora Sound."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Явный путь к .env (рядом с папкой velora/), чтобы найти файл независимо
# от cwd uWSGI/Passenger/test-runner.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)
else:
    load_dotenv(override=True)


def _persistent_secret_key() -> str:
    """Стабильный SECRET_KEY между рестартами.

    Приоритет: переменная окружения → файл instance/secret.key →
    свежесгенерированный ключ, сохранённый в файл.
    Это нужно, чтобы пользователей не выкидывало при перезапуске сервера
    (Flask использует SECRET_KEY для подписи session cookie).
    """
    env = os.environ.get("VELORA_SECRET_KEY")
    if env:
        return env
    # instance — стандартный flask-папка для приватных артефактов.
    base = Path(__file__).resolve().parent.parent / "instance"
    base.mkdir(parents=True, exist_ok=True)
    f = base / "secret.key"
    if f.exists():
        try:
            data = f.read_text(encoding="utf-8").strip()
            if data:
                return data
        except OSError:
            pass
    key = secrets.token_hex(32)
    try:
        f.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


class Config:
    SECRET_KEY = _persistent_secret_key()

    # Telegram-бот (токен и username). ВСЕ секреты ТОЛЬКО из env.
    # Если переменной нет — значение пустое, и фича просто не работает.
    TG_BOT_TOKEN = os.environ.get("VELORA_TG_BOT_TOKEN", "")
    TG_BOT_USERNAME = os.environ.get("VELORA_TG_BOT_USERNAME", "")
    # Telegram-id администратора (приходящие сообщения поддержки пересылаются
    # ему; ответ reply-ом возвращается обратно автору). Несколько id — через запятую.
    TG_ADMIN_IDS = tuple(
        int(x) for x in (os.environ.get("VELORA_TG_ADMIN_IDS") or "").replace(" ", "").split(",")
        if x.lstrip("-").isdigit()
    )
    # Базовый URL сайта — нужен для генерации ссылок auto-login в TG.
    SITE_URL = os.environ.get("VELORA_SITE_URL", "").rstrip("/")

    # OAuth: Google. Секреты ТОЛЬКО из env. Если их нет — соответствующие
    # /api/oauth/* эндпоинты вернут 503.
    OAUTH_GOOGLE_CLIENT_ID = os.environ.get("VELORA_OAUTH_GOOGLE_CLIENT_ID", "")
    OAUTH_GOOGLE_CLIENT_SECRET = os.environ.get("VELORA_OAUTH_GOOGLE_CLIENT_SECRET", "")

    # Соль для хеширования fingerprint устройства (welcome-страница).
    # На проде ОБЯЗАТЕЛЬНО задать в env, иначе хеши предсказуемы.
    PREVIEW_FP_SALT = os.environ.get(
        "VELORA_PREVIEW_FP_SALT",
        "velora-preview-default-salt-change-me-in-prod",
    )

    # Папка для загружаемых картинок (аватары, обложки плейлистов, баннеры).
    UPLOAD_DIR = Path(__file__).resolve().parent.parent / "instance" / "uploads"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Сессия: «вечная», переживает закрытие браузера и перезапуск сервера.
    SESSION_COOKIE_NAME = "velora_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 365  # год
    REMEMBER_COOKIE_DURATION = 60 * 60 * 24 * 365
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # БД (всё из env; дефолты только для локальной разработки)
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_NAME = os.environ.get("DB_NAME", "")
    DB_USER = os.environ.get("DB_USER", "")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

    # Если на dev-машине нет MySQL — используем SQLite fallback.
    # "1" — принудительно SQLite, "0" — принудительно MySQL,
    # "auto" (по умолчанию) — пробуем MySQL, при ошибке коннекта падаем на SQLite.
    USE_SQLITE_FALLBACK = os.environ.get("VELORA_SQLITE", "auto")

    @classmethod
    def _mysql_uri(cls) -> str:
        from urllib.parse import quote_plus
        pwd = quote_plus(cls.DB_PASSWORD)
        return f"mysql+pymysql://{cls.DB_USER}:{pwd}@{cls.DB_HOST}/{cls.DB_NAME}?charset=utf8mb4"

    @classmethod
    def _sqlite_uri(cls) -> str:
        return "sqlite:///velora.db"

    @classmethod
    def database_uri(cls) -> str:
        mode = str(cls.USE_SQLITE_FALLBACK).lower()
        if mode in ("1", "true", "yes"):
            return cls._sqlite_uri()
        if mode in ("0", "false", "no"):
            return cls._mysql_uri()
        # auto: пробуем подключиться к MySQL, при неудаче — SQLite
        try:
            import socket
            with socket.create_connection((cls.DB_HOST, 3306), timeout=1.5):
                pass
            # Порт открыт — пробуем настоящий коннект через pymysql.
            # Без этого SQLAlchemy упадёт уже на первом запросе и любой
            # POST в /api/auth/* вернёт 500.
            try:
                import pymysql  # type: ignore
                conn = pymysql.connect(
                    host=cls.DB_HOST,
                    user=cls.DB_USER,
                    password=cls.DB_PASSWORD,
                    database=cls.DB_NAME,
                    connect_timeout=2,
                    charset="utf8mb4",
                )
                conn.close()
                return cls._mysql_uri()
            except Exception:
                return cls._sqlite_uri()
        except OSError:
            return cls._sqlite_uri()

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }
