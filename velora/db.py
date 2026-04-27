"""Модели БД для Velora Sound."""
from __future__ import annotations

import secrets
from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _gen_uid() -> str:
    """Короткий уникальный публичный идентификатор пользователя (12 hex символов)."""
    return secrets.token_hex(6)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    # Уникальный публичный идентификатор (выдаётся при регистрации) —
    # никогда не меняется. Нужен для «два человека не попадут в один аккаунт» —
    # по этому uid матчим TG-вход, а не по имени.
    uid = db.Column(db.String(32), unique=True, nullable=True, index=True, default=_gen_uid)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    # password_hash остаётся в схеме для обратной совместимости со старыми БД,
    # но новых пользователей мы больше не создаём с паролем.
    password_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    settings = db.Column(db.Text, default="{}")
    display_name = db.Column(db.String(120), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    avatar = db.Column(db.Text, nullable=True)   # URL /api/img/<id> или data:
    cover = db.Column(db.Text, nullable=True)
    kids_mode = db.Column(db.Boolean, default=False)
    slug = db.Column(db.String(80), unique=True, nullable=True, index=True)
    location = db.Column(db.String(120), nullable=True)
    website = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(32), unique=True, nullable=True, index=True)
    phone_verified = db.Column(db.Boolean, default=False)
    email_verified = db.Column(db.Boolean, default=False)
    # Привязка Telegram — первичный способ входа.
    tg_id = db.Column(db.BigInteger, unique=True, nullable=True, index=True)
    tg_username = db.Column(db.String(64), nullable=True)
    tg_first_name = db.Column(db.String(120), nullable=True)
    tg_photo_url = db.Column(db.Text, nullable=True)
    # OAuth: Google и VK.
    google_id = db.Column(db.String(64), unique=True, nullable=True, index=True)
    vk_id = db.Column(db.BigInteger, unique=True, nullable=True, index=True)
    banner = db.Column(db.Text, nullable=True)
    is_private = db.Column(db.Boolean, default=False)
    privacy = db.Column(db.Text, default="{}")
    # Дата рождения (опционально). Если возраст < 18 — kids_mode
    # включается принудительно и не может быть выключен до совершеннолетия.
    dob = db.Column(db.Date, nullable=True)
    # Разрешить ли посторонним писать на стене этого пользователя.
    wall_enabled = db.Column(db.Boolean, default=True)


class LoginCode(db.Model):
    """Одноразовый код, выданный Telegram-ботом.

    Пользователь пишет /login в бота, бот генерирует 6-значный код, сохраняет
    сюда вместе с tg_id/username/first_name. На сайте этот код предъявляют —
    сервер находит запись, по текущему tg_id финдит/создаёт User и логинит.
    """
    __tablename__ = "login_codes"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    tg_id = db.Column(db.BigInteger, nullable=False, index=True)
    tg_username = db.Column(db.String(64), nullable=True)
    tg_first_name = db.Column(db.String(120), nullable=True)
    tg_photo_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    used_ip = db.Column(db.String(64), nullable=True)


class AuthSession(db.Model):
    """Активная сессия пользователя (один вход = одна строка).

    Используется для UI «Сессии» в настройках и для принудительного выхода.
    sid хранится в Flask session пользователя; при отсутствии здесь — логин рвётся.
    """
    __tablename__ = "auth_sessions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    platform = db.Column(db.String(64), nullable=True)   # «Windows» / «Android» / «iOS» / «macOS» / «Linux»
    browser = db.Column(db.String(64), nullable=True)    # «Chrome 138»
    provider = db.Column(db.String(32), nullable=True, default="telegram")
    geo = db.Column(db.String(120), nullable=True)       # Приближённо: страна/город или «Локально»
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    revoked = db.Column(db.Boolean, default=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True)


class PreviewView(db.Model):
    """Запись о просмотре welcome-страницы /pages-prew.

    fp_hash — это HMAC-SHA256(salt, fingerprint), где fingerprint собран
    из набора характеристик железа/браузера/API. По сути «слепок устройства»,
    хранится только хеш — обратно не восстановить. По наличию записи решаем,
    показывать ли welcome автоматически при заходе на «/».
    """
    __tablename__ = "preview_views"
    id = db.Column(db.Integer, primary_key=True)
    fp_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ImageBlob(db.Model):
    """Публичное изображение: аватар, обложка плейлиста, баннер и т.д.

    Файл лежит в instance/uploads/<sha>.<ext>, в БД — метаданные.
    Отдаётся всем через GET /api/img/<id>.
    """
    __tablename__ = "images"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    kind = db.Column(db.String(32), nullable=False, default="misc")  # avatar | cover | banner | playlist
    mime = db.Column(db.String(64), nullable=False)
    sha256 = db.Column(db.String(64), unique=True, nullable=False, index=True)
    size = db.Column(db.Integer, default=0)
    path = db.Column(db.String(255), nullable=False)  # относительно instance/
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def url(self) -> str:
        return f"/api/img/{self.id}"


class Follow(db.Model):
    """Подписка пользователя на пользователя."""
    __tablename__ = "follows"
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    followee_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("follower_id", "followee_id", name="uq_follow_pair"),
    )


