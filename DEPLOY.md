# Деплой Velora на sprinthost.ru

## 1. Подготовка локально

```powershell
# 1.1 убедись что .env заполнен (он не в git)
type .env
# 1.2 сгенерируй PROD-ключи и впиши их В .env НА СЕРВЕРЕ (не в репо!)
python -c "import secrets; print('SECRET_KEY=', secrets.token_hex(32))"
python -c "from cryptography.fernet import Fernet; print('FERNET_KEY=', Fernet.generate_key().decode())"
python -c "import secrets; print('FP_SALT=', secrets.token_hex(24))"
```

## 2. Панель → Сайты → Веб-серверы

- Включи **Python 3.13** для нужного домена.
- На той же странице запиши IP сервера (раздел «IP-адреса»).

## 3. Заливка файлов по SFTP/SSH

Используй WinSCP/PuTTY (см. [help](https://help.sprinthost.ru/file-transfer/ssh-and-sftp)).
Адрес: IP сервера, логин: твой логин аккаунта (НЕ имя БД), пароль: пароль панели.

Залей содержимое проекта (без `venv/`, `__pycache__/`, `*.db`, `instance/`) в:

```
~/site/<твой-домен>/
```

> `.env` ОБЯЗАТЕЛЬНО грузим вручную и сразу делаем `chmod 600 .env`.

## 4. SSH: виртуальное окружение и зависимости

```bash
# подключись по SSH
ssh <логин>@<IP>

cd ~/site/<домен>

# создаём venv
pip3.13 install --user virtualenv
~/.local/bin/virtualenv -p python3.13 venv
source venv/bin/activate

pip install -U pip wheel
pip install -r requirements.txt

# права на .env
chmod 600 .env
```

## 5. .htaccess для Passenger/uWSGI

Положи в корень сайта:

```apache
PassengerEnabled On
PassengerPython /home/<логин>/site/<домен>/venv/bin/python
SetEnv PYTHONUNBUFFERED 1

<Files .env>
    Require all denied
</Files>

<Files passenger_wsgi.py>
    Allow from all
</Files>

<FilesMatch "\.(py|pyc|cfg|sqlite3|db)$">
    Require all denied
</FilesMatch>
```

`passenger_wsgi.py` уже в репозитории — он автоматически активирует venv.

## 6. Чистый старт БД

```bash
source venv/bin/activate
python scripts/wipe_db.py --yes-i-really-want-to-delete-everything
```

Скрипт:
- DROP ALL → CREATE ALL (схема пересоздаётся под актуальные модели)
- удаляет содержимое `instance/uploads/` (флаг `--keep-uploads` чтобы оставить)

## 7. Перезапуск приложения

В Панели → Сайты → Веб-серверы → кнопка «Перезапустить».
Альтернативно по SSH: `touch tmp/restart.txt` (создай папку `tmp/` если её нет).

## 8. Проверка

```bash
curl -I https://<домен>/
# 200 OK
curl https://<домен>/api/wave
# 401 (нужен login) — это норма
```

## 9. Безопасность — чек-лист

- [ ] `.env` лежит на сервере, **не** в git, права 600.
- [ ] Старый пароль БД (`wQr-bTC-Y9q-eTY`) **сменён** в панели Спринтхоста.
- [ ] `VELORA_FERNET_KEY` уникальный на проде, **сохранён** в надёжном месте (без него зашифрованные данные нельзя расшифровать!).
- [ ] `VELORA_SECRET_KEY` уникальный на проде (если потеряешь — все session cookie инвалидируются).
- [ ] Старые OAuth Google secrets из репозитория **отозваны** в Google Cloud Console (они больше не в коде, но могли утечь раньше).
- [ ] Старый Telegram-токен (`7400261241:...`) **revoke** через @BotFather командой `/revoke`.
- [ ] Папка `instance/` запрещена для http (см. .htaccess выше).
- [ ] `velora.db` (SQLite) на проде НЕ должен существовать — используется MySQL.
