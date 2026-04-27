"""Шифрование секретов в БД (Fernet, AES-128-CBC + HMAC-SHA256).

Используется для значений, которые должны храниться в БД, но не должны быть
читаемы при дампе/бэкапе/утечке: telegram-токены пользователей, OAuth refresh,
ссылки автологина и т.п. Сами параметры приложения (TG_BOT_TOKEN,
OAUTH_*_CLIENT_SECRET, DB_PASSWORD) хранятся в .env и шифрованию не подлежат —
их защита это файловые права 600.

Мастер-ключ берётся из VELORA_FERNET_KEY (env). Если ключ отсутствует —
шифрование отключено и функции возвращают/принимают значение «как есть».
Это сделано чтобы локальная разработка работала без обязательного env.
"""
from __future__ import annotations

import os
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore


_FERNET: Optional["Fernet"] = None
_INITIALIZED = False


def _get() -> Optional["Fernet"]:
    global _FERNET, _INITIALIZED
    if _INITIALIZED:
        return _FERNET
    _INITIALIZED = True
    if Fernet is None:
        return None
    key = os.environ.get("VELORA_FERNET_KEY", "").strip()
    if not key:
        return None
    try:
        _FERNET = Fernet(key.encode("utf-8"))
    except Exception as exc:  # неверный формат ключа
        print(f"[crypto] VELORA_FERNET_KEY invalid: {exc}", flush=True)
        _FERNET = None
    return _FERNET


def is_enabled() -> bool:
    return _get() is not None


# Префикс маркирует, что значение зашифровано. Без него мы не знаем, лежит ли
# в колонке plain-текст (legacy) или ciphertext. Это позволяет постепенно
# мигрировать старые записи — encrypt_str() для новых, decrypt_str() читает оба.
_PREFIX = "fer1$"


def encrypt_str(value: str | None) -> str | None:
    """Шифрует строку. Возвращает 'fer1$<ciphertext>' либо исходник если ключа нет."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    f = _get()
    if f is None:
        return value
    token = f.encrypt(value.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_str(value: str | None) -> str | None:
    """Расшифровывает строку. Если префикса нет — возвращает as-is."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if not value.startswith(_PREFIX):
        return value  # legacy plain-text
    f = _get()
    if f is None:
        # Ключа нет, а значение зашифровано → читаем как есть, отдаём пустоту,
        # чтобы НЕ сломать UI на чужой машине.
        return ""
    token = value[len(_PREFIX):].encode("utf-8")
    try:
        return f.decrypt(token).decode("utf-8")
    except InvalidToken:
        return ""