class VerifyAttempt(db.Model):
    """Хранит:
    - попытки верификации (email/phone) — для лимита 3 попыток / 15 мин бан;
    - временные коды email (6 цифр, TTL 60 секунд);
    - токены глубокой ссылки Telegram (start-параметр и связанный телефон).
    """
    __tablename__ = "verify_attempts"
    id = db.Column(db.Integer, primary_key=True)
    # email | phone | tg_link
    kind = db.Column(db.String(16), nullable=False, index=True)
    # для email — нормализованный email; для phone — E.164 номер; для tg_link — telegram start-token
    target = db.Column(db.String(160), nullable=False, index=True)
    code = db.Column(db.String(16), nullable=True)         # 6-значный код (email)
    expires_at = db.Column(db.DateTime, nullable=True)     # срок действия кода
    attempts = db.Column(db.Integer, default=0)            # неудачные попытки ввода
    banned_until = db.Column(db.DateTime, nullable=True)   # бан до
    verified = db.Column(db.Boolean, default=False)        # подтверждено
    # связанные данные (для регистрации после верификации)
    extra = db.Column(db.Text, nullable=True)              # JSON — payload черновика
    phone_normalized = db.Column(db.String(32), nullable=True, index=True)  # для tg_link связки
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Like(db.Model):
    __tablename__ = "likes"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id = db.Column(db.String(64), nullable=False, index=True)
    artist_id = db.Column(db.String(64), nullable=True, index=True)
    title = db.Column(db.String(255))
    artist = db.Column(db.String(255))
    album = db.Column(db.String(255))
    cover = db.Column(db.String(512))
    duration = db.Column(db.Integer, default=0)
    source = db.Column(db.String(32), default="deezer")
    explicit = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("user_id", "track_id", "source", name="uq_user_track"),
    )


class HistoryEntry(db.Model):
    __tablename__ = "history"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id = db.Column(db.String(64), nullable=False)
    artist_id = db.Column(db.String(64), nullable=True)
    title = db.Column(db.String(255))
    artist = db.Column(db.String(255))
    album = db.Column(db.String(255))
    cover = db.Column(db.String(512))
    duration = db.Column(db.Integer, default=0)
    source = db.Column(db.String(32), default="deezer")
    explicit = db.Column(db.Boolean, default=False)
    # Откуда был запущен трек: home | search | charts | wave | playlist | artist | library | other
    from_view = db.Column(db.String(32), default="other")
    play_count = db.Column(db.Integer, default=1)
    played_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Playlist(db.Model):
    __tablename__ = "playlists"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    cover = db.Column(db.Text, nullable=True)
    pinned = db.Column(db.Boolean, default=False)
    is_public = db.Column(db.Boolean, default=False)
    slug = db.Column(db.String(80), unique=True, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    items = db.relationship(
        "PlaylistItem",
        backref="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )


class PlaylistItem(db.Model):
    __tablename__ = "playlist_items"
    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False)
    track_id = db.Column(db.String(64), nullable=False)
    title = db.Column(db.String(255))
    artist = db.Column(db.String(255))
    album = db.Column(db.String(255))
    cover = db.Column(db.String(512))
    duration = db.Column(db.Integer, default=0)
    source = db.Column(db.String(32), default="deezer")
    explicit = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, default=0)


class Dislike(db.Model):
    """Дизлайки треков и артистов: трек больше не появится в Моей волне."""
    __tablename__ = "dislikes"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    track_id = db.Column(db.String(64), nullable=True, index=True)
    artist_id = db.Column(db.String(64), nullable=True, index=True)
    title = db.Column(db.String(255))
    artist = db.Column(db.String(255))
    cover = db.Column(db.String(512))
    source = db.Column(db.String(32), default="deezer")
    scope = db.Column(db.String(16), default="track")  # track | artist
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ArtistPref(db.Model):
    """Предпочтение пользователя по артисту: like / dislike.

    Используется в «Настройки → Предпочтения артистов» и в алгоритме Моей волны
    (буст лайкнутых артистов, исключение дизлайкнутых). Запись на пару
    (user_id, artist_id, source) уникальна — переключение режима = UPDATE.
    """
    __tablename__ = "artist_prefs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    artist_id = db.Column(db.String(64), nullable=False, index=True)
    source = db.Column(db.String(32), default="deezer")
    name = db.Column(db.String(255))
    image = db.Column(db.String(512))
    kind = db.Column(db.String(16), nullable=False)  # like | dislike
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("user_id", "artist_id", "source", name="uq_user_artist_pref"),
    )


class WallPost(db.Model):
    """Запись на стене профиля пользователя.

    owner_id   — чей профиль (на чьей стене опубликовано).
    author_id  — кто написал (может совпадать с owner_id).
    text       — до 2000 символов, подмножество markdown (**жирный**, *курсив*, __подчёрк*).
    image_url  — прикреплённое изображение/GIF (URL /api/img/<id>).
    expires_at — когда запись будет автоматически удалена.
    status     — published | rejected (отклонено модерацией).
    """
    __tablename__ = "wall_posts"
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="published", index=True)
    moderation_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)


class PageVisit(db.Model):
    """??????? ????????? ???????? / ???????? / ??????????.

    ???????????? ??? ?????????? ?????? ???????????? (TasteSnapshot)
    ? ??????????? Wave ? ????????? ??? ???????????.
    kind: artist | album | playlist | track
    """
    __tablename__ = "page_visits"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False, index=True)
    target_id = db.Column(db.String(64), nullable=False, index=True)
    source = db.Column(db.String(32), default="deezer")
    name = db.Column(db.String(255))
    artist = db.Column(db.String(255))
    cover = db.Column(db.String(512))
    count = db.Column(db.Integer, default=1, nullable=False)
    last_visited_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (
        db.UniqueConstraint("user_id", "kind", "target_id", "source", name="uq_user_page_visit"),
    )


class TasteSnapshot(db.Model):
    """?????? ?????????????? ???????????? ???????????? (JSON-payload).

    ??? ??? ?????????? Wave / for-you. ??????????????? ??????.
    """
    __tablename__ = "taste_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    payload = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
