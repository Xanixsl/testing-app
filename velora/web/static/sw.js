// Velora — Service Worker.
// Стратегия:
//   - SPA-каркас (/, /p/*, /u/*, любые HTML) — network-first → shell из кэша.
//     Это даёт ПОЛНУЮ загрузку в офлайне: SPA отрисовывается, а данные
//     подтягиваются из IDB-кэша API на стороне приложения.
//   - Критическая статика (app.js, *.css, sw.js) — network-first, в кэш.
//   - Прочая /static/* — stale-while-revalidate.
//   - /api/stream и /api/* — НЕ кэшируем (offline-плеер сам играет из IDB).
const VERSION = "velora-sw-v76";
const STATIC_CACHE = "velora-static-v70";
const SHELL_CACHE = "velora-shell-v70";
const API_CACHE = "velora-api-v1";
const SHELL_URL = "/";
const NF_URL = "/static/404.html";

// Базовый набор ассетов, которые нужны, чтобы сайт грузился полностью офлайн.
const PRECACHE_STATIC = [
    "/static/app.js",
    "/static/redesign.css",
    "/static/style.css",
    "/static/mobile.css",
    "/static/manifest.webmanifest",
    "/static/404.html",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(SHELL_CACHE);
        try { await cache.add(SHELL_URL); } catch {}
        const sc = await caches.open(STATIC_CACHE);
        // Прекэш — мягкий: каждый ассет отдельно, чтобы один битый не валил всё.
        await Promise.all(PRECACHE_STATIC.map(async (u) => {
            try { await sc.add(u); } catch {}
        }));
        self.skipWaiting();
    })());
});

self.addEventListener("activate", (event) => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.map(k => {
            if (k !== STATIC_CACHE && k !== SHELL_CACHE && k !== API_CACHE) return caches.delete(k);
        }));
        await self.clients.claim();
    })());
});

function isStatic(url) {
    return url.pathname.startsWith("/static/");
}
// Критическая статика SPA — всегда свежая (network-first), чтобы новые
// версии app.js / стилей не залипали в кэше.
function isCriticalStatic(url) {
    return /^\/static\/(app\.js|redesign\.css|style\.css|sw\.js)$/.test(url.pathname);
}
function isShell(url) {
    if (url.pathname === "/" || url.pathname === "/index.html") return true;
    if (url.pathname.startsWith("/p/") || url.pathname.startsWith("/u/")) return true;
    return false;
}
// Любой запрос документа (Accept: text/html) — это попытка SPA-навигации.
function isHtmlNav(req) {
    if (req.mode === "navigate") return true;
    const a = req.headers.get("accept") || "";
    return a.includes("text/html");
}

self.addEventListener("fetch", (event) => {
    const req = event.request;
    if (req.method !== "GET") return;
    const url = new URL(req.url);
    if (url.origin !== self.location.origin) return;
    // Стрим — никогда не кэшируем (плеер сам берёт из IDB).
    if (url.pathname.startsWith("/api/stream")) return;
    // Прочие GET /api/* — network-first с фолбэком из Cache Storage,
    // чтобы XHR из приложения не падали в офлайне даже если IDB пуст.
    if (url.pathname.startsWith("/api/")) {
        if (req.method !== "GET") return;
        // Не кэшируем запросы с авторизацией Bearer и неидемпотентные ручки.
        event.respondWith((async () => {
            try {
                const resp = await fetch(req);
                if (resp && resp.ok && resp.status === 200 && (resp.headers.get("content-type") || "").includes("json")) {
                    const cache = await caches.open(API_CACHE);
                    // Ограничим размер кэша (примерно 200 записей).
                    cache.put(req, resp.clone()).then(async () => {
                        const ks = await cache.keys();
                        if (ks.length > 200) {
                            const drop = ks.slice(0, ks.length - 200);
                            await Promise.all(drop.map(k => cache.delete(k).catch(()=>{})));
                        }
                    }).catch(()=>{});
                }
                return resp;
            } catch {
                const cache = await caches.open(API_CACHE);
                const cached = await cache.match(req);
                if (cached) {
                    // Помечаем заголовком, чтобы клиент знал что это из кэша.
                    const body = await cached.clone().text();
                    return new Response(body, {
                        status: cached.status,
                        statusText: cached.statusText,
                        headers: { ...Object.fromEntries(cached.headers), "X-Velora-Cache": "1" },
                    });
                }
                // Возвращаем понятный 503-JSON, а не network error — фронт не упадёт.
                return new Response(
                    JSON.stringify({ error: "offline", message: "Нет интернета" }),
                    { status: 503, headers: { "Content-Type": "application/json", "X-Velora-Cache": "miss" } }
                );
            }
        })());
        return;
    }

    if (isStatic(url)) {
        // Для критических SPA-ассетов — network-first, чтобы новые версии
        // подтягивались без ручной очистки кэша.
        if (isCriticalStatic(url)) {
            event.respondWith((async () => {
                try {
                    const resp = await fetch(req, { cache: "no-cache" });
                    if (resp && resp.ok && resp.status === 200) {
                        const cache = await caches.open(STATIC_CACHE);
                        try { await cache.put(req, resp.clone()); } catch {}
                    }
                    return resp;
                } catch {
                    const cache = await caches.open(STATIC_CACHE);
                    const cached = await cache.match(req);
                    if (cached) return cached;
                    throw new Error("offline");
                }
            })());
            return;
        }
        event.respondWith((async () => {
            const cache = await caches.open(STATIC_CACHE);
            const cached = await cache.match(req);
            const network = fetch(req).then(resp => {
                if (resp && resp.ok && resp.status === 200) {
                    cache.put(req, resp.clone()).catch(()=>{});
                }
                return resp;
            }).catch(() => cached);
            return cached || network;
        })());
        return;
    }

    // SPA-каркас или ЛЮБАЯ HTML-навигация — отдаём shell.
    if (isShell(url) || isHtmlNav(req)) {
        event.respondWith((async () => {
            const cache = await caches.open(SHELL_CACHE);
            try {
                const resp = await fetch(req);
                if (resp && resp.ok && resp.status === 200) {
                    cache.put(SHELL_URL, resp.clone()).catch(()=>{});
                }
                return resp;
            } catch (e) {
                // Без сети — отдаём сохранённый shell (тот же что для "/"),
                // SPA увидит произвольный URL и навигирует по своей логике.
                const cached = await cache.match(SHELL_URL);
                if (cached) return cached;
                const sc = await caches.open(STATIC_CACHE);
                const nf = await sc.match(NF_URL);
                if (nf) return nf;
                return new Response("<h1>Offline</h1>", { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 });
            }
        })());
    }
});
