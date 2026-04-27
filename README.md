# Velora Sound

Современный музыкальный веб-плеер с двумя источниками музыки:
- **Локальная библиотека** через [Navidrome](https://www.navidrome.org/) (OpenSubsonic API)
- **YouTube Music** стриминг через [YouTube.js](https://github.com/LuanRT/YouTube.js)

## Архитектура

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Frontend   │────▶│  Backend (Node)  │────▶│  Navidrome    │
│  React+TS   │     │  Express         │     │  (Docker)     │
│  Vite       │     │  YouTube.js      │     │  SubsonicAPI  │
│  Tailwind   │     │  Rate-limit      │     └──────────────┘
│  Howler.js  │     │  Cache           │
│  Zustand    │     │  Stream proxy    │────▶  YouTube Music
└─────────────┘     └──────────────────┘        (InnerTube)
```

| Компонент | Технология |
|-----------|-----------|
| Frontend | React 18 + TypeScript + Vite + Tailwind CSS |
| Стейт | Zustand |
| Аудио | Howler.js |
| Backend | Node.js + Express |
| YouTube | youtubei.js (InnerTube API) |
| Локальная музыка | Navidrome (Docker) |
| Тексты | LRCLIB (синхронизированные) |

## Быстрый старт

### 1. Настройте путь к музыке

Отредактируйте `.env` в корне:
```
MUSIC_FOLDER=C:\Users\<username>\Music
```

### 2. Запустите Docker (Navidrome + Backend)

```bash
docker compose up -d
```

- Navidrome: http://localhost:4533 (создайте аккаунт при первом запуске)
- Backend API: http://localhost:4000

### 3. Или запустите Backend локально (без Docker)

```bash
cd backend
npm install
npm run dev
```

### 4. Запустите Frontend

```bash
cd frontend
npm install
npm run dev
```

Откройте http://localhost:3000

## API эндпоинты Backend

### YouTube Music

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/yt/search?q=query&type=song\|album\|artist\|playlist` | Поиск |
| GET | `/api/yt/track/:id` | Метаданные трека |
| GET | `/api/yt/stream/:id` | Аудио-поток (proxy) |
| GET | `/api/yt/album/:id` | Альбом + треки |
| GET | `/api/yt/artist/:id` | Артист + топ-треки + дискография |
| GET | `/api/yt/playlist/:id` | Плейлист + треки |

### Примеры запросов

```bash
# Поиск треков
curl "http://localhost:4000/api/yt/search?q=Daft+Punk&type=song"

# Метаданные трека
curl "http://localhost:4000/api/yt/track/VIDEO_ID"

# Стрим аудио (можно открыть в браузере)
curl "http://localhost:4000/api/yt/stream/VIDEO_ID" --output audio.m4a

# Получить альбом
curl "http://localhost:4000/api/yt/album/ALBUM_BROWSE_ID"

# Получить артиста
curl "http://localhost:4000/api/yt/artist/CHANNEL_ID"
```

### Subsonic Proxy (Navidrome)

| Метод | Путь | Описание |
|-------|------|----------|
| ALL | `/api/subsonic/rest/*` | Проксирует все запросы к Navidrome |

```bash
# Пинг Navidrome через proxy
curl "http://localhost:4000/api/subsonic/rest/ping?u=admin&t=TOKEN&s=SALT&v=1.16.1&c=VeloraSound&f=json"
```

### Health Check

```bash
curl "http://localhost:4000/api/health"
# {"status":"ok","timestamp":1713542400000}
```

## Обработка ошибок

- YouTube API возвращает `502` с `{ error: "...", details: "..." }` при сбоях
- Автоматический retry: при ошибке сессия InnerTube пересоздаётся
- Rate-limiting: 30 запросов/мин на IP для YouTube эндпоинтов
- Кэширование: результаты поиска — 10 мин, стрим-ссылки — 5 мин
- Navidrome proxy возвращает `502` если Navidrome недоступен

## Клавиатурные сочетания

| Клавиша | Действие |
|---------|----------|
| `Space` | Пауза / Воспроизведение |
| `Ctrl + →` | Следующий трек |
| `Ctrl + ←` | Предыдущий трек |
| `L` | Показать/скрыть тексты |

## Структура проекта

```
Velora Sound/
├── docker-compose.yml          # Navidrome + Backend
├── .env                        # MUSIC_FOLDER, PORT
├── backend/
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── index.js            # Express сервер
│       ├── routes/
│       │   ├── youtube.js      # YouTube Music API
│       │   └── subsonic.js     # Navidrome proxy
│       └── services/
│           ├── innertube.js    # YouTube.js singleton
│           └── cache.js        # NodeCache
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── api/                # API клиенты
│       ├── components/         # UI (Layout, Player, Lyrics...)
│       ├── pages/              # Страницы
│       ├── store/              # Zustand стейт
│       └── types/              # TypeScript типы
└── README.md
```
