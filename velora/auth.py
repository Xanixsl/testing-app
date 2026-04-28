"""Авторизация Velora Sound — ТОЛЬКО через Telegram-бота.

Логика:
  1. Пользователь открывает бота `t.me/<bot>` и пишет /start (или /login).
  2. Бот генерирует одноразовый 6-значный код, сохраняет (code, tg_id,
     tg_username, tg_first_name, tg_photo_url) в таблицу LoginCode и
     отправляет код в чат моноширным блоком (легко скопировать тапом).
  3. Пользователь вставляет код на сайте → POST /api/auth/tg/code.
  4. Сервер находит запись, помечает как использованную, создаёт/находит
     User по tg_id и логинит.

Никакой email/SMS/паролей. SMTP, phonenumbers и прочее старьё — выпилено.
Поллинг бота крутится в фоновом потоке (long-polling getUpdates).
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger("velora.auth")

CODE_TTL_SEC = 5 * 60          # код живёт 5 минут
CODE_LEN = 6                   # 6 цифр
TG_API = "https://api.telegram.org/bot{token}/{method}"


def gen_code() -> str:
    """6-значный код (с возможным ведущим нулём)."""
    return f"{secrets.randbelow(10 ** CODE_LEN):0{CODE_LEN}d}"


def gen_session_id() -> str:
    """Случайный ID серверной сессии (хранится в Flask cookie)."""
    return secrets.token_urlsafe(24)


# ====================================================================
# TELEGRAM LOGIN BOT (long-polling)
# ====================================================================
class TelegramLoginBot:
    """Бот, который реагирует на /start и /login.

    На /start — отправляет приветствие и кнопку «Войти на сайте».
    На /login (или повторный /start) — генерирует код, кладёт в БД и
    присылает в чат моноширным блоком.
    """

    HELP_TEXT = (
        "✨ <b>Добро пожаловать в Velora Sound!</b>\n\n"
        "Я — бот входа. Через меня вы безопасно регистрируетесь "
        "или входите на сайт — без паролей, всё через Telegram.\n\n"
        "<b>Как это работает:</b>\n"
        "• На сайте нажмите <b>«Войти»</b> → введите номер телефона.\n"
        "• Сайт откроет этот чат и я пришлю вам одноразовый код (вход) "
        "или попрошу <b>поделиться номером</b> (регистрация).\n"
        "• Код вводите на сайте — и вы внутри.\n\n"
        "<b>Команды:</b>\n"
        "/login — получить код для входа\n"
        "/help — эта подсказка\n\n"
        "🔒 Никому не передавайте коды из этого чата — это ключ от вашего аккаунта."
    )

    def __init__(self, token: str, db_session_factory, login_code_model, user_model,
                 verify_attempt_model=None, admin_ids: tuple = (), site_url: str = ""):
        self.token = token
        self._stop = threading.Event()
        self._db_session_factory = db_session_factory
        self._LoginCode = login_code_model
        self._User = user_model
        self._VerifyAttempt = verify_attempt_model
        self.bot_username: str = ""
        self._admin_ids = tuple(int(x) for x in (admin_ids or ()))
        self._site_url = (site_url or "").rstrip("/")
        # Карты для функционала «Поддержка»:
        # _support_mode[tg_id] = "support" | "suggestion" пока юзер в режиме ввода.
        # _support_relay[admin_message_id]=user_chat_id — ответ админа возвращается автору.
        self._support_mode: dict[int, str] = {}
        self._support_relay: dict[int, int] = {}
        # Пул потоков для параллельной обработки update'ов: пока один _handle
        # ходит в БД/сеть, следующий long-poll уже готов принять новый апдейт.
        # Без этого все сообщения от пользователей идут строго по очереди и
        # 5-10 одновременных юзеров получают «бот тупит 30+ сек».
        from concurrent.futures import ThreadPoolExecutor as _TPE
        self._pool = _TPE(max_workers=8, thread_name_prefix="velora-tg")

    # --- Telegram low-level
    def _api(self, method: str, **payload) -> dict:
        url = TG_API.format(token=self.token, method=method)
        try:
            r = requests.post(url, json=payload, timeout=20)
            return r.json()
        except Exception as exc:  # noqa: BLE001
            log.error("TG api %s failed: %s", method, exc)
            return {"ok": False, "error": str(exc)}

    def get_me(self) -> dict:
        return self._api("getMe")

    def send_message(self, chat_id: int, text: str, **extra) -> dict:
        return self._api("sendMessage", chat_id=chat_id, text=text, **extra)

    def get_user_photo_url(self, tg_id: int) -> str:
        """Достать URL аватарки пользователя в TG (если открыт профиль)."""
        try:
            r = self._api("getUserProfilePhotos", user_id=tg_id, limit=1)
            if not r.get("ok"):
                return ""
            photos = r.get("result", {}).get("photos") or []
            if not photos:
                return ""
            file_id = photos[0][-1].get("file_id")  # самая большая
            if not file_id:
                return ""
            f = self._api("getFile", file_id=file_id)
            if not f.get("ok"):
                return ""
            file_path = f.get("result", {}).get("file_path")
            if not file_path:
                return ""
            return f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        except Exception:
            return ""

    # --- жизненный цикл
    def start(self) -> None:
        me = self.get_me()
        if me.get("ok"):
            self.bot_username = me["result"].get("username", "")
            log.info("[VELORA TG] login bot @%s ready", self.bot_username)
        else:
            log.warning("[VELORA TG] getMe failed: %s", me)
        # Регистрируем меню команд (показывается слева у поля ввода).
        try:
            self._api(
                "setMyCommands",
                commands=[
                    {"command": "start", "description": "Главное меню"},
                    {"command": "site", "description": "Войти на сайт"},
                    {"command": "login", "description": "Получить код входа"},
                    {"command": "profile", "description": "Мой профиль"},
                    {"command": "support", "description": "Поддержка / Предложка"},
                    {"command": "about", "description": "О Velora"},
                    {"command": "cancel", "description": "Отменить ввод"},
                ],
            )
            # Для админов добавляем отдельный скоуп с командой /suggestions.
            for aid in self._admin_ids:
                try:
                    self._api(
                        "setMyCommands",
                        commands=[
                            {"command": "start", "description": "Главное меню"},
                            {"command": "suggestions", "description": "Открытые предложки"},
                            {"command": "support", "description": "Поддержка / Предложка"},
                            {"command": "site", "description": "Войти на сайт"},
                            {"command": "profile", "description": "Мой профиль"},
                            {"command": "cancel", "description": "Отменить ввод"},
                        ],
                        scope={"type": "chat", "chat_id": int(aid)},
                    )
                except Exception:
                    pass
        except Exception as exc:
            log.debug("setMyCommands failed: %s", exc)
        # Кнопка-меню слева от поля ввода: открывает сайт как Mini App.
        # Telegram требует https для web_app — на http показываем обычную URL-кнопку.
        try:
            if self._site_url.startswith("https://"):
                self._api(
                    "setChatMenuButton",
                    menu_button={
                        "type": "web_app",
                        "text": "Velora",
                        "web_app": {"url": self._site_url},
                    },
                )
            else:
                # Возвращаем дефолт «Commands», если site_url ещё нет.
                self._api("setChatMenuButton", menu_button={"type": "commands"})
        except Exception as exc:
            log.debug("setChatMenuButton failed: %s", exc)
        # Описание профиля бота — отображается на странице бота в TG.
        try:
            self._api(
                "setMyDescription",
                description=(
                    "Velora Sound — музыка, плейлисты, профили и стена. "
                    "Войдите по номеру или через Telegram, делитесь записями "
                    "и пишите мне в поддержку."
                ),
            )
            self._api(
                "setMyShortDescription",
                short_description="🎵 Стриминг, плейлисты и социальная стена.",
            )
        except Exception:
            pass
        t = threading.Thread(target=self._loop, name="velora-tg-login", daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        offset = 0
        while not self._stop.is_set():
            try:
                r = requests.get(
                    TG_API.format(token=self.token, method="getUpdates"),
                    params={
                        "timeout": 25,
                        "offset": offset,
                        "allowed_updates": json.dumps(["message", "callback_query"]),
                    },
                    timeout=35,
                )
                data = r.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("[VELORA TG] poll error: %s", exc)
                time.sleep(3)
                continue
            if not data.get("ok"):
                log.warning("[VELORA TG] poll bad response: %s", data)
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                # Обработка в пуле — не блокирует следующий getUpdates.
                def _run(u=upd):
                    try:
                        self._handle(u)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("[VELORA TG] handler crashed: %s", exc)
                try:
                    self._pool.submit(_run)
                except Exception:
                    # Если пул переполнен/умер — обработаем синхронно.
                    _run()

    # --- обработка
    def _handle(self, upd: dict) -> None:
        # Inline-кнопки (callback_query).
        cq = upd.get("callback_query")
        if cq:
            self._handle_callback(cq)
            return

        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return
        from_user = msg.get("from") or {}
        tg_id = int(from_user.get("id") or chat_id)
        tg_username = (from_user.get("username") or "").strip() or None
        tg_first_name = (from_user.get("first_name") or "").strip() or None
        text = (msg.get("text") or "").strip()

        # /start link_<token> — глубокая ссылка для входа по номеру.
        # ВАЖНО: проверяем ДО общего /start, иначе перехватит generic-обработчик.
        if text.startswith("/start link_"):
            token = text.split("link_", 1)[-1].strip()
            self._handle_deeplink(chat_id, tg_id, tg_username, tg_first_name, token)
            return

        # /start intent_<token> — соц-кнопка «Telegram» на сайте.
        if text.startswith("/start intent_"):
            token = text.split("intent_", 1)[-1].strip()
            self._handle_intent(chat_id, tg_id, tg_username, tg_first_name, token)
            return

        # ---- Админский reply (поддержка): админ отвечает на пересланное
        # сообщение → пересылаем его обратно автору.
        reply_to = msg.get("reply_to_message") or {}
        if int(tg_id) in self._admin_ids and reply_to:
            mid = int(reply_to.get("message_id") or 0)
            target_chat = self._relay_lookup(mid)
            if target_chat:
                self.send_message(
                    target_chat,
                    f"<b>Поддержка Velora:</b>\n{text}" if text else "[ответ поддержки]",
                    parse_mode="HTML",
                )
                self.send_message(chat_id, "✓ Доставлено")
                return
            elif int(tg_id) in self._admin_ids:
                # Если reply на сообщение бота, но запись о relay не найдена —
                # явно сообщим админу, чтобы не молчать.
                src = (reply_to.get("from") or {}).get("is_bot")
                if src:
                    self.send_message(
                        chat_id,
                        "Не нашёл, кому переслать ответ (запись relay устарела). "
                        "Попросите пользователя написать ещё раз.",
                    )
                    return

        # Команды.
        if text in ("/start", "/help") or text.startswith("/start "):
            self._cmd_start(chat_id, tg_id, tg_username, tg_first_name)
            return
        if text == "/login":
            self._issue_code(chat_id, tg_id, tg_username, tg_first_name)
            return
        if text == "/site":
            self._cmd_site(chat_id, tg_id, tg_username, tg_first_name)
            return
        if text == "/profile":
            self._cmd_profile(chat_id, tg_id)
            return
        if text == "/support":
            self._send_support_menu(chat_id)
            return
        if text == "/cancel":
            self._support_mode.pop(int(tg_id), None)
            self.send_message(chat_id, "Отменено.", reply_markup={"remove_keyboard": True})
            return
        if text in ("/suggestions", "/ideas") and int(tg_id) in self._admin_ids:
            self._cmd_admin_suggestions(chat_id, only_open=not text.endswith("all"))
            return
        if text.startswith("/claim_admin"):
            parts = text.split(maxsplit=1)
            self._handle_claim_admin(chat_id, tg_id, parts[1].strip() if len(parts) > 1 else "")
            return
        if text == "/about":
            self.send_message(
                chat_id,
                "🎵 <b>Velora Sound</b> — стриминг музыки и социальная стена.\n\n"
                "• 50+ млн треков, плейлисты, поиск по тексту\n"
                "• Профили с настройками приватности\n"
                "• Стена: записи с TTL и модерацией\n"
                "• Безопасный вход через Telegram, без паролей\n\n"
                + (f"🌐 <a href=\"{self._site_url}\">Открыть Velora</a>" if self._site_url else ""),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        # Поделился контактом → завершаем phone-flow.
        contact = msg.get("contact")
        if contact:
            self._handle_contact(chat_id, tg_id, tg_username, tg_first_name, contact)
            return

        # Если включён режим поддержки/предложки — пересылаем админу.
        mode = self._support_mode.get(int(tg_id))
        if mode and text:
            self._forward_to_support(
                chat_id, tg_id, tg_username, tg_first_name, text, kind=mode,
            )
            return

        # Любой другой текст — главное меню.
        self._cmd_start(chat_id, tg_id, tg_username, tg_first_name)

    # ------------------------------------------------------------------
    # Inline-кнопки
    # ------------------------------------------------------------------
    def _handle_callback(self, cq: dict) -> None:
        data = (cq.get("data") or "").strip()
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        from_user = cq.get("from") or {}
        tg_id = int(from_user.get("id") or chat_id or 0)
        tg_username = (from_user.get("username") or "").strip() or None
        tg_first_name = (from_user.get("first_name") or "").strip() or None
        # Подтверждаем нажатие, чтобы Telegram убрал «спиннер».
        try:
            self._api("answerCallbackQuery", callback_query_id=cq.get("id"))
        except Exception:
            pass
        if not chat_id:
            return
        # Админские sugg-кнопки.
        if data.startswith("sugg:") and int(tg_id) in self._admin_ids:
            try:
                _, action, sid = data.split(":", 2)
                self._admin_suggestion_action(chat_id, tg_id, action, int(sid))
            except Exception:
                pass
            return
        if data == "menu:site":
            self._cmd_site(chat_id, tg_id, tg_username, tg_first_name)
        elif data == "menu:login":
            self._issue_code(chat_id, tg_id, tg_username, tg_first_name)
        elif data == "menu:profile":
            self._cmd_profile(chat_id, tg_id)
        elif data == "menu:support":
            self._send_support_menu(chat_id)
        elif data == "menu:support_msg":
            self._support_mode[int(tg_id)] = "support"
            self.send_message(
                chat_id,
                "✍️ <b>Сообщение в поддержку</b>\n\n"
                "Опишите проблему одним сообщением — я передам её администратору. "
                "Когда придёт ответ, он появится здесь же. /cancel — отменить.",
                parse_mode="HTML",
            )
        elif data == "menu:suggest":
            self._support_mode[int(tg_id)] = "suggestion"
            self.send_message(
                chat_id,
                "💡 <b>Предложка</b>\n\n"
                "Поделитесь идеей или фичей — мы всё прочитаем и используем "
                "лучшее. Пишите одним сообщением. /cancel — отменить.",
                parse_mode="HTML",
            )
        elif data == "menu:cancel":
            self._support_mode.pop(int(tg_id), None)
            self.send_message(chat_id, "Отменено.")
        elif data == "menu:about":
            self.send_message(
                chat_id,
                "🎵 <b>Velora Sound</b> — музыкальный сервис со стеной и приватностью.",
                parse_mode="HTML",
            )
        elif data == "menu:admin_sugg" and int(tg_id) in self._admin_ids:
            self._cmd_admin_suggestions(chat_id, only_open=True)
        else:
            self._cmd_start(chat_id, tg_id, tg_username, tg_first_name)

    # ------------------------------------------------------------------
    # /start — главное меню (зависит от того, привязан ли аккаунт)
    # ------------------------------------------------------------------
    def _find_user_by_tg(self, tg_id: int):
        sess = None
        try:
            sess = self._db_session_factory()
            return sess.query(self._User).filter_by(tg_id=int(tg_id)).first()
        except Exception:
            return None
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _cmd_start(self, chat_id: int, tg_id: int, tg_username, tg_first_name) -> None:
        user = self._find_user_by_tg(tg_id)
        # Кнопка «Открыть сайт» — Mini App для https, иначе обычная URL.
        if self._site_url:
            if self._site_url.startswith("https://"):
                site_btn_row = [{"text": "🚀 Открыть Velora",
                                 "web_app": {"url": self._site_url}}]
            else:
                site_btn_row = [{"text": "🌐 Открыть сайт", "url": self._site_url}]
        else:
            site_btn_row = []
        is_admin = int(tg_id) in self._admin_ids
        if user:
            uname = (getattr(user, "username", None) or tg_username or tg_first_name or "друг")
            text = (
                f"✨ <b>Velora Sound</b> — добро пожаловать, <b>{uname}</b>!\n\n"
                "🎵 Стриминг, плейлисты и социальная стена\n"
                "👤 Профили с приватностью и аватарами\n"
                "🔐 Безопасный вход без паролей\n\n"
                "Выберите действие:"
            )
            kb = [
                site_btn_row or [{"text": "🔑 Войти на сайт", "callback_data": "menu:site"}],
                [{"text": "🔐 Одноразовая ссылка", "callback_data": "menu:site"}],
                [{"text": "👤 Профиль", "callback_data": "menu:profile"},
                 {"text": "🆘 Поддержка", "callback_data": "menu:support"}],
                [{"text": "ℹ️ О сервисе", "callback_data": "menu:about"}],
            ]
            if is_admin:
                kb.append([{"text": "💡 Открытые предложки",
                            "callback_data": "menu:admin_sugg"}])
        else:
            text = (
                "✨ <b>Velora Sound</b>\n\n"
                "🎵 Стриминг, плейлисты и социальная стена\n"
                "👤 Профили с приватностью и аватарами\n"
                "🔐 Безопасный вход без паролей\n\n"
                "Чтобы пользоваться сайтом, зарегистрируйтесь по номеру "
                "телефона или используйте кнопку Telegram на сайте."
            )
            kb = [
                site_btn_row or [{"text": "🌐 Перейти на сайт", "callback_data": "menu:about"}],
                [{"text": "🔑 Получить код входа", "callback_data": "menu:login"}],
                [{"text": "🆘 Поддержка / Идея", "callback_data": "menu:support"}],
                [{"text": "ℹ️ О сервисе", "callback_data": "menu:about"}],
            ]
        self.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup={"inline_keyboard": kb},
            disable_web_page_preview=True,
        )

    # ------------------------------------------------------------------
    # /site — индивидуальная одноразовая ссылка для авто-логина на сайт.
    # ------------------------------------------------------------------
    def _cmd_site(self, chat_id: int, tg_id: int, tg_username, tg_first_name) -> None:
        if not self._site_url:
            self.send_message(
                chat_id,
                "Адрес сайта не настроен. Откройте Velora вручную и нажмите «Войти», "
                "затем используйте /login для получения кода.",
            )
            return
        user = self._find_user_by_tg(tg_id)
        if not user:
            self.send_message(
                chat_id,
                "У вас ещё нет аккаунта на Velora. Зарегистрируйтесь на сайте, "
                "затем возвращайтесь — здесь можно будет получать ссылки авто-входа.",
                reply_markup={"inline_keyboard": [[
                    {"text": "🌐 Перейти на сайт", "url": self._site_url},
                ]]},
            )
            return
        if not self._VerifyAttempt:
            self.send_message(chat_id, "Сервер недоступен, попробуйте позже.")
            return
        token = secrets.token_urlsafe(20)
        sess = None
        try:
            sess = self._db_session_factory()
            row = self._VerifyAttempt(
                kind="tg_autologin",
                target=token,
                phone_normalized=getattr(user, "phone", None) or "",
                extra=json.dumps({
                    "user_id": int(user.id),
                    "tg_id": int(tg_id),
                }, ensure_ascii=False),
                expires_at=datetime.utcnow() + timedelta(minutes=10),
                verified=False,
            )
            sess.add(row)
            sess.commit()
        except Exception as exc:
            log.exception("autologin token create failed: %s", exc)
            try:
                if sess is not None: sess.rollback()
            except Exception:
                pass
            self.send_message(chat_id, "Не удалось создать ссылку. Попробуйте ещё раз.")
            return
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass
        link = f"{self._site_url}/auth/tg/auto?t={token}"
        self.send_message(
            chat_id,
            "🔐 Индивидуальная ссылка для входа (действует 10 минут, одноразовая):",
            reply_markup={"inline_keyboard": [[
                {"text": "🌐 Войти на сайт", "url": link},
            ]]},
        )

    # ------------------------------------------------------------------
    # /profile — краткая инфо о пользователе.
    # ------------------------------------------------------------------
    def _cmd_profile(self, chat_id: int, tg_id: int) -> None:
        user = self._find_user_by_tg(tg_id)
        if not user:
            self.send_message(
                chat_id,
                "Аккаунт ещё не создан. Сначала зарегистрируйтесь на сайте.",
            )
            return
        uname = getattr(user, "username", None) or "—"
        dn = getattr(user, "display_name", None) or "—"
        phone = getattr(user, "phone", None) or "—"
        created = getattr(user, "created_at", None)
        created_str = created.strftime("%d.%m.%Y") if created else "—"
        self.send_message(
            chat_id,
            (
                "👤 <b>Ваш профиль на Velora</b>\n\n"
                f"• Ник: <code>@{uname}</code>\n"
                f"• Имя: {dn}\n"
                f"• Телефон: <code>{phone}</code>\n"
                f"• С нами с: {created_str}"
            ),
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # Поддержка: пересылаем сообщение админу с reply-возможностью.
    # ------------------------------------------------------------------
    def _relay_save(self, admin_chat_id: int, message_id: int, user_chat_id: int) -> None:
        """Сохраняем relay в БД, чтобы reply работал и после рестартов / на разных воркерах."""
        if not self._VerifyAttempt:
            return
        sess = None
        try:
            sess = self._db_session_factory()
            row = self._VerifyAttempt(
                kind="support_relay",
                target=f"{admin_chat_id}:{message_id}",
                phone_normalized="",
                extra=json.dumps({"user_chat_id": int(user_chat_id)}, ensure_ascii=False),
                expires_at=datetime.utcnow() + timedelta(days=30),
                verified=False,
            )
            sess.add(row)
            sess.commit()
        except Exception as exc:
            log.warning("relay save failed: %s", exc)
            try:
                if sess is not None: sess.rollback()
            except Exception:
                pass
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _relay_lookup(self, message_id: int) -> int | None:
        """Ищем кому относится reply: сначала in-memory (быстро), затем БД."""
        cached = self._support_relay.get(int(message_id))
        if cached:
            return int(cached)
        if not self._VerifyAttempt:
            return None
        sess = None
        try:
            sess = self._db_session_factory()
            # target вида "<admin_id>:<message_id>" — ищем по суффиксу.
            row = (sess.query(self._VerifyAttempt)
                   .filter(self._VerifyAttempt.kind == "support_relay")
                   .filter(self._VerifyAttempt.target.like(f"%:{int(message_id)}"))
                   .order_by(self._VerifyAttempt.id.desc())
                   .first())
            if not row:
                return None
            try:
                extra = json.loads(row.extra or "{}") or {}
            except Exception:
                extra = {}
            uc = extra.get("user_chat_id")
            if uc:
                self._support_relay[int(message_id)] = int(uc)
                return int(uc)
            return None
        except Exception as exc:
            log.warning("relay lookup failed: %s", exc)
            return None
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _send_support_menu(self, chat_id: int) -> None:
        self.send_message(
            chat_id,
            "🆘 <b>Что вы хотите отправить?</b>\n\n"
            "• <b>Написать</b> — связь с поддержкой по проблеме или вопросу. "
            "Администратор ответит сюда же.\n"
            "• <b>Предложка</b> — идея или новая фича. Мы соберём все "
            "предложения и реализуем лучшие.",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": [
                [{"text": "✍️ Написать в поддержку", "callback_data": "menu:support_msg"}],
                [{"text": "💡 Предложка", "callback_data": "menu:suggest"}],
                [{"text": "✖️ Отмена", "callback_data": "menu:cancel"}],
            ]},
        )

    def _forward_to_support(
        self, chat_id: int, tg_id: int,
        tg_username, tg_first_name, text: str,
        kind: str = "support",
    ) -> None:
        if not self._admin_ids:
            self.send_message(
                chat_id,
                "Поддержка временно недоступна (админ не настроен). "
                "Напишите позже или используйте сайт.",
            )
            self._support_mode.pop(int(tg_id), None)
            return
        is_suggestion = (kind == "suggestion")
        title = "💡 <b>Предложка</b>" if is_suggestion else "📩 <b>Сообщение в поддержку</b>"
        # Сохраняем предложку в БД, чтобы админ мог потом просмотреть /suggestions.
        sugg_id: int | None = None
        if is_suggestion and self._VerifyAttempt:
            sess = None
            try:
                sess = self._db_session_factory()
                row = self._VerifyAttempt(
                    kind="suggestion",
                    target=secrets.token_urlsafe(10),
                    phone_normalized="",
                    extra=json.dumps({
                        "text": text[:2000],
                        "tg_id": int(tg_id),
                        "tg_username": tg_username or "",
                        "tg_first_name": tg_first_name or "",
                        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
                    }, ensure_ascii=False),
                    expires_at=datetime.utcnow() + timedelta(days=180),
                    verified=False,  # False = открытая (не реализована)
                )
                sess.add(row)
                sess.commit()
                sugg_id = int(row.id)
            except Exception as exc:
                log.exception("suggestion store failed: %s", exc)
                try:
                    if sess is not None: sess.rollback()
                except Exception:
                    pass
            finally:
                try:
                    if sess is not None and callable(getattr(sess, "remove", None)):
                        sess.remove()
                except Exception:
                    pass
        meta = (
            f"{title}\n"
            f"От: {tg_first_name or '—'}"
            + (f" (@{tg_username})" if tg_username else "")
            + f", id <code>{tg_id}</code>"
            + (f"\nID идеи: <code>#{sugg_id}</code>" if sugg_id else "")
            + f"\n\n{text}\n\n"
            f"<i>Ответьте reply’ем на это сообщение — ответ дойдёт автору.</i>"
        )
        kb = None
        if is_suggestion and sugg_id:
            kb = {"inline_keyboard": [[
                {"text": "✓ Реализовано", "callback_data": f"sugg:done:{sugg_id}"},
                {"text": "🗑 Удалить", "callback_data": f"sugg:del:{sugg_id}"},
            ]]}
        delivered = False
        for admin_id in self._admin_ids:
            r = self.send_message(admin_id, meta, parse_mode="HTML", reply_markup=kb)
            if r.get("ok"):
                mid = (r.get("result") or {}).get("message_id")
                if mid:
                    self._support_relay[int(mid)] = int(chat_id)
                    self._relay_save(int(admin_id), int(mid), int(chat_id))
                    delivered = True
        if delivered:
            if is_suggestion:
                self.send_message(chat_id, "✓ Спасибо! Ваша идея сохранена и отправлена.")
            else:
                self.send_message(chat_id, "✓ Передал администратору. Ответ придёт сюда же.")
        else:
            self.send_message(chat_id, "Не получилось передать сообщение. Попробуйте позже.")
        self._support_mode.pop(int(tg_id), None)

    # ------------------------------------------------------------------
    # /claim_admin <token> — стать админом по одноразовому токену.
    # ------------------------------------------------------------------
    def _handle_claim_admin(self, chat_id: int, tg_id: int, given: str) -> None:
        expected = getattr(self, "_admin_claim_token", "") or ""
        if int(tg_id) in self._admin_ids:
            self.send_message(chat_id, "✓ Вы уже админ.")
            return
        if not expected:
            self.send_message(
                chat_id,
                "Админ уже назначен. Если нужно добавить ещё — задайте "
                "VELORA_TG_ADMIN_IDS в окружении и перезапустите сервер.",
            )
            return
        if not given or given.strip() != expected:
            self.send_message(chat_id, "Неверный токен.")
            return
        # Назначаем.
        self._admin_ids = tuple(set(list(self._admin_ids) + [int(tg_id)]))
        self._admin_claim_token = ""
        path = getattr(self, "_admins_path", "")
        if path:
            try:
                import os as _os
                _os.makedirs(_os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"{int(tg_id)}\n")
            except Exception as exc:
                log.warning("admin persist failed: %s", exc)
        # Обновляем команды для нового админа.
        try:
            self._api(
                "setMyCommands",
                commands=[
                    {"command": "start", "description": "Главное меню"},
                    {"command": "suggestions", "description": "Открытые предложки"},
                    {"command": "support", "description": "Поддержка / Предложка"},
                    {"command": "site", "description": "Войти на сайт"},
                    {"command": "profile", "description": "Мой профиль"},
                    {"command": "cancel", "description": "Отменить ввод"},
                ],
                scope={"type": "chat", "chat_id": int(tg_id)},
            )
        except Exception:
            pass
        self.send_message(
            chat_id,
            "✅ <b>Готово — вы админ Velora.</b>\n\n"
            "Теперь вам будут приходить запросы в поддержку и предложки.\n"
            "Команда <code>/suggestions</code> — открытые идеи.",
            parse_mode="HTML",
        )


    def _cmd_admin_suggestions(self, chat_id: int, only_open: bool = True) -> None:
        if not self._VerifyAttempt:
            self.send_message(chat_id, "БД недоступна.")
            return
        sess = None
        try:
            sess = self._db_session_factory()
            q = sess.query(self._VerifyAttempt).filter_by(kind="suggestion")
            if only_open:
                q = q.filter_by(verified=False)
            rows = q.order_by(self._VerifyAttempt.id.desc()).limit(20).all()
            if not rows:
                self.send_message(
                    chat_id,
                    "Нет открытых предложек." if only_open else "Список пуст.",
                )
                return
            for row in rows:
                try:
                    extra = json.loads(row.extra or "{}") or {}
                except Exception:
                    extra = {}
                text = extra.get("text") or "(пусто)"
                created = extra.get("created_at") or ""
                fn = extra.get("tg_first_name") or "—"
                un = extra.get("tg_username") or ""
                rid = int(row.id)
                done = bool(row.verified)
                body = (
                    f"{'✅' if done else '🔵'} <b>Предложка #{rid}</b> "
                    f"<i>{created}</i>\n"
                    f"От: {fn}" + (f" (@{un})" if un else "") + "\n\n"
                    f"{text}"
                )
                kb = {"inline_keyboard": [[
                    {"text": "✓ Реализовано" if not done else "↩︎ Открыть снова",
                     "callback_data": f"sugg:{'done' if not done else 'reopen'}:{rid}"},
                    {"text": "🗑 Удалить", "callback_data": f"sugg:del:{rid}"},
                ]]}
                self.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            log.exception("/suggestions failed: %s", exc)
            self.send_message(chat_id, "Не удалось получить список.")
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _admin_suggestion_action(self, chat_id: int, tg_id: int, action: str, sid: int) -> None:
        if int(tg_id) not in self._admin_ids:
            return
        if not self._VerifyAttempt:
            return
        sess = None
        try:
            sess = self._db_session_factory()
            row = sess.get(self._VerifyAttempt, int(sid))
            if not row or row.kind != "suggestion":
                self.send_message(chat_id, f"Предложка #{sid} не найдена.")
                return
            if action == "done":
                row.verified = True
                sess.commit()
                self.send_message(chat_id, f"✅ Предложка #{sid} помечена как реализованная.")
            elif action == "reopen":
                row.verified = False
                sess.commit()
                self.send_message(chat_id, f"↩︎ Предложка #{sid} снова открыта.")
            elif action == "del":
                sess.delete(row)
                sess.commit()
                self.send_message(chat_id, f"🗑 Предложка #{sid} удалена.")
        except Exception as exc:
            log.exception("suggestion action failed: %s", exc)
            try:
                if sess is not None: sess.rollback()
            except Exception:
                pass
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _issue_code(
        self,
        chat_id: int,
        tg_id: int,
        tg_username: str | None,
        tg_first_name: str | None,
    ) -> None:
        # Достаём аватарку (один сетевой вызов — на старте, не критично).
        photo_url = self.get_user_photo_url(tg_id) or None
        sess = None
        try:
            sess = self._db_session_factory()
            # Удаляем старые неиспользованные коды от этого же пользователя.
            now = datetime.utcnow()
            try:
                old = (
                    sess.query(self._LoginCode)
                    .filter_by(tg_id=tg_id, used=False)
                    .all()
                )
                for o in old:
                    sess.delete(o)
            except Exception:
                pass
            # Генерируем код, гарантируем уникальность (на всякий случай).
            for _ in range(8):
                code = gen_code()
                exists = sess.query(self._LoginCode).filter_by(code=code, used=False).first()
                if not exists:
                    break
            else:
                code = gen_code()
            row = self._LoginCode(
                code=code,
                tg_id=tg_id,
                tg_username=tg_username,
                tg_first_name=tg_first_name,
                tg_photo_url=photo_url,
                expires_at=now + timedelta(seconds=CODE_TTL_SEC),
                used=False,
            )
            sess.add(row)
            sess.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("issue_code db error: %s", exc)
            try:
                if sess is not None:
                    sess.rollback()
            except Exception:
                pass
            self.send_message(chat_id, "Не удалось сгенерировать код. Попробуйте ещё раз.")
            return
        finally:
            try:
                if sess is not None:
                    rm = getattr(sess, "remove", None)
                    if callable(rm):
                        rm()
            except Exception:
                pass

        ttl_min = CODE_TTL_SEC // 60
        text = (
            f"<b>Ваш код для входа:</b>\n\n"
            f"<code>{code}</code>\n\n"
            f"Скопируйте его (тап по коду) и вставьте на сайте Velora.\n"
            f"Действителен <b>{ttl_min} минут</b>. Никому не передавайте."
        )
        self.send_message(chat_id, text, parse_mode="HTML")

    # ------------------------------------------------------------------
    # Соц-кнопка «Telegram» на сайте: /start intent_<token>.
    # Если у юзера уже есть аккаунт — мгновенный авто-вход в браузере
    # (помечаем intent verified+user_id, браузер видит и редиректит).
    # Иначе — обычное меню /start (регистрация по номеру / получить код).
    # ------------------------------------------------------------------
    def _handle_intent(
        self,
        chat_id: int,
        tg_id: int,
        tg_username: str | None,
        tg_first_name: str | None,
        token: str,
    ) -> None:
        if not token or not self._VerifyAttempt:
            self._cmd_start(chat_id, tg_id, tg_username, tg_first_name)
            return
        sess = None
        try:
            sess = self._db_session_factory()
            row = (sess.query(self._VerifyAttempt)
                   .filter_by(kind="tg_intent", target=token).first())
            if not row:
                self.send_message(
                    chat_id,
                    "Ссылка устарела. Вернитесь на сайт и нажмите «Telegram» ещё раз.",
                )
                return
            if row.expires_at and row.expires_at < datetime.utcnow():
                self.send_message(chat_id, "Ссылка истекла. Сгенерируйте новую на сайте.")
                return
            user = sess.query(self._User).filter_by(tg_id=int(tg_id)).first()
            if user:
                # Аккаунт привязан → мгновенный вход.
                row.verified = True
                try:
                    extra = json.loads(row.extra or "{}") or {}
                except Exception:
                    extra = {}
                extra["user_id"] = int(user.id)
                extra["tg_id"] = int(tg_id)
                row.extra = json.dumps(extra, ensure_ascii=False)
                sess.commit()
                self.send_message(
                    chat_id,
                    f"✓ С возвращением, <b>{getattr(user,'username','') or tg_first_name or 'друг'}</b>!\n"
                    "Сайт уже выполняет вход — можете возвращаться во вкладку браузера.",
                    parse_mode="HTML",
                )
                return
            # Аккаунта нет — показать меню /start как обычно.
        except Exception as exc:
            log.exception("intent handle failed: %s", exc)
            try:
                if sess is not None: sess.rollback()
            except Exception:
                pass
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass
        self.send_message(
            chat_id,
            "Здесь нет аккаунта Velora, привязанного к этому Telegram. "
            "Зарегистрируйтесь на сайте по номеру телефона — после привязки "
            "кнопка «Telegram» будет логинить вас в один клик.",
        )
        self._cmd_start(chat_id, tg_id, tg_username, tg_first_name)

    # ------------------------------------------------------------------
    # Phone-flow: /start link_<token> + share contact / send code
    # ------------------------------------------------------------------
    def _handle_deeplink(
        self,
        chat_id: int,
        tg_id: int,
        tg_username: str | None,
        tg_first_name: str | None,
        token: str,
    ) -> None:
        """Роутер deep-link: register → share contact, login → send 6-digit code."""
        if not token or not self._VerifyAttempt:
            self.send_message(chat_id, "Ссылка устарела или неверная. Откройте сайт и попробуйте заново.")
            return
        sess = None
        mode = "register"
        expected_tg_id = None
        try:
            sess = self._db_session_factory()
            row = (
                sess.query(self._VerifyAttempt)
                .filter_by(kind="tg_link", target=token)
                .first()
            )
            if not row or (row.expires_at and row.expires_at < datetime.utcnow()):
                self.send_message(chat_id, "Ссылка устарела. Откройте сайт и попробуйте ещё раз.")
                return
            try:
                extra = json.loads(row.extra or "{}") or {}
            except Exception:
                extra = {}
            mode = extra.get("mode") or "register"
            expected_tg_id = extra.get("expected_tg_id")

            if mode == "login":
                # Анти-спуф: открыть ссылку входа должен ВЛАДЕЛЕЦ аккаунта.
                if expected_tg_id and int(expected_tg_id) != int(tg_id):
                    self.send_message(
                        chat_id,
                        "Эта ссылка для другого Telegram-аккаунта. "
                        "Войдите в Telegram под тем же аккаунтом, что был при регистрации.",
                    )
                    return
                # Генерируем код и сохраняем в extra.
                code = gen_code()
                extra["code"] = code
                extra["code_sent"] = True
                extra["tg_id"] = int(tg_id)
                row.extra = json.dumps(extra, ensure_ascii=False)
                sess.commit()
                pretty = f"{code[:3]}-{code[3:]}"
                self.send_message(
                    chat_id,
                    (
                        f"<b>Код для входа на Velora:</b>\n\n"
                        f"<code>{pretty}</code>\n\n"
                        f"Введите его на сайте. Никому не передавайте."
                    ),
                    parse_mode="HTML",
                )
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("deeplink db error: %s", exc)
            try:
                if sess is not None:
                    sess.rollback()
            except Exception:
                pass
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

        # mode == register → просим контакт.
        self._prompt_share_contact(chat_id, token)

    def _prompt_share_contact(self, chat_id: int, token: str) -> None:
        """Показывает кнопку «Поделиться контактом» (register-режим)."""
        self.send_message(
            chat_id,
            (
                "Чтобы войти на Velora Sound, поделитесь своим номером телефона.\n"
                "Нажмите кнопку ниже — Telegram спросит подтверждение."
            ),
            reply_markup={
                "keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )
        # Сохраняем chat_id → token, чтобы при получении контакта найти запись.
        # Используем тот же VerifyAttempt: extra хранит chat_id и pending_token.
        sess = None
        try:
            sess = self._db_session_factory()
            # Стираем все прошлые pending привязки этого chat_id, чтобы новый
            # контакт не закрыл старую неактуальную сессию входа.
            try:
                old = (
                    sess.query(self._VerifyAttempt)
                    .filter_by(kind="tg_pending", target=str(chat_id))
                    .all()
                )
                for o in old:
                    sess.delete(o)
            except Exception:
                pass
            pending = self._VerifyAttempt(
                kind="tg_pending",
                target=str(chat_id),
                extra=json.dumps({"token": token}),
                expires_at=datetime.utcnow() + timedelta(minutes=10),
            )
            sess.add(pending)
            sess.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("pending save error: %s", exc)
            try:
                if sess is not None:
                    sess.rollback()
            except Exception:
                pass
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

    def _handle_contact(
        self,
        chat_id: int,
        tg_id: int,
        tg_username: str | None,
        tg_first_name: str | None,
        contact: dict,
    ) -> None:
        if not self._VerifyAttempt:
            return
        # Анти-спуф: пользователь должен поделиться СВОИМ контактом, не чужим.
        contact_user_id = contact.get("user_id")
        if contact_user_id and int(contact_user_id) != int(tg_id):
            self.send_message(
                chat_id,
                "Можно поделиться только собственным номером. Нажмите кнопку ещё раз.",
            )
            return
        phone = (contact.get("phone_number") or "").strip()
        if not phone:
            self.send_message(chat_id, "Не получилось прочитать номер. Попробуйте ещё раз.")
            return
        if not phone.startswith("+"):
            phone = "+" + phone

        sess = None
        try:
            sess = self._db_session_factory()
            pending = (
                sess.query(self._VerifyAttempt)
                .filter_by(kind="tg_pending", target=str(chat_id))
                .order_by(self._VerifyAttempt.id.desc())
                .first()
            )
            if not pending or (pending.expires_at and pending.expires_at < datetime.utcnow()):
                self.send_message(chat_id, "Сессия входа истекла. Откройте сайт и нажмите «Войти» заново.")
                return
            try:
                token = (json.loads(pending.extra or "{}") or {}).get("token") or ""
            except Exception:
                token = ""
            if not token:
                self.send_message(chat_id, "Не нашёл вашу ссылку входа. Попробуйте заново.")
                return
            link = (
                sess.query(self._VerifyAttempt)
                .filter_by(kind="tg_link", target=token)
                .first()
            )
            if not link or (link.expires_at and link.expires_at < datetime.utcnow()):
                self.send_message(chat_id, "Ссылка устарела. Откройте сайт и попробуйте ещё раз.")
                return
            link.phone_normalized = phone
            link.verified = True
            try:
                payload = json.loads(link.extra or "{}") or {}
            except Exception:
                payload = {}
            payload.update({
                "tg_id": int(tg_id),
                "tg_username": tg_username,
                "tg_first_name": tg_first_name,
                "phone": phone,
                "tg_photo_url": self.get_user_photo_url(tg_id) or None,
            })
            link.extra = json.dumps(payload, ensure_ascii=False)
            # Чистим pending
            sess.delete(pending)
            sess.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("contact handle db error: %s", exc)
            try:
                if sess is not None:
                    sess.rollback()
            except Exception:
                pass
            self.send_message(chat_id, "Внутренняя ошибка. Попробуйте ещё раз.")
            return
        finally:
            try:
                if sess is not None and callable(getattr(sess, "remove", None)):
                    sess.remove()
            except Exception:
                pass

        self.send_message(
            chat_id,
            "Готово! Возвращайтесь на сайт — мы уже логиним вас.",
            reply_markup={"remove_keyboard": True},
        )
