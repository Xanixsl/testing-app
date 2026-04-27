// =================================================================
// Velora Sound — клиент. Vanilla JS SPA в духе Я.Музыки.
// =================================================================
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

// ============== ОТЛАДКА ==============
// Полный дебаг включён ПО УМОЛЧАНИЮ — пишем все api()-вызовы,
// события аудио и PLAY/HEAD в консоль браузера. Отключить: veloraDebug(false).
const VELORA_DEBUG = (() => {
    try {
        if (new URLSearchParams(location.search).get("debug") === "0") {
            localStorage.setItem("velora_debug", "0");
            return false;
        }
        if (new URLSearchParams(location.search).get("debug") === "1") {
            localStorage.setItem("velora_debug", "1");
            return true;
        }
        const stored = localStorage.getItem("velora_debug");
        if (stored === "0") return false;
        return true;  // default ON
    } catch { return true; }
})();
const vlog = (...a) => { if (VELORA_DEBUG) console.log("%c[VELORA]", "color:#ffb14a;font-weight:700", ...a); };
const vwarn = (...a) => { if (VELORA_DEBUG) console.warn("%c[VELORA]", "color:#ff9a4a;font-weight:700", ...a); };
const verr = (...a) => console.error("%c[VELORA]", "color:#ff5b5b;font-weight:700", ...a);
window.veloraDebug = (on=true) => { try { localStorage.setItem("velora_debug", on?"1":"0"); } catch{} location.reload(); };
if (VELORA_DEBUG) console.log("%c[VELORA]", "color:#ffb14a;font-weight:700", "debug ON. Отключить: veloraDebug(false)");

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const fmtTime = (s) => {
    s = Number(s);
    if (!Number.isFinite(s) || s < 0) return "0:00";
    s = Math.floor(s);
    // Если значение явно мусорное (>24ч) — не показываем чудовищ типа "1320:06"
    if (s > 24 * 3600) return "—:—";
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = (s % 60).toString().padStart(2, "0");
    if (h > 0) return `${h}:${m.toString().padStart(2,"0")}:${sec}`;
    return `${m}:${sec}`;
};
const fmtNum = (n) => {
    n = Number(n||0);
    if (n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,"")+" млн";
    if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\.0$/,"")+" тыс";
    return String(n);
};
const setIcon = (svgEl, name) => {
    if (!svgEl) return;
    const u = svgEl.querySelector("use");
    if (u) u.setAttribute("href", "#i-"+name);
};

const TUNE_OPTIONS = {
    occupy: ["Тренировка","За рулём","Дорога","Учёба","Готовка","Уборка","Прогулка","Сон","Вечеринка","Работа"],
    char: [
        {key:"loved",label:"Любимое"},{key:"new",label:"Новинки"},
        {key:"pop",label:"Популярное"},{key:"local",label:"Без английского"},{key:"unfamiliar",label:"Незнакомое"}
    ],
    mood: [
        {key:"happy",label:"Радостно"},{key:"cheer",label:"Бодро"},
        {key:"calm",label:"Спокойно"},{key:"sad",label:"Грустно"}
    ],
    lang: ["Русский","English","На любом"]
};

// =================================================================
// СОСТОЯНИЕ
// =================================================================
const state = {
    me: null,
    sources: { deezer: true, apple: true },
    queue: [],
    qi: -1,
    track: null,
    streamUrl: null,
    duration: 0,
    repeat: "off",   // off | one | all
    shuffle: false,
    likedKeys: new Set(),
    likedById: new Map(),
    history: [],
    playlists: [],
    currentPlaylistId: null,
    currentView: "home",
    eq: { enabled: false, gains: Array(15).fill(0), preset: "flat" },
    audioCtx: null, sourceNode: null, eqNodes: null, gainNode: null,
    eqAnalyser: null, eqAnalyserData: null, eqVizRaf: 0,
    dislikedTrackKeys: new Set(),
    dislikedArtistIds: new Set(),
    karaokeFollow: false,
    lyrics: null,    // { lines:[{t,text}], synced }
    lyricsActive: -1,
    karaokeOpen: false,
    fullOpen: false,
    queueOpen: false,
    trailerMode: null,  // {playlistId, snippet}
    trailerTimer: null,
    pendingImage: null,  // {target,data}
    appleSearch: { tracks: [], artists: [] },
    waveTune: { occupy: null, char: null, mood: null, lang: null },
    // На мобильных по умолчанию настройки волны свёрнуты, чтобы они не «съезжали» под список.
    waveCollapsed: (typeof window !== "undefined" && window.matchMedia && window.matchMedia("(max-width: 900px)").matches),
    offline: {
        downloaded: new Set(),
        enabled: false,
        autoOnDownload: true,
        // Плейлисты, у которых ВСЕ треки уже в офлайне. Обновляется в
        // renderPlaylistPage() и persist'ится в localStorage. Используется
        // для бейджа-галочки на карточках плейлистов в коллекции/поиске.
        fullPlaylists: new Set((() => {
            try { return JSON.parse(localStorage.getItem("velora_full_pls") || "[]"); }
            catch { return []; }
        })()),
    },
};

// Помечаем плейлист как полностью скачанный/не скачанный.
function markPlaylistDownloaded(pid, isFull) {
    if (!pid) return;
    const key = String(pid);
    const set = state.offline.fullPlaylists;
    const had = set.has(key);
    if (isFull) set.add(key); else set.delete(key);
    if (had !== isFull) {
        try { localStorage.setItem("velora_full_pls", JSON.stringify([...set])); } catch {}
    }
}

const audio = $("#audio");

// =================================================================
// API
// =================================================================
async function api(path, opts={}) {
    const o = Object.assign({ headers: {} }, opts);
    if (o.body && typeof o.body === "object" && !(o.body instanceof FormData)) {
        o.headers["Content-Type"] = "application/json";
        o.body = JSON.stringify(o.body);
    }
    const method = (o.method || "GET").toUpperCase();
    // В офлайне (или при потере сети) для безопасных GET сразу пробуем
    // IDB-кэш; для мутирующих запросов выкидываем понятную ошибку.
    if (!navigator.onLine || isOfflineMode()) {
        if (method === "GET" && typeof path === "string" && path.startsWith("/api/")) {
            try {
                const cached = await apiCacheGet(path);
                if (cached != null) {
                    if (VELORA_DEBUG) vlog("📦 (offline-cache)", path);
                    return cached;
                }
            } catch {}
        }
        if (method !== "GET") {
            const err = new Error("offline_no_network");
            err.offline = true;
            throw err;
        }
    }
    const t0 = VELORA_DEBUG ? performance.now() : 0;
    vlog("→", method, path, o.body ? "(body)" : "");
    let r;
    try { r = await fetch(path, o); }
    catch (netErr) {
        // Сети нет — пробуем кэш для GET.
        if (method === "GET" && typeof path === "string" && path.startsWith("/api/")) {
            const cached = await apiCacheGet(path);
            if (cached != null) {
                if (VELORA_DEBUG) vlog("📦 (cache after netErr)", path);
                _setOfflineBanner(true);
                return cached;
            }
        }
        verr("✕ network fail", path, netErr);
        _setOfflineBanner(true);
        throw netErr;
    }
    if (VELORA_DEBUG) {
        const ms = (performance.now()-t0).toFixed(0);
        const tag = r.ok ? "✓" : "✕";
        const fn = r.ok ? vlog : vwarn;
        fn(tag, r.status, path, ms+"ms");
    }
    if (r.status === 401) { state.me = null; renderUserPill(); openAuth(); throw new Error("auth"); }
    if (!r.ok) {
        let msg = "HTTP "+r.status;
        try { const j = await r.json(); if (j.error) msg = j.error; } catch {}
        const quiet = o.silent || (o.silent404 && r.status === 404);
        if (!VELORA_DEBUG && !quiet) verr("api fail", r.status, path, msg);
        throw new Error(msg);
    }
    if (r.status === 204) return null;
    const ct = r.headers.get("content-type")||"";
    const data = ct.includes("json") ? await r.json() : await r.text();
    // Автокэш всех успешных GET /api/* JSON-ответов в IDB.
    if (method === "GET" && ct.includes("json")
        && typeof path === "string" && path.startsWith("/api/")
        && !path.startsWith("/api/stream")) {
        try { apiCachePut(path, data); } catch {}
    }
    return data;
}

// =================================================================
// OFFLINE / IndexedDB cache (треки + метаданные + кэш API/обложек)
// =================================================================
const IDB_NAME = "velora-offline";
const IDB_VER = 2;
const IDB_TRACKS = "tracks";   // {key, blob, meta, coverBlob, size, savedAt}
const IDB_META = "meta";       // {key:"settings", enabled, ...}
const IDB_CACHE = "cache";     // {key:"/api/...", json, savedAt} — снимки GET-ответов

let _idbConn = null;
function idbOpen() {
    if (_idbConn) return Promise.resolve(_idbConn);
    return new Promise((resolve, reject) => {
        if (!("indexedDB" in window)) return reject(new Error("idb_unsupported"));
        const req = indexedDB.open(IDB_NAME, IDB_VER);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(IDB_TRACKS)) db.createObjectStore(IDB_TRACKS, { keyPath: "key" });
            if (!db.objectStoreNames.contains(IDB_META)) db.createObjectStore(IDB_META, { keyPath: "key" });
            if (!db.objectStoreNames.contains(IDB_CACHE)) db.createObjectStore(IDB_CACHE, { keyPath: "key" });
        };
        req.onsuccess = () => { _idbConn = req.result; resolve(_idbConn); };
        req.onerror = () => reject(req.error);
    });
}
function idbReq(store, mode, op) {
    return idbOpen().then(db => new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode);
        const s = tx.objectStore(store);
        const r = op(s);
        if (r && "onsuccess" in r) {
            r.onsuccess = () => resolve(r.result);
            r.onerror = () => reject(r.error);
        } else {
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        }
    }));
}
const trackKey = (t) => (t.source||"")+"|"+(t.source_id||t.id||"");

async function offlineLoadIndex() {
    try {
        // Чистим повреждённые загрузки: m3u8-плейлисты (HLS) или подозрительно
        // маленькие файлы (<100 КБ) — играть их офлайн нельзя.
        try {
            const all = await idbReq(IDB_TRACKS, "readonly", s => s.getAll());
            const bad = [];
            for (const r of (all || [])) {
                if (!r || !r.blob) { if (r && r.key) bad.push(r.key); continue; }
                const ct = (r.blob.type || "").toLowerCase();
                const isHls = ct.includes("mpegurl") || ct.includes("application/vnd.apple") || ct.includes("x-mpegurl");
                let head = "";
                try { head = await r.blob.slice(0, 16).text(); } catch {}
                if (isHls || head.startsWith("#EXTM3U") || (r.blob.size && r.blob.size < 100 * 1024)) {
                    bad.push(r.key);
                }
            }
            if (bad.length) {
                await idbReq(IDB_TRACKS, "readwrite", s => { for (const k of bad) s.delete(k); });
                try { showToast(`Удалены повреждённые загрузки: ${bad.length}. Перекачайте треки.`); } catch {}
            }
        } catch {}
        const all = await idbReq(IDB_TRACKS, "readonly", s => s.getAllKeys());
        state.offline.downloaded = new Set(all || []);
        const m = await idbReq(IDB_META, "readonly", s => s.get("settings"));
        if (m) state.offline.enabled = !!m.enabled;
    } catch {}
}
async function offlineSaveSettings() {
    try { await idbReq(IDB_META, "readwrite", s => s.put({ key: "settings", enabled: state.offline.enabled })); } catch {}
}
async function offlineHas(t) {
    return state.offline.downloaded.has(trackKey(t));
}
async function offlineGetBlobUrl(t) {
    try {
        // 1) Точное совпадение по source|source_id.
        let rec = await idbReq(IDB_TRACKS, "readonly", s => s.get(trackKey(t)));
        // 2) Фолбэк: тот же source_id, но другой source (например,
        //    скачано через soundcloud, а трек пришёл из deezer-листа).
        if (!rec || !rec.blob) {
            const sid = String(t.source_id || t.id || "");
            if (sid) {
                const all = await idbReq(IDB_TRACKS, "readonly", s => s.getAll());
                rec = (all || []).find(r => r && r.blob && String(r.meta?.source_id || "") === sid)
                   || (all || []).find(r => r && r.blob
                        && (r.meta?.title || "").toLowerCase() === (t.title || "").toLowerCase()
                        && (r.meta?.artist || "").toLowerCase() === (t.artist || "").toLowerCase());
            }
        }
        if (rec && rec.blob) {
            // Подсинхронизируем in-memory Set, чтобы playQueue не отфильтровал.
            try { state.offline.downloaded.add(rec.key); } catch {}
            return URL.createObjectURL(rec.blob);
        }
    } catch {}
    return null;
}
// Кэш blob-URL обложек, чтобы не пересоздавать их на каждый рендер.
const _coverUrlCache = new Map();   // key → object URL
async function offlineGetCoverUrl(t) {
    const k = trackKey(t);
    if (_coverUrlCache.has(k)) return _coverUrlCache.get(k);
    try {
        const rec = await idbReq(IDB_TRACKS, "readonly", s => s.get(k));
        if (rec && rec.coverBlob) {
            const u = URL.createObjectURL(rec.coverBlob);
            _coverUrlCache.set(k, u);
            return u;
        }
    } catch {}
    return null;
}
// Универсальный кэш GET /api/* в IDB. Сохраняем JSON-ответы — затем
// `apiCached(path)` сначала пробует сеть, при ошибке возвращает кэш.
async function apiCachePut(path, json) {
    try { await idbReq(IDB_CACHE, "readwrite", s => s.put({ key: path, json, savedAt: Date.now() })); }
    catch {}
}

// _recordVisit({kind,id,source,name,artist,cover}) — fire-and-forget POST на
// /api/visit. Используется для построения TasteSnapshot. Дроссель: один и
// тот же (kind|id) не отправляется чаще раза в 30 сек.
const _visitThrottle = new Map();
function _recordVisit(payload) {
    try {
        if (!payload || !payload.id || !payload.kind) return;
        if (!state.user) return;
        if (!navigator.onLine) return;
        const key = `${payload.kind}|${payload.id}|${payload.source||"deezer"}`;
        const now = Date.now();
        const last = _visitThrottle.get(key) || 0;
        if (now - last < 30000) return;
        _visitThrottle.set(key, now);
        api("/api/visit", { method: "POST", body: JSON.stringify(payload), silent: true }).catch(() => {});
    } catch {}
}
async function apiCacheGet(path) {
    try {
        const rec = await idbReq(IDB_CACHE, "readonly", s => s.get(path));
        return rec ? rec.json : null;
    } catch { return null; }
}
async function apiCached(path, opts = {}) {
    // В офлайне — никаких сетевых попыток (иначе консоль завалена «Failed to fetch»).
    if (isOfflineMode()) {
        const cached = await apiCacheGet(path);
        if (cached != null) return cached;
        throw new Error("offline_no_cache");
    }
    try {
        const j = await api(path, { silent: true, silent404: true, ...opts });
        // Сохраняем только не-null/не-error результаты.
        if (j != null) apiCachePut(path, j);
        return j;
    } catch (e) {
        const cached = await apiCacheGet(path);
        if (cached != null) return cached;
        throw e;
    }
}
async function downloadTrack(t, onProgress) {
    if (offlineBlocked("Скачивание")) throw new Error("offline");
    const key = trackKey(t);
    if (state.offline.downloaded.has(key)) return { skipped: true };
    // quality=low — резолвер выдаёт opus@<=96 kbps вместо «бесового»
    // потока. Без перекодирования (без ffmpeg) — просто выбор более лёгкого
    // формата из тех, что предлагает источник. Экономия ~30–60% места,
    // потери на слух не различимы.
    const url = `/api/stream?source=${encodeURIComponent(t.source||"")}&source_id=${encodeURIComponent(t.source_id||"")}&q=${encodeURIComponent((t.artist||"")+" "+(t.title||""))}&duration=${t.duration||0}&quality=low${t.explicit?"&explicit=1":""}${t.preview_url?`&preview=${encodeURIComponent(t.preview_url)}`:""}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error("HTTP "+resp.status);
    // ⚠️ HLS-плейлисты (m3u8) играть офлайн нельзя — внутри только ссылки на сегменты CDN.
    // Отказываемся сохранять такой «трек», иначе в офлайне будет SRC_NOT_SUPPORTED.
    const ctype = (resp.headers.get("content-type") || "").toLowerCase();
    if (ctype.includes("mpegurl") || ctype.includes("application/vnd.apple") || ctype.includes("x-mpegurl")) {
        throw new Error("Этот трек идёт через HLS-стрим (SoundCloud) — оффлайн-загрузка недоступна.");
    }
    // Стараемся получить размер для прогресса.
    const total = Number(resp.headers.get("content-length") || 0);
    let received = 0;
    const reader = resp.body && resp.body.getReader ? resp.body.getReader() : null;
    let blob;
    if (reader) {
        const chunks = [];
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value); received += value.length;
            if (onProgress) onProgress(total ? received / total : 0, received);
        }
        blob = new Blob(chunks, { type: resp.headers.get("content-type") || "audio/mpeg" });
    } else {
        blob = await resp.blob();
    }
    // Доп. защита: если контент начинается с #EXTM3U — это тоже HLS, сохранять нельзя.
    try {
        const head = await blob.slice(0, 16).text();
        if (head.startsWith("#EXTM3U")) {
            throw new Error("Этот трек идёт через HLS-стрим (SoundCloud) — оффлайн-загрузка недоступна.");
        }
    } catch (e) {
        if (String(e.message||"").includes("HLS")) throw e;
    }
    const meta = {
        source: t.source, source_id: String(t.source_id || t.id || ""),
        title: t.title || "", artist: t.artist || "", album: t.album || "",
        cover: t.album_cover || t.cover_big || t.cover_small || "",
        duration: t.duration || 0, artist_id: t.artist_id || "",
        explicit: !!t.explicit,
    };
    // Дотягиваем обложку как Blob — чтобы её можно было показать офлайн.
    let coverBlob = null;
    if (meta.cover) {
        try {
            const cr = await fetch(meta.cover, { mode: "cors" });
            if (cr.ok) coverBlob = await cr.blob();
        } catch {}
    }
    await idbReq(IDB_TRACKS, "readwrite", s => s.put({
        key, blob, meta, coverBlob, size: blob.size + (coverBlob?coverBlob.size:0),
        savedAt: Date.now(),
    }));
    state.offline.downloaded.add(key);
    return { saved: true, size: blob.size };
}
async function deleteDownload(t) {
    const key = typeof t === "string" ? t : trackKey(t);
    await idbReq(IDB_TRACKS, "readwrite", s => s.delete(key));
    state.offline.downloaded.delete(key);
}
async function listDownloads() {
    try { return (await idbReq(IDB_TRACKS, "readonly", s => s.getAll())) || []; }
    catch { return []; }
}
async function clearAllDownloads() {
    try {
        await idbReq(IDB_TRACKS, "readwrite", s => s.clear());
        state.offline.downloaded.clear();
    } catch {}
}

// ============== OFFLINE-MODE HELPERS ==============
// Считаем «офлайн» — когда нет сети ИЛИ пользователь сам включил «только офлайн».
function isOfflineMode() {
    if (!navigator.onLine) return true;
    return !!(state.offline && state.offline.enabled);
}
// Видимый баннер сверху страницы. forced=true — баннер «нет сети» (красный).
let _offlineBannerEl = null;
function _ensureOfflineBanner() {
    if (_offlineBannerEl) return _offlineBannerEl;
    const el = document.createElement("div");
    el.id = "offlineBanner";
    el.className = "offline-banner";
    el.hidden = true;
    el.innerHTML = `
        <span class="ob-dot" aria-hidden="true"></span>
        <span class="ob-text"></span>
        <button type="button" class="ob-retry" title="Проверить соединение">↻</button>
    `;
    document.body.appendChild(el);
    el.querySelector(".ob-retry")?.addEventListener("click", async () => {
        try {
            const r = await fetch("/api/me", { cache: "no-store" });
            if (r.ok) { _setOfflineBanner(false); showToast("Сеть в порядке"); }
            else throw new Error("offline");
        } catch { showToast("Сеть всё ещё недоступна"); }
    });
    _offlineBannerEl = el;
    return el;
}
function _setOfflineBanner(forced) {
    const el = _ensureOfflineBanner();
    const offline = !navigator.onLine || forced;
    const userOnly = !!(state.offline && state.offline.enabled);
    if (offline) {
        el.classList.add("ob-bad");
        el.classList.remove("ob-info");
        el.querySelector(".ob-text").textContent = "Нет интернета — показываем сохранённые данные";
        el.hidden = false;
    } else if (userOnly) {
        el.classList.add("ob-info");
        el.classList.remove("ob-bad");
        el.querySelector(".ob-text").textContent = "Режим «Только офлайн» — мобильные данные не используются";
        el.hidden = false;
    } else {
        el.hidden = true;
    }
}
// Применяет класс body.offline-mode и хайдит/дисейблит UI, недоступный офлайн.
function applyOfflineUi() {
    const off = isOfflineMode();
    document.body.classList.toggle("offline-mode", off);
    _setOfflineBanner(!navigator.onLine);
}
// Запрещает действие если офлайн. Показывает тост и возвращает true (заблокировано).
function offlineBlocked(action = "Это действие") {
    if (isOfflineMode()) { showToast(`${action} недоступно офлайн`); return true; }
    return false;
}
window.addEventListener("online", () => {
    applyOfflineUi();
    _setOfflineBanner(false);
    showToast("✓ Сеть восстановлена");
});
window.addEventListener("offline", () => {
    applyOfflineUi();
    _setOfflineBanner(true);
    showToast("📡 Нет интернета — переключаемся в офлайн-режим");
});
// При старте — сразу показать баннер, если офлайн.
document.addEventListener("DOMContentLoaded", () => {
    try { applyOfflineUi(); } catch {}
});
function fmtBytes(n) {
    if (!n) return "0 Б";
    if (n < 1024) return n + " Б";
    if (n < 1024 * 1024) return (n/1024).toFixed(1) + " КБ";
    if (n < 1024 * 1024 * 1024) return (n/1024/1024).toFixed(1) + " МБ";
    return (n/1024/1024/1024).toFixed(2) + " ГБ";
}

// Нормализация треков: сервер отдаёт {id, cover_big, cover_small},
// а клиентский код исторически использует {source_id, album_cover}.
function normTrack(t) {
    if (!t || typeof t !== "object") return t;
    if (t.source_id == null && t.id != null) t.source_id = String(t.id);
    if (t.id == null && t.source_id != null) t.id = t.source_id;
    if (!t.album_cover) t.album_cover = t.cover_big || t.cover_small || "";
    if (!t.cover_big) t.cover_big = t.album_cover || "";
    if (!t.cover_small) t.cover_small = t.album_cover || "";
    return t;
}
function asTracks(d) {
    if (Array.isArray(d)) return d.map(normTrack);
    if (d && Array.isArray(d.tracks)) return d.tracks.map(normTrack);
    if (d && Array.isArray(d.items)) return d.items.map(normTrack);
    return [];
}
function asArr(d, key) {
    if (Array.isArray(d)) return d;
    if (d && key && Array.isArray(d[key])) return d[key];
    return [];
}

async function loadMe() {
    try {
        const j = await apiCached("/api/me");
        state.me = (j && j.authenticated) ? j : null;
    }
    catch (e) { state.me = null; }
    renderUserPill();
    applyGuestUi();
    // Админка убрана с сайта — она теперь только в Telegram-боте (@saylont).
    if (state.me) {
        await loadLikes(); await loadPlaylists(); await loadDislikes(); await loadArtistPrefs();
        applyUserPalette();
    }
}

// Палитра по доминирующему жанру: меняем CSS-переменные --accent / --accent-2 / --accent-3.
async function applyUserPalette() {
    if (!state.me) return;
    try {
        const p = await api("/api/taste/palette", { silent: true });
        const root = document.documentElement;
        if (p && p.palette) {
            root.style.setProperty("--accent", p.palette.accent);
            root.style.setProperty("--accent-2", p.palette.accent2);
            root.style.setProperty("--accent-3", p.palette.accent3);
            if (p.genre) document.body.dataset.genre = p.genre;
            else delete document.body.dataset.genre;
            vlog("palette applied", p.genre, p.palette);
        }
    } catch (e) { vlog("palette err", e); }
}

// Скрывает/показывает функции, недоступные гостям.
// Гостю доступно ТОЛЬКО: Поиск, Чарты, Главная (волна) — без настроек.
// Недоступно: Коллекция, История, Лайки, Дизлайки, Скачивание, EQ, Качество, Полноэкран.
function applyGuestUi() {
    const guest = !state.me;
    document.body.classList.toggle("is-guest", guest);
    // Авторизованным — никаких меток PREVIEW.
    if (!guest) {
        const npBadge = $("#np-preview-badge"); if (npBadge) npBadge.hidden = true;
        document.body.classList.remove("preview-mode");
        state._isPreview = false;
    }
    // Сайдбар: скрываем недоступные пункты
    $$(".nav-item").forEach(el => {
        const v = el.dataset.view;
        const blocked = guest && (v === "library" || v === "history");
        el.style.display = blocked ? "none" : "";
    });
    // Плеер
    const playerHide = ["#likeBtn","#dlBtn","#eqBtn","#fullBtn","#qualitySel"];
    playerHide.forEach(sel => { const el = $(sel); if (el) el.style.display = guest ? "none" : ""; });
    // Полноэкран — на всякий случай скроем кнопку (для гостя плеер всё равно не должен открываться)
    if (guest) {
        const fp = $("#fullplayer"); if (fp) fp.hidden = true;
    }
}
async function loadDislikes() {
    try {
        const arr = await apiCached("/api/dislikes");
        state.dislikedTrackKeys = new Set();
        state.dislikedArtistIds = new Set();
        (Array.isArray(arr)?arr:[]).forEach(d => {
            if (d.scope === "artist" && d.artist_id) state.dislikedArtistIds.add(String(d.artist_id));
            else if (d.track_id) state.dislikedTrackKeys.add((d.source||"")+"|"+d.track_id);
        });
    } catch {}
}
async function loadArtistPrefs() {
    try {
        const arr = await apiCached("/api/artists/preferences");
        state.artistPrefs = new Map();
        (Array.isArray(arr)?arr:[]).forEach(p => {
            if (p && p.artist_id && p.kind) {
                state.artistPrefs.set(`${p.source||"deezer"}|${p.artist_id}`, p.kind);
            }
        });
    } catch { if (!state.artistPrefs) state.artistPrefs = new Map(); }
}
async function loadLikes() {
    try {
        const j = await apiCached("/api/likes");
        state.likedKeys = new Set();
        state.likedById = new Map();
        asTracks(j).forEach(t => {
            const k = (t.source||"")+"|"+(t.source_id||"");
            state.likedKeys.add(k); state.likedById.set(k, t);
        });
    } catch {}
}
async function loadPlaylists() {
    try { state.playlists = asArr(await apiCached("/api/playlists"), "playlists"); }
    catch { state.playlists = []; }
    renderSidebarPlaylists();
}

// =================================================================
// ТОСТ
// =================================================================
function showToast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { t.hidden = true; }, 2400);
}

// =================================================================
// AUTH — мульти-шаговый флоу: pick → (classic | email-1/2/3 | tg-1/2/3)
// =================================================================
const authState = {
    step: "pick",
    classicMode: "login",         // login | register
    email: "",
    emailTtl: 0,
    emailTimer: null,
    tgToken: "",
    tgPhone: "",
    tgPollTimer: null,
    countries: [],
    selectedCountry: { iso: "RU", code: "7", flag: "🇷🇺", name: "Россия" },
};

function authShowStep(step) {
    authState.step = step;
    $$("#authModal .auth-step").forEach(el => {
        el.hidden = el.dataset.step !== step;
    });
}

// Переключатель этапов внутри auth-модалки: phone | share | code
function authShowPane(name) {
    authState.pane = name;
    $$("#authModal [data-auth-pane]").forEach(p => {
        p.hidden = p.dataset.authPane !== name;
        p.classList.toggle("is-active", p.dataset.authPane === name);
    });
    if (name === "code") {
        setTimeout(() => $("#loginCodeInput")?.focus(), 60);
    } else if (name === "phone") {
        setTimeout(() => $("#phoneInput")?.focus(), 60);
    }
}

function openAuth(_mode) {
    phoneResetUI();
    authShowPane("phone");
    $$("#authModal .auth-error").forEach(e => e.hidden = true);
    $("#authModal").hidden = false;
    mountTelegramWidget();
}

// ---------- Telegram Login Widget ----------
// Подгружает официальный <script telegram-widget.js> с data-telegram-login
// и привязывает глобальный window.onTelegramAuth(user) — отправляем user
// на бекенд, где валидируется HMAC и создаётся сессия. Имя бота берём из
// /api/auth/tg/bot. Domain должен быть прописан в @BotFather → /setdomain.
let _tgWidgetMounted = false;
async function mountTelegramWidget() {
    if (_tgWidgetMounted) return;
    const wrap = document.getElementById("tgLoginWidgetWrap");
    const host = document.getElementById("tgLoginWidget");
    if (!wrap || !host) return;
    let username = "";
    try {
        const r = await fetch("/api/auth/tg/bot");
        const j = await r.json();
        username = (j && j.username) || "";
    } catch {}
    if (!username) return;             // бот не настроен — оставляем wrap скрытым
    window.onTelegramAuth = async (user) => {
        try {
            const r = await fetch("/api/auth/tg/widget", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(user || {}),
            });
            const j = await r.json().catch(() => ({}));
            if (r.ok && j.ok) {
                showToast(j.is_new ? "Аккаунт создан — заходим…"
                                   : "Вход выполнен — обновляем страницу…");
                setTimeout(() => { window.location.reload(); }, 400);
            } else {
                showToast(j.message || "Не удалось войти через Telegram");
            }
        } catch {
            showToast("Сеть недоступна, попробуйте ещё раз");
        }
    };
    const s = document.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?23";
    s.setAttribute("data-telegram-login", username);
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "20");
    s.setAttribute("data-onauth", "onTelegramAuth(user)");
    s.setAttribute("data-request-access", "write");
    host.appendChild(s);
    wrap.hidden = false;
    _tgWidgetMounted = true;
}

// Закрытие
$("#authModal").addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === e.currentTarget) {
        $("#authModal").hidden = true;
        try { authStopTimers(); } catch {}
    }
});

function authStopTimers() { try { phoneStopPolling(); } catch {} }

function showAuthErr(id, msg) {
    const el = $("#"+id); if (!el) return;
    el.textContent = msg; el.hidden = false;
}
function clearAuthErr(id) { const el = $("#"+id); if (el) el.hidden = true; }

// =================================================================
// PHONE-ONLY AUTH (через Telegram-бота)
// =================================================================
const phoneState = {
    token: null,        // токен текущей сессии входа
    mode: null,         // "register" | "login"
    pollTimer: null,    // setInterval handle
    normalized: null,   // {e164, country, dial_code, national}
    busy: false,
    selectedCountry: { iso: "RU", dial_code: "+7", flag: "🇷🇺", name: "Россия" },
};

function phoneResetUI() {
    phoneStopPolling();
    phoneState.token = null;
    phoneState.mode = null;
    phoneState.normalized = null;
    const err = $("#phoneError"); if (err) err.hidden = true;
    const meta = $("#phoneMeta"); if (meta) meta.textContent = "";
    setSelectedCountry(phoneState.selectedCountry || { iso: "RU", dial_code: "+7", flag: "🇷🇺", name: "Россия" });
    const inp = $("#phoneInput"); if (inp) inp.value = "";
    const lb = $("#phoneLoginBtn"); if (lb) { lb.disabled = false; lb.textContent = "Войти по коду"; }
    const rb = $("#phoneRegisterBtn"); if (rb) { rb.disabled = false; rb.textContent = "Зарегистрироваться"; }
    const code = $("#loginCodeInput"); if (code) code.value = "";
    const ce = $("#loginCodeError"); if (ce) ce.hidden = true;
    closeCountryDropdown();
}

function phoneStopPolling() {
    if (phoneState.pollTimer) {
        clearInterval(phoneState.pollTimer);
        phoneState.pollTimer = null;
    }
}

// ---------- Country picker ----------
let _countriesCache = null;
let _countryDropdownOpen = false;

function setSelectedCountry(c) {
    if (!c) return;
    phoneState.selectedCountry = c;
    const lbl = $("#phoneCcLabel"); if (lbl) lbl.textContent = c.dial_code || "";
    const flg = $("#phoneFlag"); if (flg) flg.textContent = c.flag || "";
}

async function loadCountriesOnce() {
    if (_countriesCache) return _countriesCache;
    try {
        const r = await fetch("/api/auth/phone/countries").then(r => r.json());
        _countriesCache = (r && r.items) || [];
    } catch {
        _countriesCache = [];
    }
    return _countriesCache;
}

function renderCountryList(filter) {
    const list = $("#countryList"); if (!list) return;
    const f = (filter || "").trim().toLowerCase();
    const items = (_countriesCache || []).filter(c => {
        if (!f) return true;
        return c.name.toLowerCase().includes(f)
            || c.iso.toLowerCase().includes(f)
            || c.dial_code.includes(f.replace(/^\+/, ""))
            || c.dial_code.includes(f);
    });
    if (!items.length) {
        list.innerHTML = `<div class="country-empty">Ничего не найдено</div>`;
        return;
    }
    const sel = phoneState.selectedCountry?.iso;
    list.innerHTML = items.map(c => `
        <div class="country-item${c.iso === sel ? " is-active" : ""}" data-iso="${c.iso}" data-dial="${c.dial_code}" data-flag="${c.flag}" data-name="${c.name.replace(/"/g, "&quot;")}">
            <span class="country-item-flag">${c.flag}</span>
            <span class="country-item-name">${c.name}</span>
            <span class="country-item-dial">${c.dial_code}</span>
        </div>
    `).join("");
}

function openCountryDropdown() {
    const dd = $("#countryDropdown"); if (!dd) return;
    dd.hidden = false;
    _countryDropdownOpen = true;
    $("#phoneCcBtn")?.setAttribute("aria-expanded", "true");
    loadCountriesOnce().then(() => {
        renderCountryList("");
        // Прокручиваем к выбранной стране.
        const list = $("#countryList");
        const active = list?.querySelector(".country-item.is-active");
        if (active && list) list.scrollTop = Math.max(0, active.offsetTop - 60);
    });
}

function closeCountryDropdown() {
    const dd = $("#countryDropdown"); if (!dd) return;
    dd.hidden = true;
    _countryDropdownOpen = false;
    $("#phoneCcBtn")?.setAttribute("aria-expanded", "false");
}

$("#phoneCcBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (_countryDropdownOpen) closeCountryDropdown();
    else openCountryDropdown();
});

$("#countryList")?.addEventListener("click", (e) => {
    const it = e.target.closest(".country-item");
    if (!it) return;
    setSelectedCountry({
        iso: it.dataset.iso,
        dial_code: it.dataset.dial,
        flag: it.dataset.flag,
        name: it.dataset.name,
    });
    closeCountryDropdown();
    // Если уже что-то введено — пере-нормализуем с новым префиксом.
    const inp = $("#phoneInput");
    if (inp && inp.value.trim()) {
        phoneNormalize((it.dataset.dial || "") + inp.value);
    }
    inp?.focus();
});

// Закрыть дропдаун по клику вне и по Escape.
document.addEventListener("click", (e) => {
    if (!_countryDropdownOpen) return;
    if (e.target.closest("#countryDropdown") || e.target.closest("#phoneCcBtn")) return;
    closeCountryDropdown();
});
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _countryDropdownOpen) closeCountryDropdown();
});

// Поиск внутри дропдауна.
$("#countrySearch")?.addEventListener("input", (e) => {
    renderCountryList(e.target.value);
});

// ---------- Соц-входы ----------
$$("#authModal .auth-social-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
        const provider = btn.dataset.provider;
        if (provider === "telegram") {
            // Создаём уникальный intent-токен и открываем
            // t.me/<bot>?start=intent_<token>. Тапая «Start» в чате, юзер
            // отправит `/start intent_<token>`, бот распознаёт его и сразу
            // авто-логинит привязанный аккаунт; параллельно поллим статус.
            const oldText = btn.textContent;
            btn.disabled = true;
            btn.textContent = "Готовим…";
            try {
                const r = await fetch("/api/auth/tg/intent", { method: "POST" });
                const j = await r.json();
                if (!r.ok || !j.deep_link) {
                    showToast(j.message || "Telegram-бот не настроен");
                    return;
                }
                window.open(j.deep_link, "_blank", "noopener");
                showToast("Откройте Telegram и нажмите «Start» в чате с ботом");
                _tgIntentPoll(j.token);
            } catch {
                showToast("Не удалось открыть Telegram");
            } finally {
                btn.disabled = false;
                btn.textContent = oldText;
            }
            return;
        }
        // Google — серверная инициация OAuth.
        try {
            const r = await fetch(`/api/auth/oauth/${provider}/start`);
            const j = await r.json();
            if (r.ok && j.url) {
                window.location.href = j.url;
                return;
            }
            showToast(j.message || "Этот способ ещё не настроен");
        } catch {
            showToast("Не удалось начать вход");
        }
    });
});

// Поллинг intent-токена: ждём, пока бот пометит verified+user_id, и логинимся.
let _tgIntentTimer = null;
function _tgIntentPoll(token) {
    if (!token) return;
    if (_tgIntentTimer) { clearInterval(_tgIntentTimer); _tgIntentTimer = null; }
    const started = Date.now();
    const tick = async () => {
        try {
            const r = await fetch("/api/auth/tg/intent/poll?t=" + encodeURIComponent(token));
            const j = await r.json().catch(() => ({}));
            if (r.ok && j.status === "ok") {
                clearInterval(_tgIntentTimer); _tgIntentTimer = null;
                showToast("Вход выполнен — обновляем страницу…");
                setTimeout(() => { window.location.reload(); }, 400);
                return;
            }
            if (r.status === 410 || j.status === "expired" || (Date.now() - started) > 15 * 60 * 1000) {
                clearInterval(_tgIntentTimer); _tgIntentTimer = null;
                showToast("Срок действия ссылки истёк");
                return;
            }
        } catch {}
    };
    _tgIntentTimer = setInterval(tick, 2500);
    setTimeout(tick, 800);
}

// Дебаунс-нормализация номера: сервер чинит «+7+7…» и подставляет страну.
let _phoneNormTimer = null;
async function phoneNormalize(raw) {
    try {
        const r = await fetch("/api/auth/phone/normalize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone: raw }),
        }).then(r => r.json().then(j => ({ status: r.status, j })));
        if (r.status === 200 && r.j.ok) {
            phoneState.normalized = r.j;
            // Авто-синк селектора со страной из распознанного номера.
            if (r.j.country) {
                const list = await loadCountriesOnce();
                const found = list.find(c => c.iso === r.j.country);
                if (found && phoneState.selectedCountry?.iso !== found.iso) {
                    setSelectedCountry(found);
                }
            }
            const meta = $("#phoneMeta"); if (meta) meta.textContent =
                `${phoneState.selectedCountry?.name || r.j.country || ""} · ${r.j.e164}`.trim();
            const inp = $("#phoneInput");
            if (inp && document.activeElement !== inp) {
                inp.value = r.j.national || "";
            }
            const err = $("#phoneError"); if (err) err.hidden = true;
        } else {
            phoneState.normalized = null;
            const meta = $("#phoneMeta"); if (meta) meta.textContent = "";
        }
    } catch {
        phoneState.normalized = null;
    }
}

$("#phoneInput")?.addEventListener("input", (e) => {
    const v = e.target.value;
    clearTimeout(_phoneNormTimer);
    _phoneNormTimer = setTimeout(() => phoneNormalize(v), 250);
});

$("#phoneInput")?.addEventListener("blur", (e) => {
    if (phoneState.normalized && phoneState.normalized.national) {
        e.target.value = phoneState.normalized.national;
    }
});

$("#phoneInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#phoneLoginBtn")?.click(); }
});

async function phoneStartFlow(mode) {
    if (phoneState.busy) return;
    const inp = $("#phoneInput");
    const cc = phoneState.selectedCountry?.dial_code || $("#phoneCcLabel")?.textContent || "+7";
    const raw = (phoneState.normalized?.e164) || (cc + (inp?.value || ""));
    clearAuthErr("phoneError");
    if (!raw || raw.replace(/\D/g, "").length < 7) {
        return showAuthErr("phoneError", "Введите номер телефона");
    }
    // Регистрация: не дёргаем сервер сразу — сначала покажем форму
    // (имя пользователя / отображаемое имя / дата рождения).
    // Запрос /api/auth/phone/start делает кнопка «Открыть Telegram» в share-pane.
    if (mode === "register") {
        phoneState.pendingPhone = raw;
        clearAuthErr("regError");
        // Сбрасываем поля, чтобы при повторном открытии не было артефактов.
        const u = $("#regUsername"); if (u) u.value = u.value || "";
        authShowPane("share");
        // Подписываем live-проверку доступности ника (один раз).
        const ru = $("#regUsername"), rh = $("#regUsernameHint");
        if (ru && rh && !ru.dataset.liveAttached) {
            attachUsernameLiveCheck(ru, rh, "");
            ru.dataset.liveAttached = "1";
        }
        return;
    }
    phoneState.busy = true;
    const lb = $("#phoneLoginBtn"); const rb = $("#phoneRegisterBtn");
    const activeBtn = mode === "login" ? lb : rb;
    if (lb) lb.disabled = true; if (rb) rb.disabled = true;
    const oldText = activeBtn?.textContent;
    if (activeBtn) activeBtn.textContent = "Готовим…";
    try {
        const r = await fetch("/api/auth/phone/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone: raw, mode }),
        });
        const j = await r.json();
        if (!r.ok) {
            // Спец-обработка: если выбрали «Регистрация», но аккаунт уже есть — предложим войти.
            if (r.status === 409 && j.error === "already_registered") {
                return showAuthErr("phoneError", j.message || "Этот номер уже зарегистрирован. Нажмите «Войти по коду».");
            }
            if (r.status === 404 && j.error === "no_account") {
                return showAuthErr("phoneError", j.message || "Аккаунта нет. Нажмите «Зарегистрироваться».");
            }
            throw new Error(j.message || j.error || "Ошибка");
        }
        phoneState.token = j.token;
        phoneState.mode = j.mode;
        phoneState.normalized = j;
        if (j.deep_link) {
            try { window.open(j.deep_link, "_blank", "noopener"); } catch {}
        }
        const link = $("#codeOpenLink");
        if (link && j.deep_link) link.href = j.deep_link;
        authShowPane("code");
    } catch (err) {
        showAuthErr("phoneError", (err && err.message) || "Не удалось начать");
    } finally {
        phoneState.busy = false;
        if (lb) lb.disabled = false;
        if (rb) rb.disabled = false;
        if (activeBtn && oldText != null) activeBtn.textContent = oldText;
    }
}

// Живая проверка доступности `@ника` через GET /api/auth/username/check.
// Подписывает обработчик ввода с дебаунсом 350мс и красит подсказку.
function attachUsernameLiveCheck(input, hintEl, currentUsername) {
    if (!input || !hintEl) return;
    const original = (currentUsername || "").toLowerCase();
    let timer = null;
    let lastReq = 0;
    const baseClass = hintEl.className;
    const setHint = (text, kind) => {
        hintEl.textContent = text;
        hintEl.className = baseClass + " uname-status " + (kind ? "uname-" + kind : "");
    };
    input.addEventListener("input", () => {
        if (timer) clearTimeout(timer);
        const v = input.value.trim().replace(/^@/, "");
        if (!v) { setHint("3–32 символа: латиница, цифры, _ . -", ""); return; }
        if (v.toLowerCase() === original) { setHint("Это ваш текущий ник", "ok"); return; }
        if (!/^[a-zA-Z0-9_.\-]{3,32}$/.test(v)) {
            setHint("3–32 символа: латиница, цифры, _ . -", "bad"); return;
        }
        setHint("Проверяем…", "");
        timer = setTimeout(async () => {
            const reqId = ++lastReq;
            try {
                const r = await fetch("/api/auth/username/check?u=" + encodeURIComponent(v));
                const j = await r.json();
                if (reqId !== lastReq) return; // ответ устарел
                setHint((j.available ? "✓ " : "✗ ") + (j.message || ""), j.available ? "ok" : "bad");
            } catch {
                if (reqId !== lastReq) return;
                setHint("Не удалось проверить", "");
            }
        }, 350);
    });
}

// Реальный запуск register-flow по клику «Открыть Telegram» в share-pane.
async function phoneRegisterSubmit() {
    if (phoneState.busy) return;
    clearAuthErr("regError");
    const phone = phoneState.pendingPhone || phoneState.normalized?.e164;
    if (!phone) {
        showAuthErr("regError", "Сначала введите номер телефона");
        authShowPane("phone"); return;
    }
    const username = ($("#regUsername")?.value || "").trim().replace(/^@/, "");
    const display_name = ($("#regDisplay")?.value || "").trim();
    const dob = ($("#regDob")?.value || "").trim();
    if (username && !/^[a-zA-Z0-9_.\-]{3,32}$/.test(username)) {
        return showAuthErr("regError", "Имя пользователя: 3–32 символа, латиница/цифры/_.-");
    }
    if (dob) {
        const d = new Date(dob + "T00:00:00");
        if (isNaN(+d) || d > new Date()) return showAuthErr("regError", "Неверная дата рождения");
    }
    phoneState.busy = true;
    const btn = $("#shareOpenLink");
    const oldText = btn?.textContent;
    if (btn) btn.textContent = "Готовим…";
    try {
        const r = await fetch("/api/auth/phone/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                phone, mode: "register",
                username: username || undefined,
                display_name: display_name || undefined,
                dob: dob || undefined,
            }),
        });
        const j = await r.json();
        if (!r.ok) {
            return showAuthErr("regError", j.message || j.error || "Ошибка");
        }
        phoneState.token = j.token;
        phoneState.mode = j.mode;
        phoneState.normalized = j;
        if (j.deep_link) {
            // На том же клике сразу открываем бота в новой вкладке/приложении.
            try {
                if (btn) { btn.href = j.deep_link; }
                window.open(j.deep_link, "_blank", "noopener");
            } catch {}
        }
        phonePollStart();
    } catch (err) {
        showAuthErr("regError", (err && err.message) || "Не удалось");
    } finally {
        phoneState.busy = false;
        if (btn && oldText != null) btn.textContent = oldText;
    }
}

$("#phoneLoginBtn")?.addEventListener("click", () => phoneStartFlow("login"));
$("#phoneRegisterBtn")?.addEventListener("click", () => phoneStartFlow("register"));

// В share-pane клик «Открыть Telegram» = реальный сабмит регистрации.
document.addEventListener("click", (e) => {
    const a = e.target.closest("#shareOpenLink");
    if (!a) return;
    e.preventDefault();
    phoneRegisterSubmit();
});

$("#shareCancelBtn")?.addEventListener("click", () => {
    phoneStopPolling();
    authShowPane("phone");
});

$("#loginCodeCancelBtn")?.addEventListener("click", () => {
    authShowPane("phone");
});

// Авто-форматирование «123-456»: показываем дефис после 3-й цифры.
$("#loginCodeInput")?.addEventListener("input", (e) => {
    const digits = (e.target.value || "").replace(/\D/g, "").slice(0, 6);
    e.target.value = digits.length > 3
        ? digits.slice(0, 3) + "-" + digits.slice(3)
        : digits;
    clearAuthErr("loginCodeError");
});

$("#loginCodeInput")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("#loginCodeSubmitBtn")?.click(); }
});

$("#loginCodeSubmitBtn")?.addEventListener("click", async () => {
    clearAuthErr("loginCodeError");
    const code = ($("#loginCodeInput").value || "").replace(/\D/g, "");
    if (code.length !== 6) {
        return showAuthErr("loginCodeError", "Введите 6-значный код");
    }
    if (!phoneState.token) {
        return showAuthErr("loginCodeError", "Сессия устарела, начните заново");
    }
    const btn = $("#loginCodeSubmitBtn");
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = "Входим…";
    try {
        const r = await fetch("/api/auth/phone/code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: phoneState.token, code }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.message || j.error || "Ошибка");
        $("#authModal").hidden = true;
        phoneResetUI();
        await loadMe();
        renderView();
        renderUserPill();
        showToast("Вы вошли");
    } catch (err) {
        const msg = (err && err.message) || "Ошибка";
        if (msg.includes("expired")) showAuthErr("loginCodeError", "Время истекло, начните заново");
        else if (msg.includes("wrong_code") || msg.includes("Неверный")) showAuthErr("loginCodeError", "Неверный код");
        else showAuthErr("loginCodeError", msg);
    } finally {
        btn.disabled = false;
        btn.textContent = oldText;
    }
});

function phonePollStart() {
    phoneStopPolling();
    let attempts = 0;
    phoneState.pollTimer = setInterval(async () => {
        attempts++;
        if (attempts > 180) { // 3 мин (1с)
            phoneStopPolling();
            showAuthErr("phoneError", "Время ожидания истекло. Попробуйте ещё раз.");
            authShowPane("phone");
            return;
        }
        if (!phoneState.token) { phoneStopPolling(); return; }
        try {
            const r = await fetch("/api/auth/phone/check?token=" + encodeURIComponent(phoneState.token));
            if (r.status === 410) {
                phoneStopPolling();
                showAuthErr("phoneError", "Ссылка истекла. Запросите новую.");
                authShowPane("phone");
                return;
            }
            const j = await r.json();
            if (j.ok && j.status === "ok") {
                phoneStopPolling();
                $("#authModal").hidden = true;
                phoneResetUI();
                await loadMe();
                renderView();
                renderUserPill();
                showToast(j.is_new ? "Аккаунт создан" : "Вы вошли");
            }
        } catch {}
    }, 1000);
}

// =================================================================
// USER PILL + ПОПОВЕР ПРОФИЛЯ
// =================================================================
function renderUserPill() {
    const pill = $("#userPill");
    const av = $("#userAvatar"), name = $("#userName"), sub = pill.querySelector(".user-sub");
    if (state.me && (state.me.username || state.me.display_name)) {
        const dn = state.me.display_name || state.me.username || "Гость";
        name.textContent = dn;
        // Подзаголовок теперь циклически меняется (см. _startUserSubCycle).
        _startUserSubCycle(sub);
        // Используем <img>, а не background-image — чтобы корректно проигрывались
        // анимированные аватары (GIF/APNG/WebP).
        const _av_ok = state.me.avatar && !/\bt\.me\/i\/userpic\b/.test(state.me.avatar);
        if (_av_ok) {
            av.style.backgroundImage = "";
            av.innerHTML = `<img src="${escapeHtml(state.me.avatar)}" alt="" decoding="async" onerror="this.parentElement.textContent='${(state.me.display_name||state.me.username||'?').charAt(0).toUpperCase()}'">`;
        } else {
            av.style.backgroundImage = "";
            av.textContent = (dn.charAt(0)||"?").toUpperCase();
        }
        pill.classList.remove("guest");
        pill.title = "Открыть профиль";
    } else {
        _stopUserSubCycle();
        name.textContent = "Войти / Регистрация";
        sub.textContent = "Нажмите, чтобы продолжить";
        av.style.backgroundImage = ""; av.textContent = "→";
        pill.classList.add("guest");
        pill.title = "Войти в аккаунт";
    }
}
// ===== Циклическая подпись под именем (онлайн/лайки/плейлисты/...). =====
let _userSubTimer = null;
let _userSubIdx = 0;
function _userSubVariants() {
    const me = state.me || {};
    const liked = (state.likedKeys && state.likedKeys.size) || me.likes_count || 0;
    const plCount = (state.playlists && state.playlists.length) || me.playlists_count || 0;
    const dlCount = (state.offline && state.offline.downloaded && state.offline.downloaded.size) || 0;
    const off = !navigator.onLine;
    const variants = [
        off ? "📡 Офлайн" : "🟢 В сети",
    ];
    if (me.email) variants.push(me.email);
    if (me.username && me.display_name && me.display_name !== me.username) variants.push("@" + me.username);
    if (liked > 0) variants.push(`❤ ${liked} ${_plural(liked, "трек","трека","треков")}`);
    if (plCount > 0) variants.push(`💿 ${plCount} ${_plural(plCount, "плейлист","плейлиста","плейлистов")}`);
    if (dlCount > 0) variants.push(`⬇ ${dlCount} в офлайне`);
    return variants;
}
function _plural(n, one, few, many) {
    n = Math.abs(n) % 100; const n1 = n % 10;
    if (n > 10 && n < 20) return many;
    if (n1 > 1 && n1 < 5) return few;
    if (n1 === 1) return one;
    return many;
}
function _startUserSubCycle(subEl) {
    if (!subEl) return;
    _stopUserSubCycle();
    const tick = () => {
        const variants = _userSubVariants();
        if (!variants.length) return;
        _userSubIdx = (_userSubIdx + 1) % variants.length;
        const next = variants[_userSubIdx];
        // Плавная смена через CSS-transition.
        subEl.style.transition = "opacity .25s ease";
        subEl.style.opacity = "0";
        setTimeout(() => {
            subEl.textContent = next;
            subEl.style.opacity = "1";
        }, 230);
    };
    // Сразу показываем первый вариант.
    const first = _userSubVariants()[0] || "В сети";
    subEl.textContent = first;
    subEl.style.opacity = "1";
    _userSubIdx = 0;
    _userSubTimer = setInterval(tick, 3500);
}
function _stopUserSubCycle() {
    if (_userSubTimer) { clearInterval(_userSubTimer); _userSubTimer = null; }
    _userSubIdx = 0;
}
window.addEventListener("online", () => {
    const sub = document.querySelector("#userPill .user-sub");
    if (sub && state.me) _startUserSubCycle(sub);
});
window.addEventListener("offline", () => {
    const sub = document.querySelector("#userPill .user-sub");
    if (sub && state.me) _startUserSubCycle(sub);
});
$("#userPill").onclick = () => {
    if (!state.me || !(state.me.username || state.me.display_name)) return openAuth();
    const pop = $("#profilePop");
    const av = $("#profileAvatar");
    const dn = state.me.display_name || state.me.username || "Гость";
    $("#profileName").textContent = dn;
    $("#profileSub").textContent = state.me.email || "";
    if (state.me.avatar && !/\bt\.me\/i\/userpic\b/.test(state.me.avatar)) {
        av.style.backgroundImage = "";
        const _dn0 = (state.me.display_name||state.me.username||'?').charAt(0).toUpperCase();
        av.innerHTML = `<img src="${escapeHtml(state.me.avatar)}" alt="" decoding="async" onerror="this.parentElement.innerHTML='';this.parentElement.textContent='${_dn0}'">`;
    } else {
        av.style.backgroundImage = "";
        av.innerHTML = "";
        av.textContent = (state.me.display_name||state.me.username||'?').charAt(0).toUpperCase();
    }
    pop.hidden = false;
};
$("#profileClose").onclick = () => $("#profilePop").hidden = true;
document.addEventListener("click", (e) => {
    const pop = $("#profilePop");
    if (!pop.hidden && !pop.contains(e.target) && !$("#userPill").contains(e.target)) pop.hidden = true;
});
$("#profilePop").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const act = b.dataset.act;
    $("#profilePop").hidden = true;
    if (act === "logout") {
        await api("/api/auth/logout", { method: "POST" });
        state.me = null; state.likedKeys.clear(); state.playlists = []; renderUserPill(); renderSidebarPlaylists(); navigate("home");
    } else if (act === "settings") navigate("settings");
    else if (act === "library") navigate("library");
    else if (act === "history") navigate("history");
    else if (act === "profile") navigate("profile");
});

// =================================================================
// ЛОГО + НАВИГАЦИЯ
// =================================================================
$("#logoBtn").onclick = () => navigate("home");
$$(".nav-item").forEach(el => el.onclick = () => navigate(el.dataset.view));

// Mobile tab bar (для экранов <= 900px). Прямые ссылки на главные разделы:
// Главная, Поиск, Коллекция, Офлайн, Настройки. Без бургер-меню — всё на виду.
$$(".mt-item").forEach(el => el.onclick = () => {
    const t = el.dataset.mt;
    if (t === "search") {
        navigate("home");
        setTimeout(() => $("#q")?.focus(), 50);
        return;
    }
    navigate(t);
});

// Кнопка «‹ Назад» / «› Вперёд» в топбаре — навигация по стеку.
(function setupBackBtn() {
    const b = document.getElementById("backBtn");
    if (b) b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); goBack(); });
    const f = document.getElementById("fwdBtn");
    if (f) f.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); goForward(); });
})();

// Mobile sidebar — overlay backdrop + auto-close
(function setupMobileSidebar() {
    const sidebar = $("#sidebar");
    const menuBtn = $("#menuBtn");
    if (!sidebar || !menuBtn) return;
    let backdrop = document.getElementById("sidebarBackdrop");
    if (!backdrop) {
        backdrop = document.createElement("div");
        backdrop.id = "sidebarBackdrop";
        backdrop.className = "sidebar-backdrop";
        document.body.appendChild(backdrop);
    }
    const closeSidebar = () => {
        sidebar.classList.remove("open");
        backdrop.classList.remove("visible");
        document.body.classList.remove("sidebar-open");
    };
    const openSidebar = () => {
        sidebar.classList.add("open");
        backdrop.classList.add("visible");
        document.body.classList.add("sidebar-open");
    };
    menuBtn.onclick = (e) => {
        e.stopPropagation();
        sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
    };
    backdrop.addEventListener("click", closeSidebar);
    // Любой клик внутри сайдбара (по nav-item, плейлисту, поддержке) — закрываем на мобиле.
    sidebar.addEventListener("click", (e) => {
        if (window.innerWidth >= 900) return;
        const a = e.target.closest("a, button, .sidebar-pl, .nav-item");
        if (a) closeSidebar();
    });
    // ESC закрывает.
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && sidebar.classList.contains("open")) closeSidebar();
    });
    // При увеличении окна сворачиваем оверлей-режим.
    window.addEventListener("resize", () => {
        if (window.innerWidth >= 900) closeSidebar();
    });
    // Экспортируем для других мест (navigate)
    window._closeMobileSidebar = closeSidebar;
})();

// =================================================================
// СТЕК НАВИГАЦИИ «НАЗАД» (как стрелочка ‹ в iOS)
// Каждый переход (navigate / openArtist / openAlbum) кладёт «как воссоздать
// текущий экран» в стек. Кнопка «‹» в топбаре извлекает запись и просто
// перезапускает сохранённый рендер — БЕЗ перезагрузки страницы.
// =================================================================
if (!window._velBack) window._velBack = { stack: [], forward: [], current: null, suppress: false };
function _updateBackBtn() {
    const b = document.getElementById("backBtn");
    const f = document.getElementById("fwdBtn");
    const vb = window._velBack;
    // «Назад» появляется ТОЛЬКО когда в истории что-то есть (после первого перехода).
    if (b) {
        if (vb.stack.length > 0) b.removeAttribute("hidden");
        else b.setAttribute("hidden", "");
    }
    // «Вперёд» появляется только после того, как пользователь нажал «Назад».
    if (f) {
        if (vb.forward.length > 0) f.removeAttribute("hidden");
        else f.setAttribute("hidden", "");
    }
}
function _setCurrentRender(run) {
    const vb = window._velBack;
    if (vb.current && !vb.suppress) {
        vb.stack.push(vb.current);
        if (vb.stack.length > 40) vb.stack.shift();
        // Любая «новая» навигация обнуляет forward — как в браузере.
        vb.forward = [];
    }
    vb.current = { run };
    _updateBackBtn();
}
function goBack() {
    const vb = window._velBack;
    const entry = vb.stack.pop();
    if (!entry) { _updateBackBtn(); return; }
    // Текущее состояние уезжает в forward — чтобы можно было вернуться.
    if (vb.current) {
        vb.forward.push(vb.current);
        if (vb.forward.length > 40) vb.forward.shift();
    }
    vb.suppress = true;
    vb.current = null;
    try { entry.run(); } catch (e) { try { console.warn("goBack failed", e); } catch(_){} }
    finally { vb.suppress = false; _updateBackBtn(); }
}
function goForward() {
    const vb = window._velBack;
    const entry = vb.forward.pop();
    if (!entry) { _updateBackBtn(); return; }
    // Текущее состояние возвращаем в стек back.
    if (vb.current) {
        vb.stack.push(vb.current);
        if (vb.stack.length > 40) vb.stack.shift();
    }
    vb.suppress = true;
    vb.current = null;
    try { entry.run(); } catch (e) { try { console.warn("goForward failed", e); } catch(_){} }
    finally { vb.suppress = false; _updateBackBtn(); }
}
window.goBack = goBack;
window.goForward = goForward;

function navigate(view, opts={}) {
    // Уходим с волны → отменяем отложенный автозапуск (debounce от чипов).
    if (state.currentView === "wave" && view !== "wave"
        && typeof _waveAutoTimer !== "undefined" && _waveAutoTimer) {
        clearTimeout(_waveAutoTimer); _waveAutoTimer = null;
    }
    state.currentView = view;
    if (view === "playlist") state.currentPlaylistId = opts.id;
    // Сохраняем «снимок» текущего экрана для кнопки «назад».
    _setCurrentRender(() => navigate(view, opts));
    // Если адресная строка содержит /u/ или /p/ — возвращаем «/», чтобы не путать
    // пользователя при переходе на главные вкладки.
    const cur = location.pathname;
    if (/^\/(u|p)\//.test(cur) && view !== "publicUser" && view !== "publicPlaylist") {
        history.pushState(null, "", "/");
    }
    $$(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === view));
    $$(".mt-item").forEach(el => el.classList.toggle("active", el.dataset.mt === view));
    if (window.innerWidth < 900) { $("#sidebar").classList.remove("open"); window._closeMobileSidebar && window._closeMobileSidebar(); }
    // Поисковая строка в топбаре показывается только на вкладке «Поиск».
    const tb = $("#topbar");
    if (tb) {
        if (view === "search") tb.setAttribute("data-show-search", "");
        else tb.removeAttribute("data-show-search");
    }
    renderView();
    if (view === "search") setTimeout(() => $("#q")?.focus(), 30);
}

function renderSidebarPlaylists() {
    const wrap = $("#sidebarPlaylists");
    const items = [];
    if (state.me) {
        const cnt = (state.likedKeys && state.likedKeys.size) || 0;
        // Кастомная обложка «Мне нравится» — из localStorage (как и на странице).
        let likesCover = "";
        try {
            const metaKey = "velora_likes_meta_" + (state.me?.id || state.me?.username || "u");
            const meta = JSON.parse(localStorage.getItem(metaKey) || "{}");
            likesCover = meta.cover || "";
        } catch {}
        const coverStyle = likesCover ? ` style="background-image:${_cssUrl(likesCover)};background-size:cover;background-position:center"` : "";
        const coverInner = likesCover
            ? "" // если своя обложка — прячем сердце-эмблему
            : `<svg class="ic" viewBox="0 0 24 24" fill="currentColor"><path d="M12 21s-7.5-4.6-9.5-9.3C1 8.5 3 5 6.5 5 8.6 5 10.3 6.1 12 8c1.7-1.9 3.4-3 5.5-3C21 5 23 8.5 21.5 11.7 19.5 16.4 12 21 12 21z"/></svg>`;
        items.push(`
        <div class="sidebar-pl is-likes" data-special="likes">
            <div class="sidebar-pl-cover sidebar-pl-likes-cover${likesCover?' has-custom-cover':''}"${coverStyle}>
                ${coverInner}
            </div>
            <div class="sidebar-pl-meta">
                <div class="sidebar-pl-name">Мне нравится</div>
                <div class="sidebar-pl-sub">${cnt} ${cnt==1?"трек":"треков"}</div>
            </div>
        </div>`);
    }
    items.push(...state.playlists.slice(0, 12).map(p => `
        <div class="sidebar-pl" data-pid="${p.id}">
            <div class="sidebar-pl-cover">
                ${p.cover ? `<img src="${escapeHtml(p.cover)}" alt="" loading="lazy">` : `<svg class="ic"><use href="#i-library"/></svg>`}
            </div>
            <div class="sidebar-pl-meta">
                <div class="sidebar-pl-name">${escapeHtml(p.name)}</div>
                <div class="sidebar-pl-sub">${p.count||0} ${p.count==1?"трек":"треков"}</div>
            </div>
        </div>`));
    wrap.innerHTML = items.join("");
    wrap.onclick = (e) => {
        const sp = e.target.closest("[data-special='likes']");
        if (sp) return navigate("favorites");
        const el = e.target.closest("[data-pid]"); if (!el) return;
        navigate("playlist", { id: Number(el.dataset.pid) });
    };
}

// =================================================================
// ПОИСК (debounce + cancel + race-guard)
// =================================================================
let searchTimer = null;
let searchAbort = null;
let searchSeq = 0;
$("#q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        if (state.currentView !== "search") navigate("search");
        else renderSearchResults();
    }, 500);
});
$$(".sources .chip input").forEach(cb => cb.addEventListener("change", () => {
    state.sources[cb.dataset.src] = cb.checked;
    if (state.currentView === "search") renderSearchResults();
}));

async function renderSearchResults() {
    const v = $("#view"); const q = $("#q").value.trim();
    if (!q) { renderSearchDiscover(); return; }
    // Отменяем предыдущий запрос и метим текущий поколением
    if (searchAbort) { try { searchAbort.abort(); } catch {} }
    searchAbort = new AbortController();
    const mySeq = ++searchSeq;
    const sig = searchAbort.signal;
    v.innerHTML = `<div class="hint">Ищем «${escapeHtml(q)}»…</div>`;
    try {
        const sources = activeSources();
        const [tracksData, artistsData] = await Promise.all([
            fetch(`/api/search/tracks?q=${encodeURIComponent(q)}&limit=40&sources=${sources}`, { signal: sig }).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/search/artists?q=${encodeURIComponent(q)}&limit=12&sources=${sources}`, { signal: sig }).then(r=>r.ok?r.json():[]).catch(()=>[])
        ]);
        // Если за это время начался новый поиск — игнорируем устаревший ответ
        if (mySeq !== searchSeq) return;
        const tracks = asTracks(tracksData);
        const artists = asArr(artistsData, "artists");
        if (!tracks.length && !artists.length) { v.innerHTML = `<div class="hint">Ничего не найдено.</div>`; return; }
        let html = "";
        if (artists.length) html += `<h2 class="section-title first">Исполнители</h2>
            <div class="cards artists-cards" id="srArtists">${artists.map(a => {
                const img = a.image || a.picture_xl || a.picture_big || a.picture_medium || a.picture || a.picture_small || "";
                return `
                <div class="card artist" data-aid="${escapeHtml(a.id)}" data-source="${a.source||"deezer"}">
                    <div class="card-cover artist-avatar ${img?'':'placeholder'}">
                        ${img ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" decoding="async">` : `<svg class="ic"><use href="#i-mic"/></svg>`}
                    </div>
                    <div class="c-title">${escapeHtml(a.name)}</div>
                    <div class="c-sub">${fmtNum(a.fans)} слушателей</div>
                </div>`;
            }).join("")}</div>`;
        if (tracks.length) html += `<h2 class="section-title${artists.length?'':' first'}">Треки</h2><div class="track-list" id="srTracks">${tracks.map(trackRowHtml).join("")}</div>`;
        v.innerHTML = html;
        if (tracks.length) bindTrackList($("#srTracks"), tracks);
        if (artists.length) {
            $("#srArtists").onclick = (e) => {
                const c = e.target.closest("[data-aid]"); if (!c) return;
                openArtist(c.dataset.source, c.dataset.aid, c.querySelector(".c-title").textContent);
            };
        }
    } catch (e) {
        if (e.name === "AbortError") return;
        v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
    }
}
function activeSources() {
    return Object.entries(state.sources).filter(([_,v])=>v).map(([k])=>k).join(",");
}

// =================================================================
// Дефолтная страница ПОИСКА (когда поле q пустое): персональные
// авто-плейлисты + рекомендуемые артисты + треки.
// =================================================================
const _autoPlaylistCache = { at: 0, data: null };
async function renderSearchDiscover() {
    const v = $("#view");
    // Кэш на 5 минут — чтобы при возврате на страницу поиска не дергать API заново.
    const fresh = _autoPlaylistCache.data && (Date.now() - _autoPlaylistCache.at < 5 * 60 * 1000);
    if (!fresh) {
        v.innerHTML = `
            <div class="discover-skeleton">
                <div class="dsk-row"></div><div class="dsk-row"></div><div class="dsk-row"></div>
            </div>`;
    } else {
        _renderDiscover(_autoPlaylistCache.data);
        return;
    }
    let data;
    try {
        const r = await fetch(`/api/discover/feed?playlists=10`);
        data = await r.json();
    } catch (e) {
        v.innerHTML = `<div class="hint">Не удалось загрузить подборки. Попробуйте позже.</div>`;
        return;
    }
    _autoPlaylistCache.at = Date.now();
    _autoPlaylistCache.data = data;
    state._autoPlaylists = (data.playlists || []).reduce((acc, p) => { acc[p.id] = p; return acc; }, {});
    _renderDiscover(data);
}

function _renderDiscover(data) {
    const v = $("#view");
    const playlists = data.playlists || [];
    const artists = data.artists || [];
    const tracks = data.tracks || [];
    let html = "";
    const heading = data.guest
        ? "Подборки для вас"
        : "Ваши персональные плейлисты";
    if (playlists.length) {
        html += `<h2 class="section-title first">${escapeHtml(heading)}</h2>
            <div class="cards auto-pl-cards" id="srAutoPl">${playlists.map(p => {
                const cov = p.cover || (p.tracks && p.tracks[0] && (p.tracks[0].cover_big || p.tracks[0].cover_small)) || "";
                return `
                <div class="card auto-pl" data-apid="${escapeHtml(p.id)}" title="${escapeHtml(p.name)}">
                    <div class="card-cover ${cov?'':'placeholder'}">
                        ${cov ? `<img src="${escapeHtml(cov)}" alt="" loading="lazy" decoding="async">` : `<svg class="ic"><use href="#i-list"/></svg>`}
                        <button class="auto-pl-play" data-play-apid="${escapeHtml(p.id)}" title="Играть">
                            <svg class="ic"><use href="#i-play"/></svg>
                        </button>
                    </div>
                    <div class="c-title">${escapeHtml(p.name)}</div>
                    <div class="c-sub">${escapeHtml(p.subtitle || (p.tracks.length + " треков"))}</div>
                </div>`;
            }).join("")}</div>`;
    }
    if (artists.length) {
        html += `<h2 class="section-title">Артисты на которых стоит обратить внимание</h2>
            <div class="cards artists-cards" id="srDscArtists">${artists.map(a => {
                const img = a.image || a.picture || "";
                return `
                <div class="card artist" data-aid="${escapeHtml(a.id)}" data-source="${a.source || 'deezer'}">
                    <div class="card-cover artist-avatar ${img?'':'placeholder'}">
                        ${img ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" decoding="async">` : `<svg class="ic"><use href="#i-mic"/></svg>`}
                    </div>
                    <div class="c-title">${escapeHtml(a.name)}</div>
                </div>`;
            }).join("")}</div>`;
    }
    if (tracks.length) {
        html += `<h2 class="section-title">Треки, которые вам понравятся</h2>
            <div class="track-list" id="srDscTracks">${tracks.map(trackRowHtml).join("")}</div>`;
    }
    if (!html) {
        v.innerHTML = `<div class="hint">Начните вводить, чтобы искать.</div>`;
        return;
    }
    v.innerHTML = html;
    // Карточки авто-плейлистов: клик по карточке — открыть «виртуальный» плейлист,
    // клик по play — мгновенно играть очередь.
    const apl = $("#srAutoPl");
    if (apl) {
        apl.onclick = (e) => {
            const playBtn = e.target.closest("[data-play-apid]");
            if (playBtn) {
                e.stopPropagation();
                const pid = playBtn.dataset.playApid;
                const p = state._autoPlaylists && state._autoPlaylists[pid];
                if (p && p.tracks.length) {
                    playQueue(p.tracks.map(normTrack), 0, { from_view: "search" });
                }
                return;
            }
            const card = e.target.closest("[data-apid]"); if (!card) return;
            const pid = card.dataset.apid;
            openAutoPlaylist(pid);
        };
    }
    const da = $("#srDscArtists");
    if (da) {
        da.onclick = (e) => {
            const c = e.target.closest("[data-aid]"); if (!c) return;
            openArtist(c.dataset.source, c.dataset.aid, c.querySelector(".c-title").textContent);
        };
    }
    const dt = $("#srDscTracks");
    if (dt) bindTrackList(dt, tracks.map(normTrack));
}

// Открыть виртуальный авто-плейлист на полной странице (без обращения к /api/playlists).
function openAutoPlaylist(pid) {
    const p = state._autoPlaylists && state._autoPlaylists[pid];
    if (!p) return;
    const v = $("#view");
    state.currentView = "auto-playlist";
    const tracks = (p.tracks || []).map(normTrack);
    const cov = p.cover || (tracks[0] && (tracks[0].cover_big || tracks[0].cover_small)) || "";
    v.innerHTML = `
        <div class="pl-hero">
            <div class="pl-hero-cover ${cov?'':'placeholder'}">
                ${cov ? `<img src="${escapeHtml(cov)}" alt="">` : `<svg class="ic"><use href="#i-list"/></svg>`}
            </div>
            <div class="pl-hero-info">
                <div class="pl-hero-kind">Подборка</div>
                <h1 class="pl-hero-title">${escapeHtml(p.name)}</h1>
                <div class="pl-hero-sub">${escapeHtml(p.subtitle || '')} · ${tracks.length} треков</div>
                <div class="pl-hero-actions">
                    <button class="btn primary" id="autoPlPlay"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
                    <button class="btn" id="autoPlBack">Назад</button>
                </div>
            </div>
        </div>
        <h2 class="section-title">Треклист</h2>
        <div class="track-list" id="autoPlTracks">${tracks.map(trackRowHtml).join("")}</div>
    `;
    bindTrackList($("#autoPlTracks"), tracks);
    $("#autoPlPlay").onclick = () => playQueue(tracks, 0, { from_view: "search" });
    $("#autoPlBack").onclick = () => { state.currentView = "search"; renderSearchDiscover(); };
}

function renderTrackResults(tracks) {
    const v = $("#view");
    if (!tracks.length) { v.innerHTML = `<div class="hint">Ничего не найдено.</div>`; return; }
    v.innerHTML = `<h2 class="section-title first">Треки</h2><div class="track-list">${tracks.map(trackRowHtml).join("")}</div>`;
    bindTrackList(v, tracks);
}
function renderArtistResults(artists) {
    const v = $("#view");
    if (!artists.length) { v.innerHTML = `<div class="hint">Артистов не найдено.</div>`; return; }
    v.innerHTML = `<h2 class="section-title first">Артисты</h2>
        <div class="cards artists-cards">${artists.map(a => {
            const img = a.image || a.picture_xl || a.picture_big || a.picture_medium || a.picture || "";
            return `
            <div class="card artist" data-aid="${a.id}" data-source="${a.source||"deezer"}">
                <div class="card-cover artist-avatar ${img?'':'placeholder'}">
                    ${img ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" decoding="async">` : `<svg class="ic"><use href="#i-mic"/></svg>`}
                </div>
                <div class="c-title">${escapeHtml(a.name)}</div>
                <div class="c-sub">${fmtNum(a.fans)} слушателей</div>
            </div>`;
        }).join("")}</div>`;
    v.onclick = (e) => {
        const c = e.target.closest("[data-aid]"); if (!c) return;
        openArtist(c.dataset.source, c.dataset.aid, c.querySelector(".c-title").textContent);
    };
}

function trackRowHtml(t, idx) {
    const k = (t.source||"")+"|"+(t.source_id||"");
    const liked = state.likedKeys.has(k);
    const disliked = state.dislikedTrackKeys && state.dislikedTrackKeys.has(k);
    const downloaded = state.offline.downloaded.has(k);
    const playing = state.track && state.track.source === t.source && state.track.source_id === t.source_id;
    const exp = t.explicit ? `<span class="explicit-badge" title="Explicit">E</span>` : "";
    // Все артисты трека (включая featured) → каждое имя кликабельно отдельно.
    // Если массива нет, но в имени есть «, » — делаем chips без id (id дотянем
    // через /api/track при клике).
    let artList;
    if (Array.isArray(t.artists) && t.artists.length) {
        artList = t.artists;
    } else if (t.artist && t.artist.includes(",")) {
        artList = t.artist.split(/\s*,\s*/).filter(Boolean).map((nm, i) => ({
            id: i === 0 ? (t.artist_id || "") : "",
            name: nm,
        }));
    } else {
        artList = t.artist ? [{ id: t.artist_id || "", name: t.artist }] : [];
    }
    const artHtml = artList.length
        ? artList.map(a => {
            const aid = a.id ? ` data-aid="${escapeHtml(a.id)}"` : "";
            const nm = ` data-aname="${escapeHtml(a.name)}"`;
            return `<a class="t-sub-link" data-act="goto-artist"${aid}${nm} data-source="${escapeHtml(t.source||"")}">${escapeHtml(a.name)}</a>`;
          }).join(", ")
        : "";
    return `<div class="track-row ${playing?"playing":""}${downloaded?" is-dl":""}" data-idx="${idx??""}" data-key="${k}">
        <div class="t-cover">
            <img src="${t.album_cover||""}" alt="" loading="lazy">
            ${downloaded ? '<span class="dl-badge" title="Скачано"><svg class="ic"><use href="#i-check"/></svg></span>' : ''}
        </div>
        <div class="t-meta">
            <div class="t-title"><span class="name">${escapeHtml(t.title)}</span>${exp}</div>
            <div class="t-sub">${artHtml}</div>
        </div>
        <span class="t-source">${t.source||""}</span>
        <button class="row-icon-btn ${liked?'is-liked':''}" data-act="like" title="${liked?'Удалить из любимого':'В любимое'}">
            <svg class="ic"><use href="#i-${liked?'heart-fill':'heart'}"/></svg>
        </button>
        <button class="row-icon-btn ${disliked?'is-disliked':''}" data-act="dislike" title="Не нравится">
            <svg class="ic"><use href="#i-dislike"/></svg>
        </button>
        <button class="row-icon-btn ${downloaded?'is-downloaded':''}" data-act="download" title="${downloaded?'Удалить из загрузок':'Скачать в офлайн'}">
            <svg class="ic"><use href="#i-${downloaded?'check':'download'}"/></svg>
        </button>
        <button class="row-icon-btn" data-act="more" title="Добавить в плейлист…">
            <svg class="ic"><use href="#i-dots"/></svg>
        </button>
        <span class="t-dur">${fmtTime(t.duration)}</span>
    </div>`;
}

function bindTrackList(container, tracks) {
    // Сохраняем актуальный массив прямо на контейнере — чтобы старые замыкания
    // (если контейнер каким-то образом переиспользуется) не запускали стейл-треки.
    container._velTracks = tracks;
    container.addEventListener("click", async (e) => {
        const goArt = e.target.closest("[data-act=goto-artist]");
        if (goArt) {
            e.stopPropagation();
            const src = goArt.dataset.source || "deezer";
            const aid = goArt.dataset.aid || "";
            const aname = goArt.dataset.aname || goArt.textContent.trim();
            if (aid) { openArtist(src, aid, aname); return; }
            // ID нет — попробуем дотянуть через /api/track/<src_id>.
            const row = goArt.closest(".track-row");
            const idx = row ? Number(row.dataset.idx) : -1;
            const tracksArr = container._velTracks || tracks;
            const t = (idx >= 0 && tracksArr) ? tracksArr[idx] : null;
            if (t && t.source && t.source_id) {
                try {
                    const full = await api(`/api/track/${encodeURIComponent(t.source)}/${encodeURIComponent(t.source_id)}`, { silent: true, silent404: true });
                    const list = (full && full.artists) || [];
                    // Сохраняем результат в массив треков.
                    if (list.length) t.artists = list;
                    const found = list.find(a => a && a.name && a.name.toLowerCase() === aname.toLowerCase());
                    if (found && found.id) { openArtist(src, String(found.id), found.name); return; }
                } catch(_) {}
            }
            // Совсем нет id — фолбэк на поиск по имени.
            $("#q").value = aname; navigate("search");
            return;
        }
        const row = e.target.closest(".track-row"); if (!row) return;
        // Берём текущий массив с контейнера (а не из закрытия) — спасает от race с ре-рендером.
        tracks = container._velTracks || tracks;
        if (e.target.closest("[data-act=like]")) {
            e.stopPropagation();
            const idx = Number(row.dataset.idx);
            await toggleLike(tracks[idx]);
            return;
        }
        if (e.target.closest("[data-act=dislike]")) {
            e.stopPropagation();
            const idx = Number(row.dataset.idx);
            await toggleDislike(tracks[idx]);
            return;
        }
        if (e.target.closest("[data-act=download]")) {
            e.stopPropagation();
            const idx = Number(row.dataset.idx);
            await toggleDownload(tracks[idx], row);
            return;
        }
        if (e.target.closest("[data-act=more]")) {
            e.stopPropagation();
            const idx = Number(row.dataset.idx);
            openTrackMenu(tracks[idx], e.target.closest("[data-act=more]"));
            return;
        }
        const idx = Number(row.dataset.idx);
        playQueue(tracks, idx);
    });
}

// =================================================================
// АРТИСТ
// =================================================================
async function openArtist(source, id, name) {
    _setCurrentRender(() => openArtist(source, id, name));
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем «${escapeHtml(name||"артиста")}»…</div>`;
    try {
        const data = await api(`/api/artist/${id}`);
        const a = data || {};
        // Записываем посещение страницы артиста — нужно для снимка предпочтений.
        _recordVisit({ kind: "artist", id, source: source||"deezer",
                       name: a.name || name || "", cover: a.image || a.picture_big || "" });
        const top = asTracks(data && data.top_tracks);
        const albums = (data && data.albums) || [];
        // Собираем все доступные размеры/варианты аватарок: Deezer отдаёт picture_small/medium/big/xl,
        // Apple — artworkUrl (с возможностью подменить размер). Дополнительно — обложки альбомов
        // как «настроение» артиста.
        const avatarSet = new Set();
        const pushAv = (u) => { if (u && typeof u === "string") avatarSet.add(u); };
        pushAv(a.image); pushAv(a.picture_xl); pushAv(a.picture_big); pushAv(a.picture_medium); pushAv(a.picture); pushAv(a.picture_small);
        if (Array.isArray(a.images)) a.images.forEach(pushAv);
        // Apple: подменяем 100x100 → 1000x1000 для большой версии.
        if (a.image && /\/\d+x\d+(bb)?\./.test(a.image)) {
            pushAv(a.image.replace(/\/\d+x\d+(bb)?\./, "/1000x1000$1."));
            pushAv(a.image.replace(/\/\d+x\d+(bb)?\./, "/600x600$1."));
        }
        const avatars = Array.from(avatarSet);
        const heroImg = avatars[0] || "";
        const albumThumbs = albums.slice(0, 8).map(al => al.cover).filter(Boolean);
        v.innerHTML = `
            <div class="artist-hero ${heroImg?'has-img':''}" id="artistHero">
                ${heroImg ? `<button class="artist-hero-cover" data-av="0" title="Открыть в полном размере">
                    <img src="${escapeHtml(heroImg)}" alt="" loading="eager">
                </button>` : `<div class="artist-hero-cover placeholder"><svg class="ic"><use href="#i-mic"/></svg></div>`}
                <div class="artist-hero-meta">
                    <div class="artist-hero-eyebrow">Исполнитель</div>
                    <h1>${escapeHtml(a.name||name||"")}</h1>
                    <div class="stats">${fmtNum(a.fans)} слушателей${top.length?` · ${top.length} популярных`:""}${albums.length?` · ${albums.length} релизов`:""}</div>
                    <div class="artist-actions">
                        <button class="btn-primary" id="playArtist"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
                        <button class="btn-secondary" id="shuffleArtist"><svg class="ic"><use href="#i-shuffle"/></svg> Вперемешку</button>
                        ${_artistPrefBtnsHtml(source||"deezer", id)}
                    </div>
                </div>
            </div>
            ${avatars.length > 1 || albumThumbs.length ? `
            <div class="artist-gallery">
                <h2 class="section-title-sm">Аватары и обложки</h2>
                <div class="artist-gallery-row">
                    ${avatars.map((u,i) => `<button class="artist-gal-item" data-av="${i}"><img src="${escapeHtml(u)}" alt="" loading="lazy"></button>`).join("")}
                    ${albumThumbs.map((u,i) => `<button class="artist-gal-item album" data-album-cover="${escapeHtml(u)}"><img src="${escapeHtml(u)}" alt="" loading="lazy"></button>`).join("")}
                </div>
            </div>` : ""}
            <div class="section-head">
                <h2 class="section-title">Популярные треки</h2>
                ${top.length > 5 ? `<button class="btn-link" id="toggleTopAll" data-expanded="0">Все треки артиста (${top.length}) →</button>` : ""}
            </div>
            <div class="track-list" id="atracks"></div>
            ${albums.length ? `
            <div class="section-head">
                <h2 class="section-title">Альбомы и релизы</h2>
                <div class="sort-group" id="albumSort" role="tablist" aria-label="Сортировка">
                    <button class="chip-btn is-active" data-sort="popular" title="По популярности (число фанатов)">Популярные</button>
                    <button class="chip-btn" data-sort="year_desc" title="Сначала новые">Новые</button>
                    <button class="chip-btn" data-sort="year_asc" title="Сначала старые">Старые</button>
                    <button class="chip-btn" data-sort="title" title="По названию">A–Я</button>
                </div>
            </div>
            <div class="cards" id="albumsGrid"></div>` : ""}`;
        // Render initial top tracks (5 visible by default)
        let topExpanded = false;
        const aTracksEl = $("#atracks");
        const renderTopTracks = () => {
            const slice = topExpanded ? top : top.slice(0, 5);
            aTracksEl.innerHTML = slice.map(trackRowHtml).join("");
            bindTrackList(aTracksEl, slice);
        };
        renderTopTracks();
        const toggleBtn = $("#toggleTopAll");
        if (toggleBtn) {
            toggleBtn.onclick = () => {
                topExpanded = !topExpanded;
                toggleBtn.dataset.expanded = topExpanded ? "1" : "0";
                toggleBtn.textContent = topExpanded ? "Свернуть ↑" : `Все треки артиста (${top.length}) →`;
                renderTopTracks();
                if (topExpanded) toggleBtn.scrollIntoView({ behavior: "smooth", block: "nearest" });
            };
        }
        // Albums: sortable grid
        const albumsGrid = $("#albumsGrid");
        const albumCardHtml = (al) => {
            const dateStr = al.release_date ? formatReleaseDate(al.release_date) : (al.year || "");
            const sub = [
                dateStr,
                al.nb_tracks ? `${al.nb_tracks} тр.` : "",
                al.record_type && al.record_type !== "album" ? al.record_type.toUpperCase() : ""
            ].filter(Boolean).join(" · ");
            return `<div class="card album-card" data-album-id="${escapeHtml(al.id)}">
                <div class="card-cover ${al.cover?'':'placeholder'}">
                    ${al.cover ? `<img src="${escapeHtml(al.cover)}" alt="" loading="lazy">` : `<svg class="ic"><use href="#i-library"/></svg>`}
                    ${al.explicit_lyrics ? `<span class="explicit-badge card-explicit">E</span>` : ""}
                </div>
                <div class="c-title">${escapeHtml(al.title)}</div>
                <div class="c-sub">${escapeHtml(sub)}</div>
            </div>`;
        };
        let currentSort = "popular";
        const sortAlbums = (arr, mode) => {
            const a2 = arr.slice();
            if (mode === "popular") a2.sort((x,y) => (y.fans||0) - (x.fans||0));
            else if (mode === "year_desc") a2.sort((x,y) => (y.release_date||"").localeCompare(x.release_date||""));
            else if (mode === "year_asc") a2.sort((x,y) => (x.release_date||"").localeCompare(y.release_date||""));
            else if (mode === "title") a2.sort((x,y) => (x.title||"").localeCompare(y.title||"", "ru"));
            return a2;
        };
        const renderAlbums = () => {
            if (!albumsGrid) return;
            albumsGrid.innerHTML = sortAlbums(albums, currentSort).map(albumCardHtml).join("");
        };
        renderAlbums();
        if (albumsGrid) {
            albumsGrid.onclick = (e) => {
                const card = e.target.closest("[data-album-id]"); if (!card) return;
                openAlbum(card.dataset.albumId);
            };
        }
        const sortGroup = $("#albumSort");
        if (sortGroup) {
            sortGroup.onclick = (e) => {
                const b = e.target.closest("[data-sort]"); if (!b) return;
                currentSort = b.dataset.sort;
                sortGroup.querySelectorAll(".chip-btn").forEach(x => x.classList.toggle("is-active", x === b));
                renderAlbums();
            };
        }
        $("#playArtist")?.addEventListener("click", () => top.length && playQueue(top, 0, { from_view: "artist" }));
        $("#shuffleArtist")?.addEventListener("click", () => {
            if (!top.length) return;
            const shuffled = top.slice().sort(() => Math.random() - 0.5);
            playQueue(shuffled, 0, { from_view: "artist" });
        });
        // Лайтбокс аватарок: клик по hero-cover или по элементу галереи.
        const openLB = (urls, idx) => openAvatarLightbox(urls, idx);
        v.querySelectorAll("[data-av]").forEach(el => el.onclick = () => {
            openLB(avatars, Number(el.dataset.av) || 0);
        });
        v.querySelectorAll("[data-album-cover]").forEach(el => el.onclick = () => {
            openLB([el.dataset.albumCover], 0);
        });
    } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}

// Форматирование релизной даты YYYY-MM-DD → "12 марта 2023"
function formatReleaseDate(s) {
    if (!s || typeof s !== "string") return "";
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return s.slice(0, 4);
    const months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
    const d = parseInt(m[3], 10), mo = parseInt(m[2], 10), y = m[1];
    if (!d || !mo) return y;
    return `${d} ${months[mo-1]} ${y}`;
}

// =================================================================
// АЛЬБОМ
// =================================================================
async function openAlbum(albumId) {
    _setCurrentRender(() => openAlbum(albumId));
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем альбом…</div>`;
    try {
        const data = await api(`/api/album/${albumId}?meta=1`);
        // Учитываем визит альбома в снимке предпочтений.
        _recordVisit({ kind: "album", id: albumId, source: data.source || "deezer",
                       name: data.title || "", artist: data.artist || "",
                       cover: data.cover || "" });
        const tracks = asTracks(data.tracks).map((t, i) => ({ ...t, track_no: i + 1 }));
        const dateStr = data.release_date ? formatReleaseDate(data.release_date) : (data.year || "");
        const totalSec = tracks.reduce((s, t) => s + (t.duration || 0), 0);
        const totalMin = Math.round(totalSec / 60);
        const subBits = [
            data.record_type ? data.record_type.toUpperCase() : "",
            dateStr,
            `${data.nb_tracks || tracks.length} треков`,
            totalMin ? `${totalMin} мин` : "",
            data.label ? data.label : "",
        ].filter(Boolean);
        v.innerHTML = `
            <div class="artist-hero album-hero ${data.cover?'has-img':''}">
                ${data.cover ? `<button class="artist-hero-cover" id="albumCoverBtn" title="Открыть в полном размере">
                    <img src="${escapeHtml(data.cover)}" alt="" loading="eager">
                </button>` : `<div class="artist-hero-cover placeholder"><svg class="ic"><use href="#i-library"/></svg></div>`}
                <div class="artist-hero-meta">
                    <div class="artist-hero-eyebrow">${escapeHtml((data.record_type||"album").toUpperCase())}${data.explicit_lyrics?' · 18+':''}</div>
                    <h1>${escapeHtml(data.title)}</h1>
                    ${data.artist ? `<div class="album-artist"><a class="t-sub-link" data-act="goto-artist" data-aid="${escapeHtml(data.artist_id||"")}" data-source="deezer">${escapeHtml(data.artist)}</a></div>` : ""}
                    <div class="stats">${subBits.map(escapeHtml).join(" · ")}</div>
                    ${(data.genres||[]).length ? `<div class="album-genres">${data.genres.map(g => `<span class="chip-static">${escapeHtml(g)}</span>`).join("")}</div>` : ""}
                    <div class="artist-actions">
                        <button class="btn-primary" id="playAlbum"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
                        <button class="btn-secondary" id="shuffleAlbum"><svg class="ic"><use href="#i-shuffle"/></svg> Вперемешку</button>
                    </div>
                </div>
            </div>
            <h2 class="section-title">Треклист</h2>
            <div class="track-list" id="albumTracks">${tracks.map(trackRowHtml).join("")}</div>
        `;
        bindTrackList($("#albumTracks"), tracks);
        $("#playAlbum")?.addEventListener("click", () => tracks.length && playQueue(tracks, 0, { from_view: "album" }));
        $("#shuffleAlbum")?.addEventListener("click", () => {
            if (!tracks.length) return;
            const sh = tracks.slice().sort(() => Math.random() - 0.5);
            playQueue(sh, 0, { from_view: "album" });
        });
        $("#albumCoverBtn")?.addEventListener("click", () => openAvatarLightbox([data.cover], 0));
        // ссылки на артиста внутри hero
        v.querySelectorAll("[data-act=goto-artist]").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                openArtist(el.dataset.source||"deezer", el.dataset.aid, el.textContent.trim());
            });
        });
    } catch (e) {
        v.innerHTML = `<div class="error">Не удалось загрузить альбом: ${escapeHtml(e.message||"")}</div>`;
    }
}

// Лайтбокс для просмотра аватарок артиста / обложек.
function openAvatarLightbox(urls, idx=0) {
    if (!urls || !urls.length) return;
    const existing = document.getElementById("avatarLightbox");
    if (existing) existing.remove();
    const lb = document.createElement("div");
    lb.id = "avatarLightbox";
    lb.className = "avatar-lightbox";
    lb.innerHTML = `
        <button class="alb-close" title="Закрыть"><svg class="ic"><use href="#i-x"/></svg></button>
        ${urls.length > 1 ? `<button class="alb-nav alb-prev"><svg class="ic"><use href="#i-chev-left"/></svg></button>` : ""}
        ${urls.length > 1 ? `<button class="alb-nav alb-next"><svg class="ic"><use href="#i-chev-right"/></svg></button>` : ""}
        <div class="alb-stage"><img id="albImg" src="${escapeHtml(urls[idx])}" alt=""></div>
        ${urls.length > 1 ? `<div class="alb-counter"><span id="albIdx">${idx+1}</span> / ${urls.length}</div>` : ""}
    `;
    document.body.appendChild(lb);
    let cur = idx;
    const go = (delta) => {
        cur = (cur + delta + urls.length) % urls.length;
        const img = document.getElementById("albImg");
        if (img) img.src = urls[cur];
        const ctr = document.getElementById("albIdx");
        if (ctr) ctr.textContent = String(cur + 1);
    };
    lb.querySelector(".alb-close").onclick = () => lb.remove();
    lb.querySelector(".alb-prev")?.addEventListener("click", () => go(-1));
    lb.querySelector(".alb-next")?.addEventListener("click", () => go(1));
    lb.addEventListener("click", (e) => { if (e.target === lb) lb.remove(); });
    const onKey = (e) => {
        if (!document.body.contains(lb)) { document.removeEventListener("keydown", onKey); return; }
        if (e.key === "Escape") lb.remove();
        else if (e.key === "ArrowLeft") go(-1);
        else if (e.key === "ArrowRight") go(1);
    };
    document.addEventListener("keydown", onKey);
}

// =================================================================
// ВИДЫ
// =================================================================
function renderView() {
    const v = $("#view");
    v.scrollTop = 0;
    if (state.currentView === "search") return renderSearchResults();
    if (state.currentView === "home") return renderHome();
    if (state.currentView === "wave") return renderWave();
    if (state.currentView === "charts") return renderCharts();
    if (state.currentView === "library") return renderLibrary();
    if (state.currentView === "history") return renderHistory();
    if (state.currentView === "settings") return renderSettings();
    if (state.currentView === "profile") return renderProfilePage();
    if (state.currentView === "favorites") return renderFavoritesPage();
    if (state.currentView === "playlist") return renderPlaylistPage(state.currentPlaylistId);
    if (state.currentView === "dislikes") return renderDislikes();
    if (state.currentView === "offline") return renderOffline();
    if (state.currentView === "follows") return renderFollowsPage();
    if (state.currentView === "artistPrefs") return renderArtistPrefs();
    if (state.currentView === "publicUser") return renderPublicUser();
    // Если что-то ссылается на старую вкладку "admin" — редиректим на главную.
    if (state.currentView === "admin") { state.currentView = "home"; return renderHome(); }
}

// ============== ГЛАВНАЯ ==============
async function renderHome() {
    const v = $("#view");
    v.innerHTML = `
        <div class="home-grid">
            <div class="wave-hero">
                <div class="wave-blob"></div>
                <div class="wave-inner">
                    <button class="wave-play" id="homeWavePlay">
                        <svg class="ic wave-play-ic" style="width:28px;height:28px"><use href="#i-play"/></svg>
                        Моя волна
                    </button>
                    <button class="wave-sub" data-go="wave">
                        <svg class="ic"><use href="#i-settings"/></svg>
                        Настроить
                    </button>
                </div>
            </div>
            <div class="shortcuts">
                <div class="shortcut" data-go="library">
                    <div class="shortcut-cover"><svg class="ic"><use href="#i-heart-fill"/></svg></div>
                    <div><div class="name">Мне нравится</div><div class="sub">${state.likedKeys.size} ${state.likedKeys.size==1?'трек':'треков'}</div></div>
                </div>
                <div class="shortcut" data-go="history">
                    <div class="shortcut-cover" style="background:linear-gradient(135deg,#4a8eff,#b46bff)"><svg class="ic"><use href="#i-clock"/></svg></div>
                    <div><div class="name">История прослушиваний</div><div class="sub">Что вы слушали</div></div>
                </div>
            </div>
            <h2 class="section-title" id="homeRecsTitle">${state.me ? "Возможно вам будет интересно" : "Чарты"}</h2>
            <div id="homeCharts"><div class="hint">Загружаем…</div></div>
        </div>`;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) return navigate(g.dataset.go);
        if (e.target.closest("#homeWavePlay")) return toggleWave();
    };
    try {
        // Для авторизованных — персональная подборка (та же логика, что у Моей волны:
        // топ-артисты по лайкам + связанные, минус дизлайки). Гостям — обычные чарты.
        const endpoint = state.me ? "/api/wave?limit=20" : "/api/charts?limit=20";
        const data = await api(endpoint).catch(() => api("/api/charts?limit=20"));
        const tracks = asTracks(data).slice(0, 20);
        const host = $("#homeCharts");
        if (!host) return;
        if (!tracks.length) { host.innerHTML = `<div class="hint">Подборка временно недоступна.</div>`; return; }
        host.innerHTML = `<div class="track-list">${tracks.map(trackRowHtml).join("")}</div>`;
        bindTrackList(host, tracks);
    } catch {
        const host = $("#homeCharts");
        if (host) host.innerHTML = `<div class="hint">Подборка временно недоступна.</div>`;
    }
}

// ============== МОЯ ВОЛНА ==============
// Дебаунс автозапуска при выборе чипов: чтобы при быстром клике не дёргать /api/wave подряд.
let _waveAutoTimer = null;
function _waveQS() {
    const params = new URLSearchParams();
    const tune = state.waveTune || {};
    for (const k of ["occupy", "char", "mood", "lang"]) {
        if (tune[k]) params.set(k, tune[k]);
    }
    return params.toString();
}
// Live-обновление списка «Возможно, вам понравится» БЕЗ перезагрузки страницы и БЕЗ старта плеера.
async function _refreshWaveList() {
    const host = document.getElementById("waveTracks");
    if (!host) return;
    const hint = host.querySelector(".tune-hint, .hint");
    host.classList.add("is-loading");
    try {
        const qs = _waveQS();
        const endpoint = state.me ? ("/api/wave?fresh=1&limit=30" + (qs ? "&" + qs : "")) : "/api/charts?limit=30";
        const data = await api(endpoint, { silent: true }).catch(() => api("/api/charts?limit=30"));
        const tracks = asTracks(data).slice(0, 30);
        host.innerHTML = `<div class="track-list">${tracks.map(trackRowHtml).join("")}</div>`;
        bindTrackList(host, tracks);
        // Если волна СЕЙЧАС играет — обновим лишь «следующие» треки в очереди,
        // не сбивая текущий проигрываемый. Текущий трек намеренно НЕ заменяем,
        // чтобы переключение чипов не дёргало плеер.
        if (state.queueOrigin === "wave" && Array.isArray(state.queue) && state.track) {
            const cur = state.queue[state.queueIdx];
            const tail = tracks.filter(t => String(t.id) !== String(state.track.id));
            state.queue = [cur, ...tail];
            state.queueIdx = 0;
        }
    } finally {
        host.classList.remove("is-loading");
    }
}
function _scheduleWaveAutoStart() {
    if (_waveAutoTimer) clearTimeout(_waveAutoTimer);
    _waveAutoTimer = setTimeout(() => {
        _waveAutoTimer = null;
        vlog("wave live-refresh", state.waveTune);
        _refreshWaveList();
    }, 350);
}
async function renderWave() {
    const v = $("#view");
    const opts = TUNE_OPTIONS;
    const tune = state.waveTune || (state.waveTune = { occupy: null, char: null, mood: null, lang: null });
    const isActive = (group, val) => tune[group] === val ? "active" : "";
    const collapsedAttr = state.waveCollapsed ? "is-collapsed" : "";
    v.innerHTML = `
        <div class="home-grid with-tune ${collapsedAttr}" id="waveGrid">
            <div>
                <div class="wave-hero">
                    <div class="wave-blob"></div>
                    <div class="wave-inner">
                        <button class="wave-play" id="waveBigPlay">
                            <svg class="ic wave-play-ic" style="width:28px;height:28px"><use href="#i-play"/></svg>
                            Моя волна
                        </button>
                        <button class="wave-tune-toggle" id="waveTuneToggle" title="${state.waveCollapsed?'Показать настройки':'Скрыть настройки'}">
                            <svg class="ic"><use href="#i-${state.waveCollapsed?'settings':'chev-right'}"/></svg>
                            <span class="wave-tune-toggle-label">${state.waveCollapsed ? "Настройки волны" : "Скрыть настройки"}</span>
                        </button>
                    </div>
                </div>
                <h2 class="section-title">Возможно, вам понравится</h2>
                <div id="waveTracks"><div class="hint">Подбираем треки…</div></div>
            </div>
            <div class="tune-panel" id="waveTunePanel" ${state.waveCollapsed?"hidden":""}>
                <div class="tune-head">
                    <h3>Настройка моей волны</h3>
                    <button class="icon-btn" id="waveTuneClose" title="Свернуть"><svg class="ic"><use href="#i-close"/></svg></button>
                </div>
                <div class="tune-hint">Изменения применяются автоматически</div>
                <div class="tune-group">
                    <div class="label">Под занятие</div>
                    <div class="tune-chips" data-group="occupy">
                        ${opts.occupy.map(o => `<button class="tune-chip ${isActive("occupy", o)}" data-val="${o}">${o}</button>`).join("")}
                    </div>
                </div>
                <div class="tune-group">
                    <div class="label">По характеру</div>
                    <div class="tune-chips" data-group="char">
                        ${opts.char.map(o => `<button class="tune-chip ${isActive("char", o.key)}" data-char="${o.key}" data-val="${o.key}"><span class="icon-circle"></span>${o.label}</button>`).join("")}
                    </div>
                </div>
                <div class="tune-group">
                    <div class="label">По настроению</div>
                    <div class="tune-chips" data-group="mood">
                        ${opts.mood.map(o => `<button class="tune-chip ${isActive("mood", o.key)}" data-mood="${o.key}" data-val="${o.key}"><span class="icon-circle"></span>${o.label}</button>`).join("")}
                    </div>
                </div>
                <div class="tune-group">
                    <div class="label">По языку</div>
                    <div class="tune-chips" data-group="lang">
                        ${opts.lang.map(o => `<button class="tune-chip ${isActive("lang", o)}" data-val="${o}">${o}</button>`).join("")}
                    </div>
                </div>
                <div class="tune-actions">
                    <button class="btn-secondary" id="waveTuneReset">Сбросить</button>
                </div>
            </div>
        </div>`;
    v.querySelectorAll(".tune-chip").forEach(b => b.onclick = () => {
        const group = b.parentElement.dataset.group;
        const val = b.dataset.val;
        // Single-select: повторный клик снимает выбор.
        if (state.waveTune[group] === val) state.waveTune[group] = null;
        else state.waveTune[group] = val;
        // Подсветить выбранный чип в группе.
        b.parentElement.querySelectorAll(".tune-chip").forEach(x => x.classList.toggle("active", state.waveTune[group] === x.dataset.val));
        vlog("wave tune set", group, "=", state.waveTune[group]);
        // Авто-применение: дебаунс 450мс — пользователь успевает выбрать комбинацию.
        _scheduleWaveAutoStart();
    });
    $("#waveBigPlay").onclick = toggleWave;
    // Переключение панели — БЕЗ перерисовки страницы (раньше было renderWave() → мерцание).
    const togglePanel = () => {
        state.waveCollapsed = !state.waveCollapsed;
        const grid = $("#waveGrid");
        const panel = $("#waveTunePanel");
        if (grid) grid.classList.toggle("is-collapsed", state.waveCollapsed);
        if (panel) panel.hidden = state.waveCollapsed;
        const tBtn = $("#waveTuneToggle");
        if (tBtn) {
            tBtn.title = state.waveCollapsed ? "Показать настройки" : "Скрыть настройки";
            const u = tBtn.querySelector("use");
            if (u) u.setAttribute("href", "#i-" + (state.waveCollapsed ? "settings" : "chev-right"));
            const lbl = tBtn.querySelector(".wave-tune-toggle-label");
            if (lbl) lbl.textContent = state.waveCollapsed ? "Настройки волны" : "Скрыть настройки";
        }
    };
    $("#waveTuneToggle").onclick = togglePanel;
    const closeBtn = $("#waveTuneClose"); if (closeBtn) closeBtn.onclick = () => { if (!state.waveCollapsed) togglePanel(); };
    const resetBtn = $("#waveTuneReset"); if (resetBtn) resetBtn.onclick = () => {
        state.waveTune = { occupy: null, char: null, mood: null, lang: null };
        v.querySelectorAll(".tune-chip.active").forEach(c => c.classList.remove("active"));
        _scheduleWaveAutoStart();
    };
    try {
        // Персональная подборка для авторизованных, чарт-фолбэк для гостей.
        const endpoint = state.me ? "/api/wave?limit=30" : "/api/charts?limit=30";
        const data = await api(endpoint).catch(() => api("/api/charts?limit=30"));
        const tracks = asTracks(data).slice(0, 30);
        $("#waveTracks").innerHTML = `<div class="track-list">${tracks.map(trackRowHtml).join("")}</div>`;
        bindTrackList($("#waveTracks"), tracks);
    } catch { $("#waveTracks").innerHTML = `<div class="hint">Не удалось загрузить.</div>`; }
}
let _waveStartInflight = false;
async function startWave() {
    if (_waveStartInflight) { vlog("wave already starting — ignoring duplicate click"); return; }
    _waveStartInflight = true;
    try {
        const params = new URLSearchParams();
        const tune = state.waveTune || {};
        for (const k of ["occupy", "char", "mood", "lang"]) {
            if (tune[k]) params.set(k, tune[k]);
        }
        // Бесконечная волна: первая порция большая (80 треков). Когда
        // приближаемся к концу — `_extendWaveQueue()` подгружает ещё.
        params.set("limit", "80");
        params.set("fresh", "1");
        const qs = params.toString();
        const data = await api("/api/wave?" + qs).catch(() => api("/api/charts?limit=80"));
        const tracks = asTracks(data).sort(() => Math.random() - 0.5);
        if (!tracks.length) return showToast("Не удалось запустить волну");
        // Сбрасываем dedup-сет и стартуем.
        state._wavePlayedKeys = new Set(tracks.map(_trackKey));
        playQueue(tracks, 0, { from_view: "wave" });
        const tags = Object.values(tune).filter(Boolean);
        showToast(tags.length ? `Моя волна: ${tags.join(" · ")}` : "Моя волна запущена");
    } catch (e) { showToast(e.message); }
    finally { _waveStartInflight = false; }
}

// Ключ трека для dedup'а в бесконечной волне.
function _trackKey(t) { return (t && (t.source || "") + "|" + (t.source_id || "")) || ""; }

// Бесконечная волна: когда от текущей позиции до конца очереди осталось
// ≤ TAIL_THRESHOLD треков, фоном подгружаем ещё одну партию из /api/wave
// и аппендим в state.queue (без дубликатов). Сервер каждый раз генерирует
// свежий набор: учитывает `_recordVisit` посещения, denylist дизлайков
// и текущие waveTune-параметры.
let _waveExtendInflight = false;
async function _extendWaveQueue() {
    if (_waveExtendInflight) return;
    if (state.queueOrigin !== "wave") return;
    if (!state.me) return; // у гостей бесконечная волна не нужна
    const TAIL_THRESHOLD = 8;
    if (!Array.isArray(state.queue)) return;
    if (state.queue.length - state.qi > TAIL_THRESHOLD) return;
    _waveExtendInflight = true;
    try {
        const params = new URLSearchParams();
        const tune = state.waveTune || {};
        for (const k of ["occupy", "char", "mood", "lang"]) {
            if (tune[k]) params.set(k, tune[k]);
        }
        params.set("limit", "60");
        params.set("fresh", "1");
        const data = await api("/api/wave?" + params.toString(), { silent: true });
        const fresh = asTracks(data).sort(() => Math.random() - 0.5);
        const seen = state._wavePlayedKeys || new Set(state.queue.map(_trackKey));
        const add = [];
        for (const t of fresh) {
            const k = _trackKey(t);
            if (!k || seen.has(k)) continue;
            seen.add(k);
            add.push(t);
        }
        state._wavePlayedKeys = seen;
        if (add.length) {
            state.queue.push(...add);
            vlog("wave extended +" + add.length + " (queue=" + state.queue.length + ")");
        } else {
            vlog("wave extend: no new tracks");
        }
    } catch (e) {
        vlog("wave extend err", e);
    } finally {
        _waveExtendInflight = false;
    }
}
// Если волна уже играет — пауза/продолжить, иначе — запуск.
function toggleWave() {
    if (state.queueOrigin === "wave" && state.track) {
        if (audio.paused) { audio.play().catch(()=>{}); }
        else { audio.pause(); }
        return;
    }
    return startWave();
}
function updateWavePlayIcons() {
    const isWave = state.queueOrigin === "wave" && state.track;
    const playing = isWave && !audio.paused;
    document.querySelectorAll(".wave-play .wave-play-ic use").forEach(u => {
        u.setAttribute("href", playing ? "#i-pause" : "#i-play");
    });
}

// ============== ЧАРТЫ ==============
async function renderCharts() {
    const v = $("#view");
    v.innerHTML = `<h2 class="section-title first">Чарт</h2><div class="hint">Загружаем…</div>`;
    try {
        const data = await apiCached("/api/charts?limit=50");
        const tracks = asTracks(data);
        v.innerHTML = `<h2 class="section-title first">Чарт</h2><div class="track-list">${tracks.map(trackRowHtml).join("")}</div>`;
        bindTrackList(v, tracks);
    } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}

// ============== КОЛЛЕКЦИЯ (скрин 5) ==============
async function renderLibrary() {
    if (!state.me) return openAuth();
    const v = $("#view");
    let likes = [];
    try { likes = asTracks(await apiCached("/api/likes")); } catch {}
    state.likedKeys = new Set(likes.map(t => (t.source||"")+"|"+(t.source_id||"")));
    v.innerHTML = `
        <div class="coll-head">
            <h1>Коллекция</h1>
            <div class="sub"><b>${likes.length}</b> ${likes.length==1?'трек':'треков'} • <b>${state.playlists.length}</b> ${state.playlists.length==1?'плейлист':'плейлистов'}</div>
        </div>
        <div class="coll-favorites" id="favRow">
            <div class="heart"><svg class="ic"><use href="#i-heart-fill"/></svg></div>
            <div style="flex:1">
                <div class="name">Мне нравится</div>
                <div class="stats">${likes.length} ${likes.length==1?'трек':'треков'}</div>
            </div>
            <button class="btn-primary" id="favPlay"><svg class="ic"><use href="#i-play"/></svg></button>
        </div>

        <div class="section-row">
            <h2>Мои плейлисты</h2>
            <span class="more" id="newPl">+ Новый плейлист</span>
        </div>
        <div class="cards" id="myPlsGrid">
            <div class="card card-playlist" id="newPlCard">
                <div class="card-cover placeholder"><svg class="ic ic-large"><use href="#i-plus"/></svg></div>
                <div class="c-title">Новый плейлист</div>
            </div>
            ${state.playlists.map(p => {
                const isFullDl = state.offline.fullPlaylists.has(String(p.id));
                return `
                <div class="card card-playlist${isFullDl?' is-dl':''}" data-pid="${p.id}">
                    <div class="card-cover ${p.cover?'':'placeholder'}"${p.cover?` style="background-image:url(${p.cover});background-size:cover;background-position:center"`:""}>
                        ${p.cover ? "" : `<svg class="ic ic-large"><use href="#i-library"/></svg>`}
                        ${isFullDl ? '<span class="dl-badge" title="Скачан полностью"><svg class="ic"><use href="#i-check"/></svg></span>' : ''}
                    </div>
                    <div class="c-title">${escapeHtml(p.name)}</div>
                    <div class="c-sub">${p.count||0} ${p.count==1?'трек':'треков'}</div>
                </div>`;
            }).join("")}
        </div>

        <h2 class="section-title">Любимые треки</h2>
        <div class="tracks-2col" id="favTracks">
            ${likes.length
                ? `<div class="track-list">${likes.slice(0, Math.ceil(likes.length/2)).map(trackRowHtml).join("")}</div>
                   <div class="track-list">${likes.slice(Math.ceil(likes.length/2)).map(trackRowHtml).join("")}</div>`
                : `<div class="hint">Пока пусто. Лайкайте треки — они появятся здесь.</div>`}
        </div>
    `;
    if (likes.length) bindTrackList($("#favTracks"), likes);
    $("#favPlay").onclick = (e) => { e.stopPropagation(); likes.length && playQueue(likes, 0); };
    $("#favRow").onclick = () => navigate("favorites");
    $("#newPl").onclick = createPlaylist;
    $("#newPlCard").onclick = createPlaylist;
    v.querySelectorAll("[data-pid]").forEach(c => c.onclick = () => navigate("playlist", { id: Number(c.dataset.pid) }));
}

async function createPlaylist() {
    const name = prompt("Название плейлиста:", "Новый плейлист");
    if (!name) return;
    try {
        const r = await api("/api/playlists", { method: "POST", body: { name } });
        await loadPlaylists();
        navigate("playlist", { id: r.id });
    } catch (e) { showToast(e.message); }
}

// ============== ПЛЕЙЛИСТ (скрин 6) ==============
async function renderPlaylistPage(pid) {
    if (!pid) return navigate("library");
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем…</div>`;
    try {
        const data = await apiCached(`/api/playlists/${pid}`);
        const p = data || {};
        const items = asTracks(data && data.items);
        const tracks = items;
        // Учитываем визит плейлиста в снимке предпочтений.
        _recordVisit({ kind: "playlist", id: String(pid), source: "local",
                       name: p.name || "", cover: p.cover || "" });
        const totalDur = tracks.reduce((s,t)=>s+(t.duration||0),0);
        const cover = p.cover || (tracks[0]?.album_cover);
        // Сколько треков уже скачано в офлайн.
        const dlNow = tracks.reduce((n,t) => n + (state.offline.downloaded.has(trackKey(t)) ? 1 : 0), 0);
        const dlAll = tracks.length > 0 && dlNow === tracks.length;
        markPlaylistDownloaded(pid, dlAll);
        const dlSome = dlNow > 0 && !dlAll;
        const dlIcon = dlAll ? "check" : "download";
        const dlLabel = dlAll ? "Скачано" : (dlSome ? `${dlNow}/${tracks.length}` : "Скачать");
        const dlClass = dlAll ? " is-downloaded" : (dlSome ? " is-partial" : "");
        v.innerHTML = `
            <div class="playlist-hero">
                <div class="playlist-cover" id="plCover" ${cover?`style="background-image:url(${cover})"`:""}>
                    <div class="edit-overlay"><svg class="ic"><use href="#i-image"/></svg></div>
                </div>
                <div class="playlist-info">
                    <div class="kind">Плейлист${p.pinned?' • закреплён':''}</div>
                    <h1>${escapeHtml(p.name)}</h1>
                    <div class="desc" id="plDesc">${escapeHtml(p.description || "Без описания")}</div>
                    <div class="meta">${tracks.length} ${tracks.length==1?'трек':'треков'} • ${fmtTime(totalDur)}</div>
                    <div class="playlist-actions">
                        <button class="btn-primary" id="plPlay"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
                        <button class="btn-secondary" id="plTrailer"><svg class="ic"><use href="#i-trailer"/></svg> Трейлер</button>
                        <button class="btn-secondary${dlClass}" id="plDownload" title="${dlAll?'Все треки уже скачаны':'Скачать все треки в офлайн'}"><svg class="ic"><use href="#i-${dlIcon}"/></svg> ${dlLabel}</button>
                        <button class="btn-secondary" id="plShare" title="Скопировать ссылку"><svg class="ic"><use href="#i-share"/></svg> Поделиться</button>
                        <button class="btn-secondary ${p.is_public?'is-public':'is-private'}" id="plTogglePublic" title="${p.is_public?'Сделать приватным':'Сделать публичным'}">
                            <svg class="ic"><use href="#i-${p.is_public?'globe':'lock'}"/></svg>
                            ${p.is_public?'Публичный':'Приватный'}
                        </button>
                        <button class="icon-btn ${p.pinned?'active':''}" id="plPin" title="Закрепить"><svg class="ic"><use href="#i-pin"/></svg></button>
                        <button class="icon-btn" id="plEdit" title="Редактировать"><svg class="ic"><use href="#i-settings"/></svg></button>
                        <button class="icon-btn" id="plDel" title="Удалить плейлист"><svg class="ic"><use href="#i-trash"/></svg></button>
                    </div>
                </div>
            </div>
            <div class="playlist-toolbar">
                <svg class="ic"><use href="#i-search"/></svg>
                <input id="plSearch" placeholder="Найти в плейлисте">
            </div>
            <div class="track-list" id="plTracks">
                ${tracks.length ? items.map((it, idx) => playlistRowHtml(it, idx)).join("") : `<div class="hint">Плейлист пуст. Добавляйте треки прямо из поиска или импортируйте из файла в настройках.</div>`}
            </div>`;
        const tracksList = $("#plTracks");
        tracksList._velTracks = tracks;
        tracksList.addEventListener("click", async (e) => {
            // Клик по имени артиста — открываем артиста (как в trackRowHtml).
            const goArt = e.target.closest("[data-act=goto-artist]");
            if (goArt) {
                e.preventDefault(); e.stopPropagation();
                const src = goArt.dataset.source || "deezer";
                const aid = goArt.dataset.aid || "";
                const aname = goArt.dataset.aname || goArt.textContent.trim();
                if (aid) { openArtist(src, aid, aname); return; }
                const row = goArt.closest(".track-row");
                const idx = row ? Number(row.dataset.idx) : -1;
                const t = (idx >= 0) ? tracks[idx] : null;
                if (t && t.source && t.source_id) {
                    try {
                        const full = await api(`/api/track/${encodeURIComponent(t.source)}/${encodeURIComponent(t.source_id)}`, { silent: true, silent404: true });
                        const list = (full && full.artists) || [];
                        if (list.length) t.artists = list;
                        const found = list.find(a => a && a.name && a.name.toLowerCase() === aname.toLowerCase());
                        if (found && found.id) { openArtist(src, String(found.id), found.name); return; }
                    } catch(_) {}
                }
                $("#q") && ($("#q").value = aname); navigate("search");
                return;
            }
            const row = e.target.closest(".track-row"); if (!row) return;
            if (e.target.closest("[data-act=remove]")) {
                e.stopPropagation();
                const rowId = Number(row.dataset.rowid);
                try {
                    await api(`/api/playlists/${pid}/items/${rowId}`, { method: "DELETE" });
                    showToast("Трек удалён"); renderPlaylistPage(pid);
                } catch (err) { showToast(err.message); }
                return;
            }
            if (e.target.closest("[data-act=like]")) {
                e.stopPropagation();
                const idx = Number(row.dataset.idx);
                await toggleLike(tracks[idx]);
                return;
            }
            if (e.target.closest("[data-act=more]")) {
                e.stopPropagation();
                const idx = Number(row.dataset.idx);
                openTrackMenu(tracks[idx], e.target.closest("[data-act=more]"));
                return;
            }
            const idx = Number(row.dataset.idx);
            playQueue(tracks, idx);
        });
        $("#plPlay").onclick = () => tracks.length && playQueue(tracks, 0);
        $("#plTrailer").onclick = () => playTrailer(pid);
        $("#plDownload").onclick = async () => {
            if (!tracks.length) return;
            const btn = $("#plDownload");
            // Повторный клик во время скачивания — отмена.
            if (state.dlAbort && !state.dlAbort.cancelled) {
                state.dlAbort.cancelled = true;
                btn.innerHTML = `<svg class="ic"><use href="#i-download"/></svg> Останавливаем…`;
                return;
            }
            const orig = btn.innerHTML;
            const abort = state.dlAbort = { cancelled: false };
            let done = 0; const total = tracks.length;
            btn.classList.add("is-downloading");
            for (const t of tracks) {
                if (abort.cancelled) break;
                btn.innerHTML = `<svg class="ic"><use href="#i-x"/></svg> Стоп ${++done}/${total}`;
                try { await downloadTrack(t); } catch {}
            }
            btn.classList.remove("is-downloading");
            btn.innerHTML = orig;
            state.dlAbort = null;
            renderPlaylistPage(pid);
            showToast(abort.cancelled ? `Загрузка остановлена (${done-1}/${total})` : "Плейлист скачан в офлайн");
        };
        $("#plCover").onclick = () => editPlaylistCover(pid);
        $("#plDesc").onclick = () => editPlaylistField(pid, "description", p.description||"", "Описание");
        $("#plPin").onclick = async () => {
            try { await api(`/api/playlists/${pid}`, { method: "PATCH", body: { pinned: !p.pinned } });
                  await loadPlaylists(); renderPlaylistPage(pid);
            } catch (e) { showToast(e.message); }
        };
        $("#plEdit").onclick = () => openEditPlaylist(pid, p);
        $("#plDel").onclick = async () => {
            if (!confirm(`Удалить плейлист «${p.name}»?`)) return;
            try { await api(`/api/playlists/${pid}`, { method: "DELETE" });
                  await loadPlaylists(); navigate("library");
            } catch (e) { showToast(e.message); }
        };
        // Поделиться: если плейлист уже публичный — копируем ссылку, иначе предлагаем включить.
        $("#plShare").onclick = async () => {
            const slug = p.slug || pid;
            const url = `${location.origin}/p/${slug}`;
            if (!p.is_public) {
                if (!confirm("Плейлист сейчас приватный — ссылка не сработает у других. Сделать его публичным и скопировать ссылку?")) return;
                try {
                    const upd = await api(`/api/playlists/${pid}`, { method: "PATCH", body: { is_public: true } });
                    Object.assign(p, upd || {});
                } catch (e) { return showToast(e.message); }
            }
            try { await navigator.clipboard.writeText(url); showToast("Ссылка скопирована: " + url); }
            catch { prompt("Скопируйте ссылку:", url); }
            await loadPlaylists(); renderPlaylistPage(pid);
        };
        // Тумблер «Публичный/Приватный» — одной кнопкой.
        $("#plTogglePublic").onclick = async () => {
            const next = !p.is_public;
            try {
                await api(`/api/playlists/${pid}`, { method: "PATCH", body: { is_public: next } });
                showToast(next ? "Плейлист теперь публичный" : "Плейлист стал приватным");
                await loadPlaylists(); renderPlaylistPage(pid);
            } catch (e) { showToast(e.message); }
        };
        // Поиск по плейлисту с дебаунсом — на 200+ треках без него лагает
        // (каждое нажатие переписывает display у сотен .track-row).
        let _plSearchTm = null;
        $("#plSearch").addEventListener("input", (e) => {
            const q = e.target.value.toLowerCase();
            if (_plSearchTm) clearTimeout(_plSearchTm);
            _plSearchTm = setTimeout(() => {
                _plSearchTm = null;
                const rows = tracksList.querySelectorAll(".track-row");
                for (const r of rows) {
                    const txt = r.textContent.toLowerCase();
                    r.style.display = txt.includes(q) ? "" : "none";
                }
            }, 140);
        });
    } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}
function playlistRowHtml(it, idx) {
    const k = (it.source||"")+"|"+(it.source_id||"");
    const liked = state.likedKeys.has(k);
    const playing = state.track && state.track.source === it.source && state.track.source_id === it.source_id;
    const exp = it.explicit ? `<span class="explicit-badge">E</span>` : "";
    // Кликабельные имена артистов (как в trackRowHtml). Если массива нет —
    // splитим строку по запятой, id первого подставляем из it.artist_id.
    let artList;
    if (Array.isArray(it.artists) && it.artists.length) {
        artList = it.artists;
    } else if (it.artist && it.artist.includes(",")) {
        artList = it.artist.split(/\s*,\s*/).filter(Boolean).map((nm, i) => ({
            id: i === 0 ? (it.artist_id || "") : "",
            name: nm,
        }));
    } else {
        artList = it.artist ? [{ id: it.artist_id || "", name: it.artist }] : [];
    }
    const artHtml = artList.length
        ? artList.map(a => {
            const aid = a.id ? ` data-aid="${escapeHtml(String(a.id))}"` : "";
            const nm = ` data-aname="${escapeHtml(a.name)}"`;
            return `<a class="t-sub-link" data-act="goto-artist"${aid}${nm} data-source="${escapeHtml(it.source||"")}">${escapeHtml(a.name)}</a>`;
          }).join(", ")
        : "";
    return `<div class="track-row ${playing?"playing":""}" data-idx="${idx}" data-rowid="${it.row_id}">
        <img src="${it.album_cover||""}" alt="" loading="lazy">
        <div class="t-meta">
            <div class="t-title"><span class="name">${escapeHtml(it.title)}</span>${exp}</div>
            <div class="t-sub">${artHtml}</div>
        </div>
        <span class="t-source">${it.source||""}</span>
        <button class="row-icon-btn ${liked?'is-liked':''}" data-act="like"><svg class="ic"><use href="#i-${liked?'heart-fill':'heart'}"/></svg></button>
        <button class="row-icon-btn" data-act="more" title="В плейлист…"><svg class="ic"><use href="#i-dots"/></svg></button>
        <span class="t-dur">${fmtTime(it.duration)}</span>
        <button class="row-icon-btn" data-act="remove" title="Удалить"><svg class="ic"><use href="#i-trash"/></svg></button>
    </div>`;
}

// ============== ЛЮБИМЫЕ ТРЕКИ (как плейлист) ==============
async function renderFavoritesPage() {
    if (!state.me) return openAuth();
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем…</div>`;
    try {
        const tracks = asTracks(await apiCached("/api/likes"));
        state.likedKeys = new Set(tracks.map(t => (t.source||"")+"|"+(t.source_id||"")));
        const totalDur = tracks.reduce((s,t)=>s+(t.duration||0),0);
        const metaKey = "velora_likes_meta_" + (state.me?.id || state.me?.username || "u");
        const meta = JSON.parse(localStorage.getItem(metaKey) || "{}");
        const cover = meta.cover || tracks[0]?.album_cover || "";
        const desc = (meta.desc != null && meta.desc !== "")
            ? meta.desc
            : "Треки, которые вы лайкнули. Нажмите на сердце, чтобы убрать.";
        v.innerHTML = `
            <div class="playlist-hero">
                <div class="playlist-cover playlist-cover-likes ${meta.cover?'has-custom-cover':''}" id="plCover" ${cover?`style="background-image:${_cssUrl(cover)}"`:""} title="Сменить обложку">
                    <div class="likes-emblem"><svg class="ic"><use href="#i-heart-fill"/></svg></div>
                    <div class="cover-edit-hint"><svg class="ic"><use href="#i-plus"/></svg></div>
                </div>
                <div class="playlist-info">
                    <div class="kind">Авто-плейлист</div>
                    <h1>Мне нравится</h1>
                    <div class="desc" id="plDesc" title="Изменить описание">${escapeHtml(desc)}</div>
                    <div class="meta">${tracks.length} ${tracks.length==1?'трек':'треков'} • ${fmtTime(totalDur)}</div>
                    <div class="playlist-actions">
                        <button class="btn-primary" id="plPlay"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
                        <button class="btn-secondary" id="plShuffle"><svg class="ic"><use href="#i-shuffle"/></svg> В случайном порядке</button>
                        <button class="btn-secondary" id="plDownload"><svg class="ic"><use href="#i-download"/></svg> Скачать</button>
                        <button class="btn-secondary" id="plShareLikes" title="Опубликовать как обычный плейлист и получить ссылку"><svg class="ic"><use href="#i-share"/></svg> Поделиться</button>
                        <button class="btn-secondary" id="plEdit"><svg class="ic"><use href="#i-settings"/></svg> Редактировать</button>
                    </div>
                    <div class="likes-privacy-hint">
                        <svg class="ic"><use href="#i-lock"/></svg>
                        <span>Авто-плейлист «Мне нравится» всегда приватный и виден только вам. Чтобы поделиться им — нажмите «Поделиться»: создам публичную копию-плейлист.</span>
                    </div>
                </div>
            </div>
            <div class="playlist-toolbar">
                <svg class="ic"><use href="#i-search"/></svg>
                <input id="plSearch" placeholder="Найти в любимых">
            </div>
            <div class="track-list" id="plTracks">
                ${tracks.length ? tracks.map((it, idx) => favRowHtml(it, idx)).join("") : `<div class="hint">Пока пусто. Лайкайте треки — они появятся здесь.</div>`}
            </div>`;
        const tracksList = $("#plTracks");
        tracksList.addEventListener("click", async (e) => {
            const row = e.target.closest(".track-row"); if (!row) return;
            if (e.target.closest("[data-act=like]")) {
                e.stopPropagation();
                const idx = Number(row.dataset.idx);
                await toggleLike(tracks[idx]);
                renderFavoritesPage();
                return;
            }
            if (e.target.closest("[data-act=more]")) {
                e.stopPropagation();
                const idx = Number(row.dataset.idx);
                openTrackMenu(tracks[idx], e.target.closest("[data-act=more]"));
                return;
            }
            const idx = Number(row.dataset.idx);
            playQueue(tracks, idx);
        });
        $("#plPlay").onclick = () => tracks.length && playQueue(tracks, 0);
        $("#plShuffle").onclick = () => {
            if (!tracks.length) return;
            const arr = tracks.slice();
            for (let i = arr.length-1; i > 0; i--) {
                const j = Math.floor(Math.random()*(i+1));
                [arr[i], arr[j]] = [arr[j], arr[i]];
            }
            playQueue(arr, 0);
        };
        $("#plDownload").onclick = async () => {
            if (!tracks.length) return;
            const btn = $("#plDownload");
            if (state.dlAbort && !state.dlAbort.cancelled) {
                state.dlAbort.cancelled = true;
                btn.innerHTML = `<svg class="ic"><use href="#i-download"/></svg> Останавливаем…`;
                return;
            }
            const orig = btn.innerHTML;
            const abort = state.dlAbort = { cancelled: false };
            let done = 0; const total = tracks.length;
            btn.classList.add("is-downloading");
            for (const t of tracks) {
                if (abort.cancelled) break;
                btn.innerHTML = `<svg class="ic"><use href="#i-x"/></svg> Стоп ${++done}/${total}`;
                try { await downloadTrack(t); } catch {}
            }
            btn.classList.remove("is-downloading");
            btn.innerHTML = orig;
            state.dlAbort = null;
            showToast(abort.cancelled ? `Загрузка остановлена (${done-1}/${total})` : "Любимые скачаны в офлайн");
        };
        $("#plCover").onclick = () => openImageUpload("favCover");
        $("#plDesc").onclick = () => editLikesDescription();
        $("#plEdit").onclick = () => editLikesDescription();
        $("#plShareLikes").onclick = () => shareLikesAsPlaylist();
        // Поиск по «Мне нравится» с дебаунсом — на 200+ лайках без него лагает.
        let _favSearchTm = null;
        $("#plSearch").addEventListener("input", (e) => {
            const q = e.target.value.toLowerCase();
            if (_favSearchTm) clearTimeout(_favSearchTm);
            _favSearchTm = setTimeout(() => {
                _favSearchTm = null;
                const rows = tracksList.querySelectorAll(".track-row");
                for (const r of rows) {
                    const txt = r.textContent.toLowerCase();
                    r.style.display = txt.includes(q) ? "" : "none";
                }
            }, 140);
        });
    } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}

function editLikesDescription() {
    const metaKey = "velora_likes_meta_" + (state.me?.id || state.me?.username || "u");
    const meta = JSON.parse(localStorage.getItem(metaKey) || "{}");
    const cur = meta.desc != null ? meta.desc : "";
    // Используем общую модалку #editModal вместо браузерного prompt().
    $("#editTitle").textContent = "Описание плейлиста «Мне нравится»";
    $("#editError").hidden = true;
    $("#editBody").innerHTML = `
        <textarea id="edLikesDesc" rows="4" maxlength="500" placeholder="Например: моя любимая подборка на лето">${escapeHtml(cur)}</textarea>
        <small class="muted">Описание видно только вам — это локальный авто-плейлист.</small>
    `;
    $("#editModal").hidden = false;
    setTimeout(() => $("#edLikesDesc")?.focus(), 30);
    $("#editSave").onclick = () => {
        meta.desc = ($("#edLikesDesc").value || "").slice(0, 500);
        localStorage.setItem(metaKey, JSON.stringify(meta));
        $("#editModal").hidden = true;
        renderFavoritesPage();
        showToast("Описание сохранено");
    };
}

// Создаёт обычный плейлист-копию из всех лайкнутых треков и публикует его
// (можно делиться ссылкой). Используется кнопкой «Поделиться» на странице
// «Мне нравится» — потому что сам авто-плейлист по своей природе локальный
// и публичной ссылки иметь не может.
async function shareLikesAsPlaylist() {
    if (!state.me) return openAuth();
    try {
        const tracks = asTracks(await api("/api/likes"));
        if (!tracks.length) return showToast("В «Мне нравится» нет треков");
        if (!confirm(`Создать публичный плейлист «Любимые от ${state.me.display_name||state.me.username}» из ${tracks.length} треков? Им можно будет поделиться по ссылке.`)) return;
        const created = await api("/api/playlists", { method: "POST", body: {
            name: `Любимые от ${state.me.display_name||state.me.username}`,
            description: "Публичный снимок моего авто-плейлиста «Мне нравится»."
        }});
        const pid = created.id;
        // Добавляем все треки последовательно (порциями по 10 параллельных запросов).
        for (let i = 0; i < tracks.length; i += 10) {
            const chunk = tracks.slice(i, i+10);
            await Promise.allSettled(chunk.map(t => api(`/api/playlists/${pid}/add`, {
                method: "POST", body: t
            })));
        }
        // Включаем публичность.
        const upd = await api(`/api/playlists/${pid}`, { method: "PATCH", body: { is_public: true } });
        const slug = upd.slug || pid;
        const url = `${location.origin}/p/${slug}`;
        try { await navigator.clipboard.writeText(url); } catch {}
        showToast("Плейлист создан и опубликован — ссылка скопирована");
        await loadPlaylists();
        navigate("playlist", { id: pid });
    } catch (e) {
        showToast(e.message || "Не удалось создать публичный плейлист");
    }
}
function favRowHtml(it, idx) {
    const playing = state.track && state.track.source === it.source && state.track.source_id === it.source_id;
    const exp = it.explicit ? `<span class="explicit-badge">E</span>` : "";
    return `<div class="track-row ${playing?"playing":""}" data-idx="${idx}" data-key="${(it.source||"")+"|"+(it.source_id||"")}">
        <img src="${it.album_cover||""}" alt="" loading="lazy">
        <div class="t-meta">
            <div class="t-title"><span class="name">${escapeHtml(it.title)}</span>${exp}</div>
            <div class="t-sub">${escapeHtml(it.artist)}</div>
        </div>
        <span class="t-source">${it.source||""}</span>
        <button class="row-icon-btn is-liked" data-act="like" title="Убрать из любимого"><svg class="ic"><use href="#i-heart-fill"/></svg></button>
        <button class="row-icon-btn" data-act="more" title="В плейлист…"><svg class="ic"><use href="#i-dots"/></svg></button>
        <span class="t-dur">${fmtTime(it.duration)}</span>
    </div>`;
}

// ============== ТРЕЙЛЕР ==============
async function playTrailer(pid) {
    try {
        const data = await api(`/api/playlists/${pid}/trailer`);
        const items = asTracks(data && data.items);
        if (!items.length) return showToast("Плейлист пуст — нечего показывать");
        const snippet = (data && data.snippet_seconds) || 25;
        const tracks = items;
        state.trailerMode = { playlistId: pid, snippet };
        showToast(`Трейлер: ${tracks.length} треков по ~${snippet} сек`);
        playQueue(tracks, 0, { trailer: true });
    } catch (e) { showToast(e.message); }
}

// ============== ИСТОРИЯ ==============
const HISTORY_ORIGINS = {
    home:     { label: "Главная",   icon: "i-home" },
    search:   { label: "Поиск",     icon: "i-search" },
    charts:   { label: "Чарты",     icon: "i-fire" },
    wave:     { label: "Моя волна", icon: "i-wave" },
    playlist: { label: "Плейлист",  icon: "i-library" },
    artist:   { label: "Артист",    icon: "i-mic" },
    library:  { label: "Коллекция", icon: "i-library" },
    other:    { label: "—",         icon: "i-clock" },
};
function _historyDayLabel(d) {
    if (!d) return "Раньше";
    const dt = new Date(d);
    if (isNaN(+dt)) return "Раньше";
    const today = new Date(); today.setHours(0,0,0,0);
    const that = new Date(dt); that.setHours(0,0,0,0);
    const diff = Math.round((today - that) / 86400000);
    if (diff === 0) return "Сегодня";
    if (diff === 1) return "Вчера";
    if (diff < 7) return dt.toLocaleDateString("ru-RU", { weekday: "long" });
    return dt.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: dt.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}
function _historyTimeLabel(d) {
    if (!d) return "";
    const dt = new Date(d); if (isNaN(+dt)) return "";
    return dt.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

async function renderHistory() {
    if (!state.me) return openAuth();
    const v = $("#view");
    try {
        const data = await apiCached("/api/history?limit=200");
        const tracks = asTracks(data);
        // Сервер уже мерджит подряд идущие повторы. Дополнительно группируем по дням.
        const groups = []; const byDay = new Map();
        for (const t of tracks) {
            const day = _historyDayLabel(t.played_at);
            if (!byDay.has(day)) { const arr = []; byDay.set(day, arr); groups.push({ day, tracks: arr }); }
            byDay.get(day).push(t);
        }
        const renderRow = (t, idx, baseIdx) => {
            const o = HISTORY_ORIGINS[t.from_view || "other"] || HISTORY_ORIGINS.other;
            const time = _historyTimeLabel(t.played_at);
            const count = (t.play_count || 1) > 1 ? ` ×${t.play_count}` : "";
            const meta = `<div class="hist-meta">
                <span class="hist-origin"><svg class="ic"><use href="#${o.icon}"/></svg>${escapeHtml(o.label)}</span>
                <span class="hist-time">${escapeHtml(time)}${escapeHtml(count)}</span>
            </div>`;
            const row = trackRowHtml(t, baseIdx + idx);
            // Вставляем meta перед закрытием самого внешнего <div class="track-row">.
            const lastClose = row.lastIndexOf("</div>");
            return row.slice(0, lastClose) + meta + row.slice(lastClose);
        };
        let html = `<div class="section-row">
            <h2>История прослушиваний</h2>
            ${tracks.length ? `<button class="btn-link" id="histClear">Очистить</button>` : ""}
        </div>`;
        if (!tracks.length) {
            html += `<div class="hint">Пока пусто. Включите трек, и он появится здесь.</div>`;
        } else {
            let base = 0;
            for (const g of groups) {
                html += `<h3 class="hist-day">${escapeHtml(g.day)}</h3>
                    <div class="track-list hist-list">${g.tracks.map((t, i) => renderRow(t, i, base)).join("")}</div>`;
                base += g.tracks.length;
            }
        }
        v.innerHTML = html;
        if (tracks.length) {
            // bindTrackList работает по data-idx → передаём общий список (порядок тот же).
            v.querySelectorAll(".hist-list").forEach((list) => {
                bindTrackList(list, tracks);
            });
            $("#histClear").onclick = async () => {
                if (!confirm("Очистить историю?")) return;
                try { await api("/api/history", { method: "DELETE" }); renderHistory(); } catch (e) { showToast(e.message); }
            };
        }
    } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}

// ============== ДИЗЛАЙКИ ==============
async function renderDislikes() {
    if (!state.me) return openAuth();
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем дизлайки…</div>`;
    let rows = [];
    try { rows = await api("/api/dislikes"); } catch (e) { v.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; return; }
    const tracks = (rows || []).filter(r => r.scope !== "artist").map(r => ({
        source: r.source, source_id: r.track_id, id: r.track_id,
        title: r.title || "—", artist: r.artist || "", album: "",
        cover_big: r.cover, cover_small: r.cover, album_cover: r.cover,
        duration: 0, explicit: false, artist_id: r.artist_id || "",
    }));
    const artists = (rows || []).filter(r => r.scope === "artist");
    v.innerHTML = `
        <button class="settings-back" data-go="settings">
            <span class="back-circle"><svg class="ic"><use href="#i-chev-left"/></svg></span>
            Дизлайки
        </button>
        <div class="section-row">
            <div class="hint">Эти треки и артисты не появятся в Моей волне.</div>
            ${tracks.length ? `<button class="btn-secondary" id="dislPl">Создать плейлист «Дизлайки»</button>` : ""}
        </div>
        ${artists.length ? `<h3 class="hist-day">Артисты</h3>
            <div class="cards">${artists.map(a => `
                <div class="card artist" data-aid="${escapeHtml(a.artist_id||"")}" data-source="${escapeHtml(a.source||"deezer")}">
                    <div class="card-cover ${a.cover?"":"placeholder"}">
                        ${a.cover ? `<img src="${escapeHtml(a.cover)}" alt="">` : `<svg class="ic"><use href="#i-mic"/></svg>`}
                    </div>
                    <div class="c-title">${escapeHtml(a.artist||"—")}</div>
                    <button class="btn-link" data-undislike-artist="${a.id}">Убрать</button>
                </div>`).join("")}
            </div>` : ""}
        ${tracks.length ? `<h3 class="hist-day">Треки</h3>
            <div class="track-list" id="dislList">${tracks.map(trackRowHtml).join("")}</div>` :
            `<div class="hint">Дизлайков пока нет.</div>`}
    `;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) return navigate(g.dataset.go);
        const ua = e.target.closest("[data-undislike-artist]");
        if (ua) {
            const rid = Number(ua.dataset.undislikeArtist);
            api("/api/dislikes", { method: "DELETE", body: { row_id: rid } })
                .then(() => renderDislikes()).catch(err => showToast(err.message));
            return;
        }
        const card = e.target.closest(".card.artist");
        if (card && !e.target.closest("button")) {
            openArtist(card.dataset.source||"deezer", card.dataset.aid, card.querySelector(".c-title").textContent);
        }
    };
    if (tracks.length) bindTrackList($("#dislList"), tracks);
    const plBtn = $("#dislPl");
    if (plBtn) plBtn.onclick = async () => {
        try {
            const pl = await api("/api/playlists", { method: "POST", body: { name: "Дизлайки 💀" } });
            // Добавляем все треки разом
            for (const t of tracks) {
                try { await api(`/api/playlists/${pl.id}/add`, { method: "POST", body: t }); } catch {}
            }
            await loadPlaylists();
            renderSidebarPlaylists();
            showToast("Плейлист создан");
            navigate("playlist", { id: pl.id });
        } catch (e) { showToast(e.message); }
    };
}

// ============== ПРЕДПОЧТЕНИЯ АРТИСТОВ ==============
// state.artistPrefs: Map<"source|id", "like"|"dislike">
async function renderArtistPrefs() {
    if (!state.me) return openAuth();
    const v = $("#view");
    if (!state.artistPrefs) state.artistPrefs = new Map();
    if (!state._artistPrefsState) {
        state._artistPrefsState = { q: "", offset: 0, limit: 60, loading: false, items: [], hasMore: true, total: 0 };
    }
    const s = state._artistPrefsState;

    v.innerHTML = `
        <button class="settings-back" data-go="settings">
            <span class="back-circle"><svg class="ic"><use href="#i-chev-left"/></svg></span>
            Предпочтения артистов
        </button>
        <div class="ap-head">
            <div class="ap-intro">
                Отметьте исполнителей: <b>сердце</b> — будем чаще предлагать их в Моей волне,
                <b>Ø</b> — никогда не показывать. Можно изменить в любой момент.
            </div>
            <div class="ap-search-wrap">
                <input type="search" id="apSearch" placeholder="Найти исполнителя…"
                       class="ap-search" value="${escapeHtml(s.q)}" autocomplete="off" spellcheck="false">
                <span class="ap-stat" id="apStat"></span>
            </div>
        </div>
        <div class="ap-grid" id="apGrid">${s.items.length ? "" : `<div class="hint">Загружаем артистов…</div>`}</div>
        <div class="ap-more-wrap"><button class="btn-secondary" id="apMore" hidden>Показать ещё</button></div>`;

    // Сначала подгружаем актуальные предпочтения с сервера (для статуса лайк/дизлайк).
    try {
        const prefs = await api("/api/artists/preferences");
        state.artistPrefs.clear();
        for (const p of (prefs || [])) {
            state.artistPrefs.set(`${p.source||"deezer"}|${p.artist_id}`, p.kind);
        }
    } catch {}

    if (!s.items.length) await _loadArtistPrefsPage(true);
    _renderArtistPrefsGrid();

    // Поиск с дебаунсом — каждый ввод сбрасывает страницу.
    let _t = null;
    const apSearch = document.getElementById("apSearch");
    if (!apSearch) return; // пользователь успел уйти со страницы за время api()
    apSearch.addEventListener("input", (e) => {
        clearTimeout(_t);
        const val = e.target.value;
        _t = setTimeout(async () => {
            s.q = val.trim();
            s.offset = 0;
            s.items = [];
            s.hasMore = true;
            $("#apGrid").innerHTML = `<div class="hint">Ищем «${escapeHtml(s.q)||"артистов"}»…</div>`;
            await _loadArtistPrefsPage(true);
            _renderArtistPrefsGrid();
        }, 280);
    });
    $("#apMore").onclick = async () => { await _loadArtistPrefsPage(false); _renderArtistPrefsGrid(); };
    // Делегированные клики по кнопкам like/dislike.
    $("#apGrid").onclick = (e) => {
        const btn = e.target.closest("[data-ap-act]");
        if (!btn) {
            const card = e.target.closest(".ap-card");
            if (card && card.dataset.aid) {
                openArtist(card.dataset.source||"deezer", card.dataset.aid, card.dataset.name||"");
            }
            return;
        }
        e.stopPropagation();
        const card = btn.closest(".ap-card"); if (!card) return;
        const id = card.dataset.aid; const src = card.dataset.source || "deezer";
        const key = `${src}|${id}`;
        const cur = state.artistPrefs.get(key) || null;
        const want = btn.dataset.apAct;  // "like" | "dislike"
        const next = cur === want ? null : want;  // повторный клик = снять
        _setArtistPref({
            artist_id: id, source: src,
            name: card.dataset.name || "", image: card.dataset.image || "",
        }, next);
    };
}

async function _loadArtistPrefsPage(reset) {
    const s = state._artistPrefsState;
    if (s.loading || (!reset && !s.hasMore)) return;
    s.loading = true;
    try {
        const params = new URLSearchParams();
        if (s.q) params.set("q", s.q);
        params.set("offset", String(reset ? 0 : s.offset));
        params.set("limit", String(s.limit));
        const data = await api("/api/artists/catalog?" + params.toString());
        const list = (data && data.items) || [];
        if (reset) { s.items = list; s.offset = list.length; }
        else { s.items = s.items.concat(list); s.offset += list.length; }
        s.hasMore = !!(data && data.has_more);
        s.total = (data && data.total) || s.items.length;
    } catch (e) {
        showToast(e.message || "Не удалось загрузить");
        s.hasMore = false;
    } finally { s.loading = false; }
}

function _renderArtistPrefsGrid() {
    const s = state._artistPrefsState;
    const grid = $("#apGrid"); if (!grid) return;
    if (!s.items.length) {
        grid.innerHTML = `<div class="hint">Ничего не найдено.</div>`;
    } else {
        grid.innerHTML = s.items.map(_artistPrefCardHtml).join("");
    }
    const stat = $("#apStat");
    if (stat) {
        const liked = [...state.artistPrefs.values()].filter(k => k === "like").length;
        const dis = [...state.artistPrefs.values()].filter(k => k === "dislike").length;
        stat.textContent = `Лайков: ${liked} · Ø: ${dis} · показано ${s.items.length}`;
    }
    const more = $("#apMore"); if (more) more.hidden = !s.hasMore;
}

function _artistPrefCardHtml(a) {
    const key = `${a.source||"deezer"}|${a.id}`;
    const kind = state.artistPrefs.get(key) || null;
    const liked = kind === "like";
    const disliked = kind === "dislike";
    const img = a.image || "";
    return `<div class="ap-card ${liked?'is-liked':''} ${disliked?'is-disliked':''}"
        data-aid="${escapeHtml(a.id)}" data-source="${escapeHtml(a.source||"deezer")}"
        data-name="${escapeHtml(a.name||"")}" data-image="${escapeHtml(img)}">
        <div class="ap-cover ${img?'':'placeholder'}">
            ${img ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" decoding="async"
                       onerror="const p=this.parentNode;p.classList.add('placeholder');p.innerHTML='<svg class=\\'ic\\'><use href=\\'#i-mic\\'/></svg>';">`
                  : `<svg class="ic"><use href="#i-mic"/></svg>`}
        </div>
        <div class="ap-name">${escapeHtml(a.name||"—")}</div>
        <div class="ap-actions">
            <button class="ap-btn ap-like ${liked?'is-on':''}" data-ap-act="like"
                title="${liked?'Убрать лайк':'Нравится'}">
                <svg class="ic"><use href="#i-${liked?'heart-fill':'heart'}"/></svg>
            </button>
            <button class="ap-btn ap-dislike ${disliked?'is-on':''}" data-ap-act="dislike"
                title="${disliked?'Убрать дизлайк':'Не показывать'}">
                <svg class="ic"><use href="#i-no-entry"/></svg>
            </button>
        </div>
    </div>`;
}

// Универсальные кнопки like/Ø для конкретного артиста (страница артиста и т.п.).
// Использует state.artistPrefs для актуального состояния.
function _artistPrefBtnsHtml(source, id, name, image) {
    if (!id) return "";
    const key = `${source||"deezer"}|${id}`;
    const kind = (state.artistPrefs && state.artistPrefs.get(key)) || null;
    const liked = kind === "like";
    const disliked = kind === "dislike";
    const dataName = name ? ` data-aname="${escapeHtml(name)}"` : "";
    const dataImg = image ? ` data-aimg="${escapeHtml(image)}"` : "";
    return `
        <button class="apb apb-like ${liked?'is-on':''}" data-ap-action="like"
            data-source="${escapeHtml(source||"deezer")}" data-aid="${escapeHtml(String(id))}"${dataName}${dataImg}
            title="${liked?'Убрать лайк':'Нравится'}" aria-label="Нравится">
            <svg class="ic"><use href="#i-${liked?'heart-fill':'heart'}"/></svg>
        </button>
        <button class="apb apb-dislike ${disliked?'is-on':''}" data-ap-action="dislike"
            data-source="${escapeHtml(source||"deezer")}" data-aid="${escapeHtml(String(id))}"${dataName}${dataImg}
            title="${disliked?'Убрать дизлайк':'Не показывать этого артиста'}" aria-label="Дизлайк">
            <svg class="ic"><use href="#i-no-entry"/></svg>
        </button>`;
}

// Глобальный делегированный обработчик кнопок like/Ø артиста.
document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ap-action]");
    if (!btn) return;
    // Не перехватываем кнопки внутри страницы предпочтений (там свой обработчик через .ap-card).
    if (btn.closest(".ap-card")) return;
    e.preventDefault(); e.stopPropagation();
    const id = btn.dataset.aid; if (!id) return;
    const source = btn.dataset.source || "deezer";
    const name = btn.dataset.aname || "";
    const image = btn.dataset.aimg || "";
    const action = btn.dataset.apAction;
    const key = `${source}|${id}`;
    if (!state.artistPrefs) state.artistPrefs = new Map();
    const cur = state.artistPrefs.get(key) || null;
    const next = (cur === action) ? null : action; // повторный клик = снять
    await _setArtistPref({ artist_id: id, source, name, image }, next);
    // Обновляем все кнопки этого артиста на странице.
    document.querySelectorAll(`[data-ap-action][data-aid="${CSS.escape(id)}"][data-source="${CSS.escape(source)}"]`).forEach(b => {
        const a2 = b.dataset.apAction;
        const on = (state.artistPrefs.get(key) === a2);
        b.classList.toggle("is-on", on);
        const useEl = b.querySelector("svg use");
        if (useEl && a2 === "like") {
            useEl.setAttribute("href", on ? "#i-heart-fill" : "#i-heart");
        }
    });
});

async function _setArtistPref(artist, kind) {
    if (offlineBlocked("Лайк/дизлайк артиста")) return;
    const key = `${artist.source||"deezer"}|${artist.artist_id}`;
    // Оптимистично обновляем UI.
    if (kind == null) state.artistPrefs.delete(key);
    else state.artistPrefs.set(key, kind);
    // Перерисовываем только конкретную карточку (если она на странице есть).
    const card = document.querySelector(`.ap-card[data-aid="${CSS.escape(artist.artist_id)}"][data-source="${CSS.escape(artist.source||'deezer')}"]`);
    if (card) {
        card.outerHTML = _artistPrefCardHtml({
            id: artist.artist_id, source: artist.source,
            name: artist.name, image: artist.image,
        });
    }
    const stat = $("#apStat");
    if (stat) {
        const liked = [...state.artistPrefs.values()].filter(k => k === "like").length;
        const dis = [...state.artistPrefs.values()].filter(k => k === "dislike").length;
        const total = state._artistPrefsState ? state._artistPrefsState.items.length : 0;
        stat.textContent = `Лайков: ${liked} · Ø: ${dis} · показано ${total}`;
    }
    try {
        await api("/api/artists/preferences", {
            method: "POST",
            body: {
                artist_id: artist.artist_id, source: artist.source || "deezer",
                name: artist.name || "", image: artist.image || "",
                kind: kind,
            },
        });
    } catch (e) {
        // Откат при ошибке.
        if (kind == null) state.artistPrefs.set(key, kind);
        else state.artistPrefs.delete(key);
        showToast(e.message || "Не удалось сохранить");
    }
}

// ============== ОФЛАЙН РЕЖИМ ==============
async function renderOffline() {
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем…</div>`;
    const items = await listDownloads();
    const total = items.reduce((s, r) => s + (r.size || 0), 0);
    // Преобразуем записи IDB к формату трека для рендера и воспроизведения.
    const tracks = items
        .sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0))
        .map(r => ({
            source: r.meta?.source || "deezer",
            source_id: r.meta?.source_id || "",
            id: r.meta?.source_id || "",
            title: r.meta?.title || "—",
            artist: r.meta?.artist || "",
            album: r.meta?.album || "",
            artist_id: r.meta?.artist_id || "",
            cover_big: r.meta?.cover || "",
            cover_small: r.meta?.cover || "",
            album_cover: r.meta?.cover || "",
            duration: r.meta?.duration || 0,
            explicit: !!r.meta?.explicit,
            _size: r.size || 0,
        }));
    v.innerHTML = `
        <button class="settings-back" data-go="settings">
            <span class="back-circle"><svg class="ic"><use href="#i-chev-left"/></svg></span>
            Офлайн режим
        </button>
        <div class="offline-card">
            <label class="ed-toggle">
                <input type="checkbox" id="offEnabled" ${state.offline.enabled?"checked":""}>
                <span><b>Только офлайн</b><br><small>Воспроизводить только скачанные треки. Незагруженные будут пропущены.</small></span>
            </label>
            <div class="offline-stats">
                <div><b>${tracks.length}</b><span>${tracks.length==1?'трек':'треков'}</span></div>
                <div><b>${escapeHtml(fmtBytes(total))}</b><span>занято</span></div>
                <div><b>${navigator.onLine?'Онлайн':'Офлайн'}</b><span>сеть</span></div>
            </div>
            ${tracks.length ? `<div class="offline-quick">
                <button class="btn-primary" id="offPlay"><svg class="ic"><use href="#i-play"/></svg> Слушать всё</button>
                <button class="btn-secondary" id="offShuffle"><svg class="ic"><use href="#i-shuffle"/></svg> Перемешать</button>
            </div>` : ""}
            <div class="offline-actions">
                <button class="btn-secondary" id="offClear" ${tracks.length?"":"disabled"}>Удалить все загрузки</button>
            </div>
        </div>
        ${tracks.length ? `<h3 class="hist-day">Скачанные треки</h3>
            <div class="track-list" id="offList">${tracks.map(trackRowHtml).join("")}</div>` :
            `<div class="hint">Пока нет скачанных треков. Нажмите кнопку <svg class="ic"><use href="#i-download"/></svg> рядом с любым треком, чтобы добавить его в офлайн.</div>`}
    `;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) return navigate(g.dataset.go);
    };
    $("#offEnabled").onchange = async (e) => {
        state.offline.enabled = e.target.checked;
        await offlineSaveSettings();
        applyOfflineUi();
        showToast(state.offline.enabled ? "Офлайн режим включён" : "Офлайн режим выключен");
    };
    if (tracks.length) {
        $("#offPlay") && ($("#offPlay").onclick = () => playQueue(tracks, 0));
        $("#offShuffle") && ($("#offShuffle").onclick = () => {
            const shuffled = tracks.slice().sort(() => Math.random() - 0.5);
            playQueue(shuffled, 0);
        });
        bindTrackList($("#offList"), tracks);
        // Подменяем обложки на blob-URL из IDB (чтобы работали без сети).
        $$(".track-row", $("#offList")).forEach(async (row, i) => {
            const img = row.querySelector("img");
            if (!img) return;
            const u = await offlineGetCoverUrl(tracks[i]);
            if (u) img.src = u;
        });
        $("#offClear").onclick = async () => {
            if (!confirm("Удалить все скачанные треки? Их можно будет загрузить снова.")) return;
            await clearAllDownloads();
            renderOffline();
        };
    }
}

// ============== НАСТРОЙКИ ==============

// =================================================================
// SESSIONS MODAL
// =================================================================
async function openSessionsModal() {
    const wrap = document.createElement("div");
    wrap.className = "modal";
    wrap.id = "sessionsModal";
    wrap.innerHTML = `
        <div class="modal-box" style="max-width:560px">
            <button class="close" data-close>×</button>
            <h2>Активные сессии</h2>
            <p class="auth-hint">Список устройств, где вы вошли. Завершите чужие сессии.</p>
            <div id="sessionsList" class="sessions-list"><div class="sessions-loading">Загрузка…</div></div>
            <div class="modal-actions" style="margin-top:14px">
                <button id="sessionsRevokeAll" class="btn-link" style="color:#ff6b6b">Завершить все остальные</button>
            </div>
        </div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener("click", e => {
        if (e.target.closest("[data-close]") || e.target === wrap) {
            wrap.remove();
        }
    });
    await refreshSessionsList();
    const all = wrap.querySelector("#sessionsRevokeAll");
    all.onclick = async () => {
        if (!confirm("Завершить все сессии, кроме текущей?")) return;
        try {
            await api("/api/me/sessions/all", { method: "DELETE" });
            showToast("Остальные сессии завершены");
            await refreshSessionsList();
        } catch (e) { showToast(e.message || "Ошибка"); }
    };
}

async function refreshSessionsList() {
    const box = document.getElementById("sessionsList");
    if (!box) return;
    try {
        const r = await fetch("/api/me/sessions").then(r => r.json());
        const rows = r.sessions || [];
        if (!rows.length) {
            box.innerHTML = `<div class="sessions-empty">Нет активных сессий</div>`;
            return;
        }
        const fmt = (iso) => {
            if (!iso) return "—";
            try {
                const d = new Date(iso);
                return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
            } catch { return iso; }
        };
        box.innerHTML = rows.map(s => `
            <div class="session-card${s.current?' is-current':''}">
                <div class="session-card-head">
                    <div class="session-platform">
                        <b>${escapeHtml(s.platform || "Неизвестно")}</b>
                        <span class="session-browser">${escapeHtml(s.browser || "")}</span>
                        ${s.current ? '<span class="session-badge">текущая</span>' : ""}
                    </div>
                    ${s.current ? "" : `<button class="btn-link session-kill" data-sid="${s.id}" style="color:#ff6b6b">Завершить</button>`}
                </div>
                <div class="session-meta">
                    <div><span class="sm-key">IP:</span> <span class="mono">${escapeHtml(s.ip || "—")}</span></div>
                    <div><span class="sm-key">Источник:</span> ${escapeHtml(s.provider || "—")}</div>
                    <div><span class="sm-key">Вход:</span> ${escapeHtml(fmt(s.created_at))}</div>
                    <div><span class="sm-key">Активность:</span> ${escapeHtml(fmt(s.last_seen))}</div>
                </div>
            </div>
        `).join("");
        box.querySelectorAll(".session-kill").forEach(b => {
            b.onclick = async (e) => {
                const sid = e.currentTarget.dataset.sid;
                if (!confirm("Завершить эту сессию? Пользователь будет выкинут из аккаунта.")) return;
                try {
                    await api(`/api/me/sessions/${sid}`, { method: "DELETE" });
                    showToast("Сессия завершена");
                    await refreshSessionsList();
                } catch (err) { showToast(err.message || "Ошибка"); }
            };
        });
    } catch (e) {
        box.innerHTML = `<div class="sessions-empty">Ошибка загрузки: ${escapeHtml(e.message || "")}</div>`;
    }
}

function renderSettings() {
    const v = $("#view");
    const me = state.me || {};
    const sources = Object.entries(state.sources).filter(([_,v])=>v).map(([k])=>k);
    const quality = localStorage.getItem("velora_quality") || "std";
    const qLabel = ({low:"Низкое", std:"Оптимальное", high:"Высокое", max:"Максимальное"})[quality] || "Оптимальное";
    const lyricsFollow = localStorage.getItem("velora_lyrics_follow") !== "0";
    const autoplay = localStorage.getItem("velora_autoplay") !== "0";
    const autoDlLikes = localStorage.getItem("velora_auto_dl_likes") === "1";
    const autoLyrics = localStorage.getItem("velora_auto_lyrics") === "1";
    const notifs = localStorage.getItem("velora_notifs") === "1";
    const reduceMotion = localStorage.getItem("velora_reduce_motion") === "1";
    const crossfade = Number(localStorage.getItem("velora_crossfade") || 0);
    v.innerHTML = `
        <button class="settings-back" data-go="home">
            <span class="back-circle"><svg class="ic"><use href="#i-chev-left"/></svg></span>
            Настройки
        </button>
        <div class="settings-list">
            <div class="settings-section-title">Звук</div>
            <div class="settings-row" id="srEq">
                <div><div class="sr-name">Эквалайзер</div><div class="sr-sub">15-полосная настройка с пресетами</div></div>
                <div class="sr-right"><span>Скоро…</span><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            <div class="settings-row" id="srQuality">
                <div><div class="sr-name">Качество звука</div><div class="sr-sub">Битрейт потока (если поддерживается источником)</div></div>
                <div class="sr-right">
                    <select id="qualityInline" class="settings-inline-select">
                        <option value="low" ${quality==="low"?"selected":""}>Низкое</option>
                        <option value="std" ${quality==="std"?"selected":""}>Оптимальное</option>
                        <option value="high" ${quality==="high"?"selected":""}>Высокое</option>
                        <option value="max" ${quality==="max"?"selected":""}>Максимальное</option>
                    </select>
                </div>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Громкость</div><div class="sr-sub">${Math.round((audio.volume||0)*100)}% — сохраняется автоматически</div></div>
                <div class="sr-right">
                    <input type="range" id="volInline" class="settings-inline-range" min="0" max="100" value="${Math.round((audio.volume||0)*100)}">
                </div>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Кроссфейд между треками</div><div class="sr-sub">Плавный переход в секундах</div></div>
                <div class="sr-right">
                    <input type="range" id="crossfadeInline" class="settings-inline-range" min="0" max="12" value="${crossfade}">
                    <span id="crossfadeVal">${crossfade}s</span>
                </div>
            </div>

            <div class="settings-section-title">Воспроизведение</div>
            <div class="settings-row">
                <div><div class="sr-name">Автоплей похожих</div><div class="sr-sub">Продолжать воспроизведение после очереди</div></div>
                <label class="toggle"><input type="checkbox" id="autoplayToggle" ${autoplay?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Авто-скачивание лайков</div><div class="sr-sub">При лайке трек сразу сохраняется в офлайн (фоном)</div></div>
                <label class="toggle"><input type="checkbox" id="autoDlLikesToggle" ${autoDlLikes?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Авто-текст треков</div><div class="sr-sub">Автоматически показывать текст в полноэкранном плеере при смене трека</div></div>
                <label class="toggle"><input type="checkbox" id="autoLyricsToggle" ${autoLyrics?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Текст следует за музыкой</div><div class="sr-sub">Авто-скролл караоке к текущей строке</div></div>
                <label class="toggle"><input type="checkbox" id="lyricsFollowToggle" ${lyricsFollow?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row" id="srSources">
                <div><div class="sr-name">Источники</div><div class="sr-sub">Откуда берём треки</div></div>
                <div class="sr-right"><span>${sources.join(", ")||"—"}</span><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>

            <div class="settings-section-title">Контент</div>
            <div class="settings-row" id="srImport" ${!state.me?'class="settings-row is-disabled"':""}>
                <div><div class="sr-name">Импорт медиатеки</div><div class="sr-sub">Загрузите .txt / .docx со списком треков (формат: «Артист — Название» построчно)</div></div>
                <div class="sr-right"><svg class="ic"><use href="#i-import"/></svg><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            <div class="settings-row" id="srKids">
                <div><div class="sr-name">Режим для детей</div><div class="sr-sub">Скрывает и не воспроизводит 18+ контент</div></div>
                <label class="toggle"><input type="checkbox" id="kidsToggle" ${me.kids_mode?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row" id="srDislikes">
                <div><div class="sr-name">Дизлайки</div><div class="sr-sub">Треки и артисты, которые не появятся в Моей волне</div></div>
                <div class="sr-right"><span>${(state.dislikedTrackKeys && state.dislikedTrackKeys.size) || 0}</span><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            <div class="settings-row" id="srArtistPrefs">
                <div><div class="sr-name">Предпочтения артистов</div><div class="sr-sub">Лайки и дизлайки исполнителей · влияет на Мою волну</div></div>
                <div class="sr-right"><svg class="ic"><use href="#i-mic"/></svg><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            <div class="settings-row" id="srOffline">
                <div><div class="sr-name">Офлайн режим</div><div class="sr-sub">Скачанные треки и их прослушивание без интернета</div></div>
                <div class="sr-right"><span>${state.offline.downloaded.size} ${state.offline.enabled?'· вкл':''}</span><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>

            <div class="settings-section-title">Интерфейс</div>
            <div class="settings-row">
                <div><div class="sr-name">Уменьшить анимации</div><div class="sr-sub">Отключает фоновые анимации и переходы</div></div>
                <label class="toggle"><input type="checkbox" id="reduceMotionToggle" ${reduceMotion?"checked":""}><span class="toggle-slider"></span></label>
            </div>
            <div class="settings-row">
                <div><div class="sr-name">Системные уведомления</div><div class="sr-sub">Показывать инфо о текущем треке (Media Session)</div></div>
                <label class="toggle"><input type="checkbox" id="notifsToggle" ${notifs?"checked":""}><span class="toggle-slider"></span></label>
            </div>

            <div class="settings-section-title">Аккаунт</div>
            <div class="settings-row" id="srProfile">
                <div><div class="sr-name">Профиль</div><div class="sr-sub">${me.username ? "Вы вошли как "+(me.display_name||me.username) : "Не выполнен вход"}</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            ${state.me ? `<div class="settings-row" id="srFollows">
                <div><div class="sr-name">Подписки</div><div class="sr-sub">Пользователи, на которых вы подписаны</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>
            <div class="settings-row" id="srPrivacy">
                <div><div class="sr-name">Приватность профиля</div><div class="sr-sub">${me.is_private?'🔒 Профиль приватный':'🌐 Профиль публичный'} · что видят другие</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>` : ""}
            <div class="settings-row" id="srInstall">
                <div><div class="sr-name">Установить как приложение</div><div class="sr-sub">Добавить Velora на рабочий стол / Home Screen</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-download"/></svg></div>
            </div>
            <div class="settings-row" id="srClearCache">
                <div><div class="sr-name">Очистить кэш</div><div class="sr-sub">Удалить локальные настройки и временные данные (без выхода)</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-trash"/></svg></div>
            </div>
            ${state.me ? `<div class="settings-row" id="srSessions">
                <div><div class="sr-name">Активные сессии</div><div class="sr-sub">Устройства и браузеры, где вы вошли · можно завершить чужие</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>` : ""}
            ${state.me ? `<div class="settings-row danger" id="srLogout">
                <div><div class="sr-name">Выйти из аккаунта</div><div class="sr-sub">Текущая сессия будет завершена</div></div>
                <div class="sr-right"><svg class="ic chev"><use href="#i-chev-right"/></svg></div>
            </div>` : ""}
            <div class="settings-row">
                <div><div class="sr-name">Velora Sound</div><div class="sr-sub">Веб-плеер · версия 1.0 · vanilla JS</div></div>
            </div>
        </div>`;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) return navigate(g.dataset.go);
    };
    $("#srEq").onclick = openEq;
    $("#srSources").onclick = () => showToast("Переключайте источники в строке поиска сверху.");
    $("#srImport").onclick = () => { if (!state.me) return openAuth(); $("#filePicker").click(); };
    $("#srProfile").onclick = () => state.me ? navigate("profile") : openAuth();
    $("#srDislikes").onclick = () => state.me ? navigate("dislikes") : openAuth();
    $("#srArtistPrefs") && ($("#srArtistPrefs").onclick = () => state.me ? navigate("artistPrefs") : openAuth());
    $("#srOffline").onclick = () => navigate("offline");
    $("#srFollows") && ($("#srFollows").onclick = () => navigate("follows"));
    $("#srPrivacy") && ($("#srPrivacy").onclick = () => { navigate("profile"); setTimeout(openPrivacyEditor, 50); });
    $("#srInstall").onclick = () => triggerInstall();
    $("#srClearCache").onclick = () => {
        if (!confirm("Удалить локальные настройки (громкость, EQ, качество, история волны)?\nАккаунт и плейлисты НЕ затрагиваются.")) return;
        const keep = ["velora_pwa_prompt"];
        Object.keys(localStorage).filter(k=>k.startsWith("velora_") && !keep.includes(k)).forEach(k=>localStorage.removeItem(k));
        showToast("Кэш очищен"); setTimeout(()=>location.reload(), 600);
    };
    $("#srLogout") && ($("#srLogout").onclick = async () => {
        if (!confirm("Выйти из аккаунта?")) return;
        try { await api("/api/auth/logout", { method: "POST" }); } catch {}
        state.me = null; state.likedKeys.clear(); state.playlists = [];
        renderUserPill(); renderSidebarPlaylists(); applyGuestUi(); navigate("home");
    });
    $("#srSessions") && ($("#srSessions").onclick = openSessionsModal);
    $("#qualityInline").onchange = (e) => {
        const q = e.target.value;
        try { localStorage.setItem("velora_quality", q); } catch {}
        const sel = $("#qualitySel"); if (sel) sel.value = q;
        showToast("Качество: " + ({low:"Низкое",std:"Оптимальное",high:"Высокое",max:"Максимальное"})[q]);
    };
    $("#volInline").addEventListener("input", (e) => {
        const pct = Number(e.target.value);
        audio.volume = pct/100;
        // EQ отключён — gainNode отсутствует, ничего дополнительно не нужно дёргать.
        setVolumeUi(pct);
    });
    $("#crossfadeInline").addEventListener("input", (e) => {
        const v = Number(e.target.value);
        $("#crossfadeVal").textContent = v + "s";
        try { localStorage.setItem("velora_crossfade", String(v)); } catch {}
    });
    $("#autoplayToggle").onchange = (e) => {
        try { localStorage.setItem("velora_autoplay", e.target.checked ? "1" : "0"); } catch {}
    };
    $("#autoDlLikesToggle") && ($("#autoDlLikesToggle").onchange = (e) => {
        try { localStorage.setItem("velora_auto_dl_likes", e.target.checked ? "1" : "0"); } catch {}
        showToast(e.target.checked ? "Лайкнутые треки будут скачиваться автоматически" : "Авто-скачивание выключено");
    });
    $("#autoLyricsToggle") && ($("#autoLyricsToggle").onchange = (e) => {
        try { localStorage.setItem("velora_auto_lyrics", e.target.checked ? "1" : "0"); } catch {}
        const fp = $("#fullplayer");
        if (fp) fp.classList.toggle("lyrics-hidden", !e.target.checked);
        const tool = $("#fpToolLyrics"); if (tool) tool.classList.toggle("active", e.target.checked);
        const tgl = $("#fpLyricsToggle"); if (tgl) tgl.classList.toggle("active", e.target.checked);
        showToast(e.target.checked ? "Текст будет показываться автоматически" : "Текст скрыт до ручного открытия");
    });
    $("#lyricsFollowToggle").onchange = (e) => {
        try { localStorage.setItem("velora_lyrics_follow", e.target.checked ? "1" : "0"); } catch {}
        state.karaokeFollow = e.target.checked;
    };
    $("#reduceMotionToggle").onchange = (e) => {
        try { localStorage.setItem("velora_reduce_motion", e.target.checked ? "1" : "0"); } catch {}
        document.body.classList.toggle("reduce-motion", e.target.checked);
    };
    $("#notifsToggle").onchange = (e) => {
        try { localStorage.setItem("velora_notifs", e.target.checked ? "1" : "0"); } catch {}
    };
    $("#kidsToggle").onchange = async (e) => {
        if (!state.me) { e.target.checked = false; return openAuth(); }
        try {
            await api("/api/profile", { method: "POST", body: { kids_mode: e.target.checked } });
            state.me.kids_mode = e.target.checked;
            showToast(e.target.checked ? "Детский режим включён" : "Детский режим выключен");
        } catch (err) { showToast(err.message); }
    };
}

// ============== ПРОФИЛЬ ==============
function _fmtDate(iso) {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        if (isNaN(+d)) return "—";
        return d.toLocaleDateString("ru-RU", { day:"numeric", month:"long", year:"numeric" });
    } catch { return "—"; }
}
function _fmtDob(isoDate) {
    // Форматирует дату рождения "YYYY-MM-DD" в "DD месяца YYYY".
    if (!isoDate) return "";
    try {
        const d = new Date(String(isoDate).slice(0, 10) + "T00:00:00");
        if (isNaN(+d)) return String(isoDate);
        return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
    } catch { return String(isoDate); }
}
function _daysSince(iso) {
    if (!iso) return 0;
    try {
        const d = new Date(iso);
        return Math.max(0, Math.floor((Date.now() - +d) / 86400000));
    } catch { return 0; }
}
async function renderProfilePage() {
    if (!state.me) return openAuth();
    const v = $("#view");
    const me = state.me;
    const dn = me.display_name || me.username;
    // Подтянем счётчики истории и подписок (best-effort)
    let historyCount = 0;
    try { historyCount = (asTracks(await apiCached("/api/history?limit=500"))||[]).length; } catch {}
    let followingCount = 0, followersCount = 0;
    try {
        const [f1, f2] = await Promise.all([
            api("/api/me/follows?kind=following").catch(()=>[]),
            api("/api/me/follows?kind=followers").catch(()=>[]),
        ]);
        followingCount = (f1||[]).length;
        followersCount = (f2||[]).length;
    } catch {}
    const dlCount = state.offline.downloaded.size;
    const likeCount = state.likedKeys.size;
    const dislikeCount = (state.dislikedTrackKeys?.size||0) + (state.dislikedArtistIds?.size||0);
    const days = _daysSince(me.created_at);
    const seed = (me.slug||me.username||String(me.id||"x")).toLowerCase();
    const bannerStyle = me.banner
        ? `background-image:${_cssUrl(me.banner)};background-size:cover;background-position:center`
        : `background:${gradientFromSeed("banner-"+seed)}`;
    const avStyle = me.avatar
        ? `background-image:${_cssUrl(me.avatar)};background-size:cover;background-position:center`
        : `background:${gradientFromSeed(seed)}`;
    v.innerHTML = `
        <div class="profile-page is-self">
            <div class="profile-cover" id="pCover" style="${bannerStyle}">
                <button class="cover-edit" id="pCoverEdit" title="Изменить обложку" aria-label="Изменить обложку">
                    <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M12 20h9"/>
                        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
                    </svg>
                </button>
            </div>
            <div class="profile-card">
                <div class="pavatar" id="pAvatar" style="${avStyle}">${me.avatar?"":escapeHtml(dn.charAt(0).toUpperCase())}</div>
                <div class="pinfo">
                    <h1>${escapeHtml(dn)}</h1>
                    <div class="phandle">@${escapeHtml(me.username||"")}${me.is_private?' · <span class="badge-private">🔒 приватный</span>':''}${me.kids_mode?' · <span class="badge-kids">Детский режим</span>':''}</div>
                    <div class="pbio">${escapeHtml(me.bio || "Нет описания. Расскажите о себе — нажмите «Редактировать».")}</div>
                    <div class="pmeta-row">
                        ${me.location ? `<span class="pchip">📍 ${escapeHtml(me.location)}</span>` : ""}
                        ${me.website ? `<a class="pchip" href="${escapeHtml(me.website)}" target="_blank" rel="noopener">🔗 ${escapeHtml(me.website.replace(/^https?:\/\//,''))}</a>` : ""}
                        ${me.dob ? `<span class="pchip" title="${escapeHtml(me.dob)}">🎂 ${escapeHtml(_fmtDob(me.dob))}</span>` : ""}
                        <span class="pchip">📅 С нами ${days} ${days===1?'день':(days%10>=2&&days%10<=4&&(days%100<10||days%100>=20)?'дня':'дней')}</span>
                    </div>
                </div>
                <div class="profile-actions">
                    <button class="btn-secondary" id="pEdit">Редактировать</button>
                    <button class="btn-secondary" id="pPrivacy">Приватность</button>
                    <button class="btn-secondary" id="pShare" title="Скопировать ссылку"><svg class="ic"><use href="#i-share"/></svg></button>
                </div>
            </div>
            <div class="profile-stats">
                <div class="profile-stat clickable" data-go-stat="favorites"><div class="num">${likeCount}</div><div class="lbl">Любимые</div></div>
                <div class="profile-stat clickable" data-go-stat="library"><div class="num">${state.playlists.length}</div><div class="lbl">Плейлисты</div></div>
                <div class="profile-stat"><div class="num">${historyCount}</div><div class="lbl">Прослушано</div></div>
                <div class="profile-stat clickable" data-go-stat="follows"><div class="num">${followersCount}</div><div class="lbl">Подписчики</div></div>
                <div class="profile-stat clickable" data-go-stat="follows"><div class="num">${followingCount}</div><div class="lbl">Подписки</div></div>
                <div class="profile-stat"><div class="num">${dlCount}</div><div class="lbl">Скачано</div></div>
                <div class="profile-stat"><div class="num">${dislikeCount}</div><div class="lbl">Дизлайки</div></div>
            </div>
            <div class="profile-info-grid">
                <div class="pinfo-card">
                    <h3>Аккаунт</h3>
                    <div class="pinfo-row"><span>Имя пользователя</span><b>@${escapeHtml(me.username||"—")}</b></div>
                    <div class="pinfo-row"><span>Отображаемое имя</span><b>${escapeHtml(me.display_name||me.username||"—")}</b></div>
                    <div class="pinfo-row"><span>ID</span><b class="mono">${me.id||'—'}</b></div>
                    <div class="pinfo-row"><span>Регистрация</span><b>${_fmtDate(me.created_at)}</b></div>
                    <div class="pinfo-row"><span>Видимость профиля</span><b>${me.is_private?'🔒 Приватный':'🌐 Публичный'}</b></div>
                </div>
                <div class="pinfo-card">
                    <h3>Контакты <small class="muted">(только вы видите)</small></h3>
                    <div class="pinfo-row"><span>Email</span><b>${me.email?escapeHtml(me.email)+(me.email_verified?' <span class="ok">✓</span>':' <span class="warn">не подтверждён</span>'):'—'}</b></div>
                    <div class="pinfo-row"><span>Телефон</span><b>${me.phone?escapeHtml(me.phone)+(me.phone_verified?' <span class="ok">✓</span>':''):'—'}</b></div>
                </div>
                <div class="pinfo-card">
                    <h3>Активность</h3>
                    <div class="pinfo-row"><span>Любимые треки</span><b>${likeCount}</b></div>
                    <div class="pinfo-row"><span>Плейлисты</span><b>${state.playlists.length}</b></div>
                    <div class="pinfo-row"><span>В офлайн</span><b>${dlCount}</b></div>
                    <div class="pinfo-row"><span>Подписки</span><b>${followingCount}</b></div>
                    <div class="pinfo-row"><span>Подписчики</span><b>${followersCount}</b></div>
                </div>
            </div>
            <div id="wallMount"></div>
        </div>`;
    $("#pAvatar").onclick = () => {
        if (state.me.avatar) openImageLightbox(state.me.avatar, { editable: true, kind: "avatar" });
        else pickImage("avatar");
    };
    $("#pCoverEdit").onclick = (e) => { e.stopPropagation(); pickImage("banner"); };
    $("#pCover").onclick = (e) => {
        // Не перехватываем клик по кнопке-карандашу.
        if (e.target.closest("#pCoverEdit")) return;
        if (state.me.banner) openImageLightbox(state.me.banner, { editable: true, kind: "banner" });
        else pickImage("banner");
    };
    $("#pEdit").onclick = openEditProfile;
    $("#pPrivacy").onclick = openPrivacyEditor;
    $("#pShare").onclick = async () => {
        const url = `${location.origin}/u/${me.slug||me.username||me.id}`;
        try { await navigator.clipboard.writeText(url); showToast("Ссылка на профиль скопирована"); }
        catch { showToast(url); }
    };
    v.querySelectorAll("[data-go-stat]").forEach(el => {
        el.onclick = () => navigate(el.dataset.goStat);
    });
    renderWall(me.slug || me.username, $("#wallMount"));
}

function openPrivacyEditor() {
    const me = state.me;
    const priv = me.privacy || {};
    const def = (k, fallback=true) => priv[k] !== undefined ? !!priv[k] : fallback;
    $("#editTitle").textContent = "Настройки приватности профиля";
    $("#editError").hidden = true;
    $("#editBody").innerHTML = `
        <label class="ed-toggle ed-toggle-strong">
            <input type="checkbox" id="prPrivate" ${me.is_private?"checked":""}>
            <span><b>Приватный профиль 🔒</b><br><small>Посторонние видят только @никнейм. Ни описание, ни фото, ни плейлисты.</small></span>
        </label>
        <div class="privacy-section">
            <div class="ps-title">Что показывать на публичной странице</div>
            <small class="muted">Email, телефон, способы входа и настройки воспроизведения <b>никогда</b> не показываются другим пользователям.</small>
            <label class="ed-toggle"><input type="checkbox" id="prAvatar" ${def("show_avatar")?"checked":""}><span>Аватар</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prBanner" ${def("show_banner")?"checked":""}><span>Обложка профиля</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prBio" ${def("show_bio")?"checked":""}><span>Описание (bio)</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prLocation" ${def("show_location")?"checked":""}><span>Город</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prWebsite" ${def("show_website")?"checked":""}><span>Сайт</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prStats" ${def("show_stats")?"checked":""}><span>Статистику (любимые, плейлисты)</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prPlaylists" ${def("show_playlists")?"checked":""}><span>Публичные плейлисты</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prDob" ${def("show_dob", false)?"checked":""}><span>Дату рождения</span></label>
            <label class="ed-toggle"><input type="checkbox" id="prWall" ${def("show_wall")?"checked":""}><span>Стену профиля посторонним</span></label>
        </div>
        <div class="privacy-section">
            <div class="ps-title">Кто может писать на стене</div>
            <label class="ed-toggle ed-toggle-strong">
                <input type="checkbox" id="prWallEnabled" ${me.wall_enabled!==false?"checked":""}>
                <span><b>Принимать записи от других пользователей</b><br><small>Если выключено — писать на стене сможете только вы.</small></span>
            </label>
        </div>
    `;
    $("#editModal").hidden = false;
    $("#editSave").onclick = async () => {
        const body = {
            is_private: $("#prPrivate").checked,
            wall_enabled: $("#prWallEnabled").checked,
            privacy: {
                show_avatar: $("#prAvatar").checked,
                show_banner: $("#prBanner").checked,
                show_bio: $("#prBio").checked,
                show_location: $("#prLocation").checked,
                show_website: $("#prWebsite").checked,
                show_stats: $("#prStats").checked,
                show_playlists: $("#prPlaylists").checked,
                show_dob: $("#prDob").checked,
                show_wall: $("#prWall").checked,
            },
        };
        try {
            await api("/api/profile", { method: "POST", body });
            state.me.is_private = body.is_private;
            state.me.wall_enabled = body.wall_enabled;
            state.me.privacy = body.privacy;
            $("#editModal").hidden = true;
            renderProfilePage();
            showToast("Настройки приватности сохранены");
        } catch (e) { $("#editError").textContent = e.message; $("#editError").hidden = false; }
    };
}

function openEditProfile() {
    const me = state.me;
    $("#editTitle").textContent = "Редактировать профиль";
    $("#editError").hidden = true;
    const dobVal = me.dob ? String(me.dob).slice(0, 10) : "";
    const locked = !!me.kids_mode_locked;
    $("#editBody").innerHTML = `
        <label class="ed-field">
            <span>Имя пользователя <small>(@ник)</small></span>
            <input type="text" id="edUsername" maxlength="32" autocomplete="username"
                   spellcheck="false" value="${escapeHtml(me.username||"")}">
            <small class="muted" id="edUsernameHint">3–32 символа: латиница, цифры, _ . -</small>
        </label>
        <label class="ed-field">
            <span>Отображаемое имя</span>
            <input type="text" id="edDisplay" maxlength="60" placeholder="Ваше имя"
                   value="${escapeHtml(me.display_name||"")}">
        </label>
        <label class="ed-field">
            <span>О себе</span>
            <textarea id="edBio" rows="4" maxlength="500" placeholder="Несколько слов о вас">${escapeHtml(me.bio||"")}</textarea>
        </label>
        <label class="ed-field">
            <span>Город</span>
            <input type="text" id="edLocation" maxlength="120" value="${escapeHtml(me.location||"")}">
        </label>
        <label class="ed-field">
            <span>Сайт</span>
            <input type="url" id="edWebsite" maxlength="255" placeholder="https://…" value="${escapeHtml(me.website||"")}">
        </label>
        <label class="ed-field">
            <span>Дата рождения</span>
            <input type="date" id="edDob" value="${escapeHtml(dobVal)}">
            <small class="muted">${locked
                ? "🔒 Возраст < 18 — детский режим включён принудительно и не отключится до 18 лет."
                : "Если меньше 18 — детский режим включится автоматически и не отключится до совершеннолетия."}</small>
        </label>
    `;
    $("#editModal").hidden = false;
    // Живая проверка доступности ника.
    attachUsernameLiveCheck($("#edUsername"), $("#edUsernameHint"), me.username || "");
    $("#editSave").onclick = async () => {
        const body = {
            username: $("#edUsername").value.trim(),
            display_name: $("#edDisplay").value.trim(),
            bio: $("#edBio").value.trim(),
            location: $("#edLocation").value.trim(),
            website: $("#edWebsite").value.trim(),
            dob: $("#edDob").value.trim(),
        };
        try {
            await api("/api/profile", { method: "POST", body });
            await loadMe();
            $("#editModal").hidden = true;
            renderUserPill(); renderProfilePage();
            showToast("Профиль обновлён");
        } catch (e) {
            $("#editError").textContent = e.message || "Не удалось сохранить";
            $("#editError").hidden = false;
        }
    };
}
function openEditPlaylist(pid, p) {
    $("#editTitle").textContent = "Редактировать плейлист";
    $("#editError").hidden = true;
    const isPub = !!p.is_public;
    const slug = p.slug || pid;
    const shareUrl = `${location.origin}/p/${slug}`;
    $("#editBody").innerHTML = `
        <input type="text" id="edName" placeholder="Название" value="${escapeHtml(p.name||"")}">
        <textarea id="edDesc" placeholder="Описание" rows="3">${escapeHtml(p.description||"")}</textarea>
        <label class="ed-toggle">
            <input type="checkbox" id="edPub" ${isPub?"checked":""}>
            <span><b>Публичный плейлист</b><br><small>Виден по ссылке всем. Снимите галочку, чтобы плейлист был доступен только вам.</small></span>
        </label>
        <div class="ed-share">
            <label>Ссылка на плейлист</label>
            <div class="ed-share-row">
                <input type="text" id="edShare" readonly value="${escapeHtml(shareUrl)}">
                <button type="button" class="btn-secondary" id="edShareCopy">Копировать</button>
            </div>
            <small id="edShareHint">${isPub ? "Скопируйте ссылку и поделитесь с друзьями." : "Включите «Публичный», чтобы ссылка работала для других."}</small>
        </div>
    `;
    $("#editModal").hidden = false;
    $("#edShareCopy").onclick = async () => {
        try { await navigator.clipboard.writeText(shareUrl); showToast("Ссылка скопирована"); }
        catch { $("#edShare").select(); document.execCommand("copy"); showToast("Ссылка скопирована"); }
    };
    $("#edPub").onchange = (e) => {
        $("#edShareHint").textContent = e.target.checked
            ? "Скопируйте ссылку и поделитесь с друзьями."
            : "Включите «Публичный», чтобы ссылка работала для других.";
    };
    $("#editSave").onclick = async () => {
        try {
            await api(`/api/playlists/${pid}`, { method: "PATCH", body: {
                name: $("#edName").value.trim(),
                description: $("#edDesc").value.trim(),
                is_public: $("#edPub").checked,
            }});
            $("#editModal").hidden = true;
            await loadPlaylists(); renderPlaylistPage(pid);
            showToast("Сохранено");
        } catch (e) { $("#editError").textContent = e.message; $("#editError").hidden = false; }
    };
}
function editPlaylistField(pid, field, current, label) {
    const val = prompt(label, current); if (val == null) return;
    api(`/api/playlists/${pid}`, { method: "PATCH", body: { [field]: val } })
        .then(() => { loadPlaylists().then(() => renderPlaylistPage(pid)); })
        .catch(e => showToast(e.message));
}
function editPlaylistCover(pid) { state.pendingImage = { target: "playlist_cover", pid }; $("#imgPicker").click(); }

// Лайтбокс для аватара/баннера: показывает картинку на весь экран,
// для своего профиля — ещё кнопка «Изменить».
function openImageLightbox(src, opts = {}) {
    if (!src) return;
    const editable = !!opts.editable;
    const kind = opts.kind || "image";
    const dlg = document.createElement("div");
    dlg.className = "img-lightbox";
    dlg.innerHTML = `
        <button class="img-lb-close" aria-label="Закрыть">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>
        </button>
        ${editable ? `<button class="img-lb-edit" aria-label="Изменить">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
            Изменить
        </button>`:""}
        <img class="img-lb-pic" src="${escapeHtml(src)}" alt="">
    `;
    const close = () => { try { dlg.remove(); } catch {} document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    dlg.addEventListener("click", (e) => {
        if (e.target.closest(".img-lb-close")) return close();
        if (e.target.closest(".img-lb-edit")) {
            close();
            return pickImage(kind === "banner" ? "banner" : "avatar");
        }
        if (e.target === dlg) close();
    });
    document.body.appendChild(dlg);
}

// Стена профиля: подгружает посты + форма для авторизованных.
// Поддерживает: жирный/курсив/подчёркивание (markdown-подмножество),
// прикрепление картинок/GIF, выбор TTL (1ч…30д), бейдж "удалится через X".
const _WALL_TTL_LABELS = {
    1: "1 час", 6: "6 часов", 24: "1 день",
    72: "3 дня", 168: "7 дней", 720: "30 дней",
};

function _renderWallText(raw) {
    // Сначала экранируем, потом подставляем разрешённый markdown.
    let s = escapeHtml(raw || "");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/\*([^*\n]+)\*/g, "<i>$1</i>");
    s = s.replace(/__([^_\n]+)__/g, "<u>$1</u>");
    s = s.replace(/\n/g, "<br>");
    return s;
}

function _fmtTtlBadge(expiresIso) {
    if (!expiresIso) return "";
    const ms = new Date(expiresIso).getTime() - Date.now();
    if (ms <= 0) return "истекает";
    const m = Math.floor(ms / 60000);
    if (m < 60) return `удалится через ${m} мин.`;
    const h = Math.floor(m / 60);
    if (h < 24) return `удалится через ${h} ч.`;
    const d = Math.floor(h / 24);
    return `удалится через ${d} д.`;
}

// Готовит файл к загрузке на стену.
// Поддерживаем все форматы изображений и анимаций, кроме видео.
// Анимированные (GIF, APNG, animated-WebP, AVIF) отдаём как есть, чтобы
// сохранить анимацию. Статичные пропускаем через canvas → JPEG/PNG до 1600 px:
// это решает HEIC с iPhone, гигантские TIFF и т. п.
function _readArrayBuffer(file) {
    return new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(new Error("Не удалось прочитать файл"));
        fr.readAsArrayBuffer(file);
    });
}
function _readDataUrl(file) {
    return new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result || ""));
        fr.onerror = () => rej(new Error("Не удалось прочитать файл"));
        fr.readAsDataURL(file);
    });
}
function _isAnimatedBuffer(buf, mime) {
    try {
        const u = new Uint8Array(buf);
        const m = (mime || "").toLowerCase();
        // GIF — формат всегда «потенциально анимирован», шлём как есть.
        if (m === "image/gif") return true;
        // APNG: ищем чанк acTL до IDAT в первых ~64 КБ.
        if (m === "image/png" || m === "image/apng") {
            const limit = Math.min(u.length - 4, 65536);
            for (let i = 8; i < limit; i++) {
                if (u[i] === 0x61 && u[i+1] === 0x63 && u[i+2] === 0x54 && u[i+3] === 0x4c) return true;       // acTL
                if (u[i] === 0x49 && u[i+1] === 0x44 && u[i+2] === 0x41 && u[i+3] === 0x54) break;             // IDAT
            }
            return m === "image/apng";
        }
        // WebP: RIFF...WEBP, ANIM-чанк означает анимацию.
        if (m === "image/webp") {
            const limit = Math.min(u.length - 4, 65536);
            for (let i = 12; i < limit; i++) {
                if (u[i] === 0x41 && u[i+1] === 0x4e && u[i+2] === 0x49 && u[i+3] === 0x4d) return true;       // ANIM
            }
            return false;
        }
        // AVIF: оставляем без перекодирования (canvas теряет анимацию AVIF).
        if (m === "image/avif") return true;
        return false;
    } catch (_) { return false; }
}
async function _prepareWallImage(file) {
    if (!file || !file.type || !file.type.startsWith("image/")) {
        throw new Error("Это не изображение");
    }
    const TARGET = 10 * 1024 * 1024;     // ≤ 10 МБ — целевой размер.
    const HARD_MAX = 25 * 1024 * 1024;   // абсолютный лимит сервера.
    // Анимированные форматы — без пере-кодирования.
    let buf = null;
    try { buf = await _readArrayBuffer(file); } catch (_) {}
    const animated = buf ? _isAnimatedBuffer(buf, file.type) : (file.type === "image/gif");
    if (animated) {
        if (file.size > HARD_MAX) {
            throw new Error("Анимация больше 25 МБ — браузер не может её безопасно ужать без потери кадров. Выберите GIF/WebP поменьше.");
        }
        if (buf) {
            const u8 = new Uint8Array(buf);
            let bin = "";
            const chunk = 0x8000;
            for (let i = 0; i < u8.length; i += chunk) {
                bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
            }
            return `data:${file.type};base64,${btoa(bin)}`;
        }
        return await _readDataUrl(file);
    }
    // Статичные: рисуем на canvas. Итеративно ужимаем размер/качество, пока
    // не уложимся в TARGET. Стартуем с 1800 px по большей стороне.
    return await new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
            try {
                const isPng = file.type === "image/png";
                let maxSide = 1800;
                let q = 0.92;
                let dataUrl = "";
                let attempts = 0;
                const w0 = img.naturalWidth, h0 = img.naturalHeight;
                if (!w0 || !h0) { URL.revokeObjectURL(url); reject(new Error("Битое изображение")); return; }
                const c = document.createElement("canvas");
                const ctx = c.getContext("2d");
                while (attempts < 8) {
                    const ratio = Math.min(1, maxSide / Math.max(w0, h0));
                    c.width = Math.max(1, Math.round(w0 * ratio));
                    c.height = Math.max(1, Math.round(h0 * ratio));
                    ctx.clearRect(0, 0, c.width, c.height);
                    ctx.drawImage(img, 0, 0, c.width, c.height);
                    dataUrl = isPng ? c.toDataURL("image/png") : c.toDataURL("image/jpeg", q);
                    // Грубо: размер data:URL ≈ base64 → байты * 3/4.
                    const approxBytes = Math.floor((dataUrl.length - dataUrl.indexOf(",") - 1) * 3 / 4);
                    if (approxBytes <= TARGET) break;
                    attempts++;
                    if (isPng) {
                        // PNG: только уменьшаем размер.
                        maxSide = Math.max(480, Math.round(maxSide * 0.8));
                    } else {
                        // JPEG: сначала качество, потом размеры.
                        if (q > 0.6) q = Math.max(0.6, q - 0.08);
                        else maxSide = Math.max(480, Math.round(maxSide * 0.8));
                    }
                }
                URL.revokeObjectURL(url);
                resolve(dataUrl);
            } catch (_) {
                URL.revokeObjectURL(url);
                reject(new Error("Не удалось обработать изображение"));
            }
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error("Этот формат не поддерживается браузером. Попробуйте JPEG/PNG/WebP/GIF."));
        };
        img.src = url;
    });
}

async function renderWall(slug, mount) {
    if (!mount) return;
    mount.innerHTML = `<div class="wall-block"><h2 class="section-title">Стена</h2><div class="hint">Загружаем…</div></div>`;
    let data;
    try {
        data = await fetch(`/api/u/${encodeURIComponent(slug)}/wall?limit=50`).then(r => r.json());
    } catch {
        mount.innerHTML = "";
        return;
    }
    if (!data || data.error) { mount.innerHTML = ""; return; }
    if (data.hidden) {
        // Стена скрыта владельцем для посторонних.
        mount.innerHTML = `<div class="wall-block"><h2 class="section-title">Стена</h2><div class="hint muted">Стена скрыта владельцем.</div></div>`;
        return;
    }
    const me = state.me;
    const canPost = !!data.can_post;
    const wallOff = !data.wall_enabled;
    const ttlChoices = (data.ttl_choices && data.ttl_choices.length) ? data.ttl_choices : [1, 6, 24, 72, 168, 720];
    const ttlDefault = data.ttl_default || 168;
    const ttlOptionsHtml = ttlChoices.map(h =>
        `<option value="${h}"${h === ttlDefault ? " selected" : ""}>${_WALL_TTL_LABELS[h] || (h + " ч.")}</option>`
    ).join("");
    const formHtml = me
        ? (canPost
            ? `<form class="wall-form" id="wallForm">
                 <textarea id="wallText" rows="3" maxlength="2000"
                           placeholder="Что у вас нового?  **жирный**,  *курсив*,  __подчёркнуто__"></textarea>
                 <div id="wallAttachPreview" class="wall-attach-preview" hidden>
                     <img id="wallAttachImg" alt="">
                     <div id="wallAttachOverlay" class="wall-attach-overlay" hidden>
                         <div class="wall-attach-spinner" aria-hidden="true"></div>
                         <span>Обработка…</span>
                     </div>
                     <button type="button" id="wallAttachClear" class="wall-attach-clear" title="Убрать">✕</button>
                 </div>
                 <div class="wall-form-row">
                     <div class="wall-toolbar" role="toolbar" aria-label="Форматирование">
                         <button type="button" class="wall-tb-btn" data-fmt="bold" title="Жирный (Ctrl+B)">B</button>
                         <button type="button" class="wall-tb-btn" data-fmt="italic" title="Курсив (Ctrl+I)">I</button>
                         <button type="button" class="wall-tb-btn" data-fmt="underline" title="Подчёркивание (Ctrl+U)">U</button>
                         <span class="wall-tb-sep"></span>
                         <button type="button" class="wall-tb-btn" id="wallAttachBtn" title="Прикрепить картинку или GIF" aria-label="Прикрепить">
                             <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                         </button>
                         <input type="file" id="wallAttachInput" accept="image/*" hidden>
                     </div>
                     <div class="wall-form-actions">
                         <small id="wallCounter" class="muted">0 / 2000</small>
                         <label class="wall-ttl-wrap" title="Через сколько пост удалится">
                             <span aria-hidden="true">⏱</span>
                             <select id="wallTtl" class="wall-ttl-select">${ttlOptionsHtml}</select>
                         </label>
                         <button type="submit" class="btn-primary">Опубликовать</button>
                     </div>
                 </div>
               </form>`
            : (wallOff
                ? `<div class="hint muted">Владелец отключил приём записей на стене.</div>`
                : `<div class="hint muted">Здесь нельзя писать (приватный профиль).</div>`))
        : `<div class="hint"><button class="btn-link" id="wallLogin">Войдите</button>, чтобы оставить запись.</div>`;
    const posts = data.posts || [];
    const postsHtml = posts.length
        ? posts.map(p => {
            const pSlug = escapeHtml(p.author.slug || p.author.username || "");
            const pName = escapeHtml(p.author.display_name || p.author.username || "?");
            const pAvBg = p.author.avatar
                ? `background-image:${_cssUrl(p.author.avatar)};background-size:cover;background-position:center`
                : `background:${gradientFromSeed(p.author.username || "x")}`;
            const pInitial = escapeHtml((p.author.display_name || p.author.username || "?").charAt(0).toUpperCase());
            return `
            <div class="wall-post" data-pid="${p.id}">
                <a class="wall-av" href="/u/${pSlug}" data-go-user="${pSlug}" style="${pAvBg}">${p.author.avatar ? "" : pInitial}</a>
                <div class="wall-body">
                    <div class="wall-head">
                        <a class="wall-name" href="/u/${pSlug}" data-go-user="${pSlug}">${pName}</a>
                        <span class="wall-time muted">${_fmtDate(p.created_at)}</span>
                        ${p.expires_at ? `<span class="wall-ttl-badge muted" title="${escapeHtml(p.expires_at)}">${_fmtTtlBadge(p.expires_at)}</span>` : ""}
                        ${p.can_delete ? `<button class="wall-del" data-del="${p.id}" title="Удалить">✕</button>` : ""}
                    </div>
                    ${p.text ? `<div class="wall-text">${_renderWallText(p.text)}</div>` : ""}
                    ${p.image_url ? `<a class="wall-post-image" href="${escapeHtml(p.image_url)}" data-img="${escapeHtml(p.image_url)}"><img loading="lazy" src="${escapeHtml(p.image_url)}" alt=""></a>` : ""}
                </div>
            </div>`;
        }).join("")
        : `<div class="wall-empty">Пока никто ничего не написал.</div>`;
    mount.innerHTML = `
        <div class="wall-block">
            <h2 class="section-title">Стена</h2>
            ${formHtml}
            <div class="wall-list">${postsHtml}</div>
        </div>`;
    const f = $("#wallForm");
    if (f) {
        const ta = $("#wallText"), ctr = $("#wallCounter");
        const ttlSel = $("#wallTtl");
        const attachInput = $("#wallAttachInput");
        const attachBtn = $("#wallAttachBtn");
        const preview = $("#wallAttachPreview");
        const previewImg = $("#wallAttachImg");
        const clearBtn = $("#wallAttachClear");
        let attachedDataUrl = ""; // data:URL текущего вложения
        ta?.addEventListener("input", () => {
            if (ctr) ctr.textContent = `${ta.value.length} / 2000`;
        });
        // Кнопки форматирования: оборачиваем выделение.
        const wrapSelection = (open, close) => {
            if (!ta) return;
            const s = ta.selectionStart, e = ta.selectionEnd;
            const sel = ta.value.slice(s, e) || "текст";
            ta.setRangeText(open + sel + close, s, e, "select");
            ta.focus();
        };
        f.querySelectorAll("[data-fmt]").forEach(btn => {
            btn.onclick = () => {
                const k = btn.dataset.fmt;
                if (k === "bold") wrapSelection("**", "**");
                else if (k === "italic") wrapSelection("*", "*");
                else if (k === "underline") wrapSelection("__", "__");
            };
        });
        // Сочетания клавиш Ctrl+B/I/U внутри textarea.
        ta?.addEventListener("keydown", (e) => {
            if (!(e.ctrlKey || e.metaKey)) return;
            const code = e.code;
            if (code === "KeyB") { e.preventDefault(); wrapSelection("**", "**"); }
            else if (code === "KeyI") { e.preventDefault(); wrapSelection("*", "*"); }
            else if (code === "KeyU") { e.preventDefault(); wrapSelection("__", "__"); }
        });
        // Прикрепление картинки.
        attachBtn && (attachBtn.onclick = () => attachInput?.click());
        attachInput && (attachInput.onchange = async () => {
            const file = attachInput.files && attachInput.files[0];
            if (!file) return;
            if (file.size > 30 * 1024 * 1024) { showToast("Файл больше 30 МБ — слишком крупный."); attachInput.value=""; return; }
            const overlay = $("#wallAttachOverlay");
            const submitBtn = f.querySelector("button[type=submit]");
            // Сразу покажем превью с локальным URL и наложим оверлей "Обработка".
            const localUrl = URL.createObjectURL(file);
            if (previewImg) previewImg.src = localUrl;
            if (preview) { preview.style.display = ""; preview.hidden = false; }
            if (overlay) overlay.hidden = false;
            if (submitBtn) { submitBtn.disabled = true; submitBtn.dataset.busy = "1"; }
            try {
                attachedDataUrl = await _prepareWallImage(file);
                if (previewImg) previewImg.src = attachedDataUrl;
            } catch (err) {
                attachedDataUrl = "";
                if (attachInput) attachInput.value = "";
                if (previewImg) previewImg.removeAttribute("src");
                if (preview) { preview.hidden = true; preview.style.display = "none"; }
                showToast(err && err.message ? err.message : "Не удалось прочитать файл");
            } finally {
                URL.revokeObjectURL(localUrl);
                if (overlay) overlay.hidden = true;
                if (submitBtn) { submitBtn.disabled = false; delete submitBtn.dataset.busy; }
            }
        });
        if (clearBtn) {
            clearBtn.addEventListener("click", (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                attachedDataUrl = "";
                if (attachInput) attachInput.value = "";
                if (previewImg) previewImg.removeAttribute("src");
                if (preview) {
                    preview.hidden = true;
                    preview.style.display = "none";
                }
            });
        }
        f.onsubmit = async (e) => {
            e.preventDefault();
            const submitBtn = f.querySelector("button[type=submit]");
            if (submitBtn?.dataset.busy === "1") {
                showToast("Подождите, идёт обработка изображения…"); return;
            }
            const text = (ta?.value || "").trim();
            if (!text && !attachedDataUrl) { showToast("Напишите текст или прикрепите картинку"); return; }
            const ttl = parseInt(ttlSel?.value || ttlDefault, 10);
            try {
                const body = { text, ttl_hours: ttl };
                if (attachedDataUrl) body.image_data_url = attachedDataUrl;
                const r = await fetch(`/api/u/${encodeURIComponent(slug)}/wall`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
                const j = await r.json();
                if (!r.ok) throw new Error(j.message || j.error || "Ошибка");
                if (ta) ta.value = "";
                if (ctr) ctr.textContent = "0 / 2000";
                attachedDataUrl = "";
                if (attachInput) attachInput.value = "";
                if (preview) preview.hidden = true;
                renderWall(slug, mount);
            } catch (err) { showToast(err.message || "Не удалось опубликовать"); }
        };
    }
    mount.querySelectorAll("[data-del]").forEach(btn => {
        btn.onclick = async () => {
            if (!confirm("Удалить запись?")) return;
            try {
                const r = await fetch(`/api/wall/${btn.dataset.del}`, { method: "DELETE" });
                if (!r.ok) throw new Error("Ошибка");
                renderWall(slug, mount);
            } catch (err) { showToast(err.message); }
        };
    });
    mount.querySelectorAll("[data-go-user]").forEach(a => {
        a.onclick = (e) => {
            e.preventDefault();
            const s = a.dataset.goUser;
            history.pushState(null, "", `/u/${s}`);
            openPublicUser(s);
        };
    });
    // Открыть прикреплённую картинку поста в lightbox.
    mount.querySelectorAll("[data-img]").forEach(a => {
        a.onclick = (e) => {
            e.preventDefault();
            const src = a.dataset.img;
            if (typeof openImageLightbox === "function") openImageLightbox(src, { kind: "post" });
            else window.open(src, "_blank");
        };
    });
    const lb = $("#wallLogin"); if (lb) lb.onclick = () => openAuth();
}

function pickImage(target) {
    // avatar / banner → новая 2-шаговая модалка с кропом и предпросмотром.
    if (target === "avatar" || target === "banner" || target === "cover") {
        // legacy "cover" мапим на banner — раньше cover использовался как обложка
        // профиля, теперь это banner; но поле в БД "cover" сохраняем как fallback.
        const t = target === "cover" ? "banner" : target;
        return openImageUpload(t);
    }
    state.pendingImage = { target }; $("#imgPicker").click();
}

$("#imgPicker").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
        const dataUrl = ev.target.result;
        const t = state.pendingImage;
        try {
            // Загружаем картинку как файл на сервер → получаем стабильный
            // URL /api/img/<id>, который видно всем пользователям.
            const kind = (t.target === "playlist_cover") ? "playlist" : t.target;
            let url = dataUrl;
            try {
                const up = await api("/api/upload/image", {
                    method: "POST",
                    body: { data_url: dataUrl, kind },
                });
                if (up && up.url) url = up.url;
            } catch (upErr) {
                // Fallback: оставим data:URL, чтобы старая логика всё ещё работала.
                console.warn("upload failed, fallback to data URL", upErr);
            }
            if (t.target === "avatar" || t.target === "cover") {
                await api("/api/profile", { method: "POST", body: { [t.target]: url } });
                state.me[t.target] = url;
                renderUserPill(); renderProfilePage();
                showToast(t.target==="avatar"?"Аватар обновлён":"Обложка обновлена");
            } else if (t.target === "playlist_cover") {
                await api(`/api/playlists/${t.pid}`, { method: "PATCH", body: { cover: url } });
                await loadPlaylists(); renderPlaylistPage(t.pid);
                showToast("Обложка обновлена");
            }
        } catch (err) { showToast(err.message); }
        finally { state.pendingImage = null; e.target.value = ""; }
    };
    reader.readAsDataURL(f);
});

// ИМПОРТ из файла — фоновый job + модальное окно с прогрессом.
$("#filePicker").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
        const fd = new FormData();
        fd.append("file", f);
        const r = await api("/api/import/file", { method: "POST", body: fd });
        if (!r || !r.job_id) throw new Error("Сервер не вернул job_id");
        await _runImportJob(r.job_id, r.total, f.name);
    } catch (err) { showToast(err.message); }
    finally { e.target.value = ""; }
});

// Открывает модалку прогресса и поллит /api/import/status пока не done/error.
async function _runImportJob(jobId, total, filename) {
    let modal = $("#importModal");
    if (!modal) {
        modal = document.createElement("div");
        modal.id = "importModal";
        modal.className = "modal";
        modal.innerHTML = `
          <div class="modal-card import-modal-card">
            <div class="modal-head">
              <div>
                <div class="modal-title">Импорт треков</div>
                <div class="modal-sub" id="importFilename"></div>
              </div>
              <button class="icon-btn" data-close-import title="Свернуть">×</button>
            </div>
            <div class="import-bar"><div class="import-bar-fill" id="importBarFill"></div></div>
            <div class="import-stats">
              <span id="importStatsCount">0 / 0</span>
              <span class="import-dot">·</span>
              <span class="import-added" id="importStatsAdded">+0</span>
              <span class="import-dot">·</span>
              <span class="import-skipped" id="importStatsSkipped">пропущено 0</span>
            </div>
            <div class="import-current" id="importCurrent"></div>
            <div class="import-skipped-list" id="importSkippedList"></div>
            <div class="import-actions">
              <button class="btn btn-secondary" id="importCancelBtn">Отменить</button>
            </div>
          </div>`;
        document.body.appendChild(modal);
        modal.addEventListener("click", (ev) => {
            if (ev.target.closest("[data-close-import]") || ev.target === modal) {
                modal.hidden = true;
            }
        });
    }
    modal.hidden = false;
    $("#importFilename").textContent = filename || "";
    $("#importBarFill").style.width = "0%";
    $("#importStatsCount").textContent = `0 / ${total}`;
    $("#importStatsAdded").textContent = "+0";
    $("#importStatsSkipped").textContent = "пропущено 0";
    $("#importCurrent").textContent = "Подготовка…";
    $("#importSkippedList").innerHTML = "";
    let cancelled = false;
    $("#importCancelBtn").onclick = async () => {
        cancelled = true;
        try { await api("/api/import/cancel", { method: "POST", body: { id: jobId } }); } catch {}
        $("#importCurrent").textContent = "Отменено пользователем.";
        $("#importCancelBtn").textContent = "Закрыть";
        $("#importCancelBtn").onclick = () => { modal.hidden = true; };
    };
    // Поллим каждые 700 мс, не загружая сервер.
    while (true) {
        await new Promise(r => setTimeout(r, 700));
        let st;
        try { st = await api("/api/import/status?id=" + encodeURIComponent(jobId), { silent: true }); }
        catch (e) { $("#importCurrent").textContent = "Ошибка соединения, повтор…"; continue; }
        const done = st.index || 0;
        const tot = st.total || total || 1;
        const pct = Math.min(100, Math.round((done / tot) * 100));
        $("#importBarFill").style.width = pct + "%";
        $("#importStatsCount").textContent = `${done} / ${tot}`;
        $("#importStatsAdded").textContent = `+${st.added || 0}`;
        $("#importStatsSkipped").textContent = `пропущено ${st.skipped || 0}`;
        $("#importCurrent").textContent = st.current || "";
        if (Array.isArray(st.skipped_lines) && st.skipped_lines.length) {
            $("#importSkippedList").innerHTML =
                `<div class="import-skipped-title">Не найдены (${st.skipped_lines.length}${st.skipped > st.skipped_lines.length ? "+" : ""}):</div>` +
                st.skipped_lines.slice(-10).map(line => `<div class="import-skipped-line">${escapeHtml(line)}</div>`).join("");
        }
        if (st.status === "done" || st.status === "error" || cancelled) {
            if (st.status === "done") {
                $("#importCurrent").textContent = `Готово: добавлено ${st.added || 0} из ${tot}.`;
                showToast(`Импорт завершён: ${st.added || 0}/${tot}`);
                try { await loadPlaylists(); } catch {}
                if (st.playlist_id && !cancelled) {
                    setTimeout(() => { modal.hidden = true; navigate("playlist", { id: st.playlist_id }); }, 1200);
                }
            } else if (st.status === "error") {
                $("#importCurrent").textContent = "Ошибка: " + (st.error || "неизвестно");
            }
            $("#importCancelBtn").textContent = "Закрыть";
            $("#importCancelBtn").onclick = () => { modal.hidden = true; };
            break;
        }
    }
}

// =================================================================
// EDIT MODAL — закрытие
// =================================================================
$("#editModal").addEventListener("click", (e) => {
    if (e.target.closest("[data-close]") || e.target === e.currentTarget) $("#editModal").hidden = true;
});

// =================================================================
// ВОСПРОИЗВЕДЕНИЕ
// =================================================================
async function playQueue(tracks, idx, opts={}) {
    // В офлайне — отфильтровываем подборку до тех треков, для которых есть
    // локальный blob (точное совпадение или fuzzy по source_id/названию).
    if (isOfflineMode()) {
        const orig = tracks[idx];
        const all = await listDownloads().catch(() => []);
        const sids = new Set(all.map(r => String(r.meta?.source_id || "")));
        const titles = new Set(all.map(r => ((r.meta?.title || "") + "|" + (r.meta?.artist || "")).toLowerCase()));
        const hasOffline = (t) => {
            if (!t) return false;
            if (state.offline.downloaded.has(trackKey(t))) return true;
            const sid = String(t.source_id || t.id || "");
            if (sid && sids.has(sid)) return true;
            const k = ((t.title || "") + "|" + (t.artist || "")).toLowerCase();
            return titles.has(k);
        };
        const filtered = (tracks || []).filter(hasOffline);
        if (!filtered.length) {
            showToast("Нет скачанных треков из этой подборки");
            return;
        }
        let ni = filtered.indexOf(orig);
        if (ni < 0) ni = 0;
        tracks = filtered; idx = ni;
    }
    state.queue = tracks.slice(); state.qi = idx;
    if (!opts.trailer) state.trailerMode = null;
    // Запоминаем, откуда был запущен трек: home/search/charts/wave/playlist/library/artist
    state.queueOrigin = opts.from_view || opts.origin || state.currentView || "other";
    // Если был запланирован автозапуск волны (debounce из настроек чипов), а
    // пользователь успел кликнуть на трек в другом месте (поиск, плейлист) —
    // отменяем, иначе волна перезапишет очередь через 450 мс.
    if (typeof _waveAutoTimer !== "undefined" && _waveAutoTimer && state.queueOrigin !== "wave") {
        clearTimeout(_waveAutoTimer); _waveAutoTimer = null;
        vlog("wave auto-start cancelled by playQueue from", state.queueOrigin);
    }
    vlog("playQueue", state.queueOrigin, "size=", state.queue.length, "idx=", idx,
         "first:", tracks[idx] && (tracks[idx].title + " — " + tracks[idx].artist));
    await playCurrent();
    // Прогреваем серверный резолвер-кэш для следующих 3 треков очереди —
    // моментальный старт при переключении next.
    try { _prewarmNext(); } catch {}
}
function _prewarmNext() {
    if (isOfflineMode()) return;
    if (!Array.isArray(state.queue)) return;
    const next = [];
    for (let i = state.qi + 1; i < Math.min(state.queue.length, state.qi + 4); i++) {
        const t = state.queue[i];
        if (!t || !t.title) continue;
        next.push({
            q: ((t.artist || "") + " " + (t.title || "")).trim(),
            duration: t.duration || 0,
        });
    }
    if (!next.length) return;
    fetch("/api/prewarm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tracks: next }),
        keepalive: true,
    }).catch(() => {});
    vlog("prewarm queued", next.length, "tracks");
}
async function playCurrent() {
    if (state.qi < 0 || state.qi >= state.queue.length) return;
    const t = state.queue[state.qi]; state.track = t;
    const myGen = (state._playGen = (state._playGen || 0) + 1);
    vlog("▶ PLAY gen=", myGen, "qi=", state.qi,
         "id=", t.source_id, "src=", t.source,
         "title=", t.title, "artist=", t.artist,
         "dur=", t.duration, "preview=", !!t.preview_url);
    // ЖЁСТКИЙ сброс — иначе старый поток может «доиграть» поверх нового.
    // pause() достаточно: установка нового src сама отменит предыдущий запрос.
    try { audio.pause(); } catch {}
    renderNowPlaying(); refreshTrackRows();
    if (state.trailerTimer) { clearTimeout(state.trailerTimer); state.trailerTimer = null; }
    try {
        const url = `/api/stream?source=${encodeURIComponent(t.source||"")}&source_id=${encodeURIComponent(t.source_id||"")}&q=${encodeURIComponent((t.artist||"")+" "+(t.title||""))}&duration=${t.duration||0}${t.explicit?"&explicit=1":""}${t.preview_url?`&preview=${encodeURIComponent(t.preview_url)}`:""}`;
        state.streamUrl = url;
        vlog("  stream URL:", url);
        if (myGen !== state._playGen) { vlog("  cancelled (gen mismatch before blob)"); return; }     // пользователь уже выбрал другой
        // Сначала ВСЕГДА пытаемся найти локальный blob — даже если в Set ключа
        // не оказалось (Set может быть устаревшим / сорc/source_id чуть-чуть
        // отличаются). Это гарантирует, что скачанные треки никогда не идут
        // через сеть.
        let srcUrl = url;
        const blobUrl = await offlineGetBlobUrl(t);
        if (blobUrl) {
            if (state._lastBlobUrl) { try { URL.revokeObjectURL(state._lastBlobUrl); } catch {} }
            state._lastBlobUrl = blobUrl;
            srcUrl = blobUrl;
        } else if (isOfflineMode()) {
            // Офлайн и blob не найден — пропускаем трек.
            showToast("Офлайн режим: трек не скачан");
            setTimeout(() => playNext(), 300);
            return;
        }
        state._lastApiStreamUrl = url;
        audio.src = srcUrl;
        // Параллельно узнаём фактический источник (full/preview) у сервера.
        state._previewKnown = false;
        state._isPreview = false;
        document.body.classList.remove("preview-mode");
        const npBadge = $("#np-preview-badge"); if (npBadge) npBadge.hidden = true;
        // Бейдж «PREVIEW» — только для гостей. У залогиненных пользователей,
        // даже если сервер отдал короткий фрагмент из-за фолбэка источников,
        // не пугаем их этим лейблом.
        if (!state.me) {
            fetch(url, { method: "HEAD" }).then(resp => {
                if (myGen !== state._playGen) return;
                const kind = (resp.headers.get("X-Velora-Source") || "").toLowerCase();
                const isPreview = kind === "preview";
                state._previewKnown = true;
                state._isPreview = isPreview;
                document.body.classList.toggle("preview-mode", isPreview);
                if (npBadge) npBadge.hidden = !isPreview;
                // Превью-фрагмент = LRC-тайминги не совпадут → показываем текст без авто-следования.
                if (state.lyrics) renderLyrics();
            }).catch(() => {});
        }
        await audio.play();
        if (myGen !== state._playGen) { try { audio.pause(); } catch {} return; }
        if (state.trailerMode) {
            const snippet = state.trailerMode.snippet || 25;
            const dur = t.duration || 60;
            const start = Math.max(0, Math.floor(dur/2 - snippet/2));
            audio.currentTime = start;
            state.trailerTimer = setTimeout(() => playNext(), snippet * 1000);
        }
        if (state.me && !isOfflineMode()) api("/api/history", { method: "POST", body: { ...t, from_view: state.queueOrigin || "other" } }).catch(()=>{});
        loadLyrics();
        // Догружаем полные данные о треке (фиты + album_id) в фоне, не блокируя воспроизведение.
        // После апдейта — пере-рендер плеера с кликабельными именами всех артистов.
        if (!isOfflineMode() && (t.source||"deezer") === "deezer" && t.source_id && (!t.artists || !t.artists.length || !t.album_id)) {
            api(`/api/track/${encodeURIComponent(t.source||"deezer")}/${encodeURIComponent(t.source_id)}`, { silent404: true }).then(full => {
                if (myGen !== state._playGen || !full) return;
                const cur = state.track;
                if (!cur || cur.source_id !== t.source_id) return;
                if (full.artists && full.artists.length) cur.artists = full.artists;
                if (full.album_id) cur.album_id = String(full.album_id);
                if (full.artist) cur.artist = full.artist; // объединённое имя «A, B, C»
                renderNowPlaying();
                refreshTrackRows();
                vlog("track enriched", cur.title, "artists=", cur.artists, "album_id=", cur.album_id);
            }).catch(()=>{});
        }
    } catch (e) {
        if (e.message === "auth") return;
        if (e.name === "AbortError") return;
        if (String(e).includes("451")) showToast("Этот трек заблокирован детским режимом");
        else showToast("Не удалось воспроизвести: "+e.message);
        // авто-следующий, если ошибка
        setTimeout(() => playNext(), 500);
    }
}
function playNext() {
    if (state.repeat === "one" && !state.trailerMode) { audio.currentTime = 0; audio.play().catch(()=>{}); return; }
    if (state.shuffle && state.queue.length > 1) {
        let j = state.qi; while (j === state.qi) j = Math.floor(Math.random()*state.queue.length);
        state.qi = j;
    } else state.qi++;
    if (state.qi >= state.queue.length) {
        if (state.repeat === "all") state.qi = 0;
        // Бесконечная волна: перед остановкой пробуем подгрузить ещё.
        else if (state.queueOrigin === "wave") {
            state.qi = state.queue.length; // временно за пределами
            _extendWaveQueue().then(() => {
                if (state.qi < state.queue.length) playCurrent();
                else { state.qi = state.queue.length-1; state.trailerMode = null; }
            });
            return;
        }
        else { state.qi = state.queue.length-1; state.trailerMode = null; return; }
    }
    // Префетч следующей партии волны заранее.
    if (state.queueOrigin === "wave") _extendWaveQueue();
    playCurrent();
}
function playPrev() {
    if (audio.currentTime > 3) { audio.currentTime = 0; return; }
    state.qi = Math.max(0, state.qi-1);
    playCurrent();
}

// =================================================================
// UI ПЛЕЕРА
// =================================================================
// HTML с кликабельными именами артистов (включая фитов).
// Если t.artists пуст — fallback на одно имя t.artist.
// Каждое имя → data-artist-id для делегированного клика.
function _artistsHtml(t) {
    if (!t) return "";
    const list = Array.isArray(t.artists) && t.artists.length
        ? t.artists
        : (t.artist ? [{ id: t.artist_id || "", name: t.artist }] : []);
    if (!list.length) return "";
    return list.map(a => {
        const aid = a.id ? ` data-artist-id="${escapeHtml(a.id)}"` : "";
        return `<span class="np-artist-chip"${aid}>${escapeHtml(a.name)}</span>`;
    }).join('<span class="np-artist-sep">, </span>');
}
function renderNowPlaying() {
    const t = state.track;
    // Если трека нет — прячем блок «обложка + название + артист + E» в нижнем плеере.
    if (!t) {
        document.body.classList.remove("has-track");
        return;
    }
    document.body.classList.add("has-track");
    $("#np-cover").src = t.album_cover || "";
    // В офлайне (или если cover blob уже сохранён) — подменяем на blob-URL,
    // чтобы обложка показывалась без сети.
    if (state.offline.downloaded.has((t.source||"")+"|"+(t.source_id||""))) {
        offlineGetCoverUrl(t).then(u => {
            if (!u) return;
            const c1 = $("#np-cover"); if (c1) c1.src = u;
            const c2 = $("#fp-cover"); if (c2) c2.src = u;
        });
    }
    const npTitle = $("#np-title"), npArtist = $("#np-artist");
    npTitle.textContent = t.title || "—";
    // Артисты: если есть массив t.artists (после fetch /api/track) — рендерим
    // каждое имя как кликабельную ссылку с разделителем «, ». Иначе — одно имя.
    npArtist.innerHTML = _artistsHtml(t);
    npTitle.classList.add("clickable"); npArtist.classList.add("clickable");
    npTitle.title = t.album_id ? "Открыть альбом" : "Открыть страницу трека";
    $("#np-explicit").hidden = !t.explicit;
    $("#fp-cover").src = t.album_cover || "";
    // Динамическая подложка фуллскрина: блюрим обложку и применяем как фон.
    const fp = $("#fullplayer");
    if (fp && t.album_cover) {
        fp.style.setProperty("--fp-bg", `url(${JSON.stringify(t.album_cover)})`);
        fp.classList.add("has-bg");
    } else if (fp) {
        fp.classList.remove("has-bg");
    }
    const fpName = $("#fp-track-name"), fpArt = $("#fp-track-artist");
    fpName.textContent = t.title || "";
    fpName.classList.add("clickable");
    fpName.title = t.title ? `Открыть «${t.title}»` : "";
    fpArt.innerHTML = _artistsHtml(t);
    fpArt.classList.add("clickable");
    fpArt.title = t.artist ? `К исполнителю «${t.artist}»` : "";
    const k = (t.source||"")+"|"+(t.source_id||"");
    const liked = state.likedKeys.has(k);
    $("#likeBtn").classList.toggle("is-liked", liked);
    setIcon($("#likeBtn").querySelector(".ic"), liked ? "heart-fill" : "heart");
    $("#fpLikeBtn").classList.toggle("is-liked", liked);
    setIcon($("#fpLikeIcon"), liked ? "heart-fill" : "heart");
    const disliked = state.dislikedTrackKeys && state.dislikedTrackKeys.has(k);
    $("#fpToolDislike")?.classList.toggle("active", !!disliked);
    // Подсвечиваем кнопку «скачать» в мини-плеере и фуллскрине, если трек уже сохранён.
    const dlOn = state.offline.downloaded.has(k);
    const dlBtn = $("#dlBtn");
    if (dlBtn) {
        dlBtn.classList.toggle("is-downloaded", dlOn);
        dlBtn.title = dlOn ? "Удалить из загрузок" : "Скачать в офлайн";
        const dlIc = $("#dlIcon"); if (dlIc) dlIc.querySelector("use").setAttribute("href", "#i-" + (dlOn?"check":"download"));
    }
    const fpDl = $("#fpToolDownload");
    if (fpDl) {
        fpDl.classList.toggle("active", dlOn);
        fpDl.title = dlOn ? "Удалить из загрузок" : "Скачать";
        const fpDlIc = $("#fpToolDownloadIcon"); if (fpDlIc) fpDlIc.querySelector("use").setAttribute("href", "#i-" + (dlOn?"check":"download"));
    }
    // Префетч следующего трека для мгновенного переключения.
    schedulePrefetchNext();
    // Иконки волны на главной/wave-странице — синхронизируем с состоянием.
    updateWavePlayIcons();
    // Кнопка дизлайка в нижнем плеере — синхронизируем подсветку с текущим треком.
    if (window._updateDislikeBtn) window._updateDislikeBtn();
}

// =================================================================
// PREFETCH следующего трека (мгновенное переключение)
// =================================================================
let _prefetchedKey = null;
let _prefetchTimer = null;
function schedulePrefetchNext() {
    if (_prefetchTimer) clearTimeout(_prefetchTimer);
    _prefetchTimer = setTimeout(() => prefetchNext(), 1500);
}
async function prefetchNext() {
    try {
        const next = state.queue[state.qi + 1];
        if (!next) return;
        const k = (next.source||"")+"|"+(next.source_id||"");
        if (k === _prefetchedKey) return;
        _prefetchedKey = k;
        // Если уже скачан в офлайн — ничего не делаем, играем напрямую из IDB.
        if (await offlineHas(next)) return;
        const url = `/api/stream?source=${encodeURIComponent(next.source||"")}&source_id=${encodeURIComponent(next.source_id||"")}&q=${encodeURIComponent((next.artist||"")+" "+(next.title||""))}&duration=${next.duration||0}${next.explicit?"&explicit=1":""}${next.preview_url?`&preview=${encodeURIComponent(next.preview_url)}`:""}`;
        // Прогреваем браузерный кэш HEAD-запросом — сервер вернёт Content-Length и Last-Modified.
        // Низкий приоритет, чтобы не мешать текущему стриму.
        try { fetch(url, { method: "HEAD", priority: "low" }).catch(()=>{}); } catch {}
        // <link rel="prefetch"> — браузер сам подтянет первые ranges, если поддерживает.
        const old = document.head.querySelector("link[data-prefetch-stream]");
        if (old) old.remove();
        const link = document.createElement("link");
        link.rel = "prefetch";
        link.as = "audio";
        link.href = url;
        link.dataset.prefetchStream = "1";
        document.head.appendChild(link);
    } catch {}
}
function refreshTrackRows() {
    $$(".track-row").forEach(r => {
        const k = r.dataset.key;
        const cur = state.track ? (state.track.source||"")+"|"+(state.track.source_id||"") : "";
        r.classList.toggle("playing", k && k === cur);
    });
}

audio.addEventListener("play", () => {
    // Защита от «застрявшего» muted=true (например, после неудачного iOS-unlock):
    // если громкость > 0, гарантируем что звук слышен. Без этой строки приходилось
    // дёргать слайдер громкости после перезагрузки, чтобы трек заиграл.
    if (audio.volume > 0 && audio.muted) audio.muted = false;
    setIcon($("#playIcon"),"pause"); setIcon($("#fpPlayIcon"),"pause"); updateWavePlayIcons();
});
audio.addEventListener("pause", () => { setIcon($("#playIcon"),"play"); setIcon($("#fpPlayIcon"),"play"); updateWavePlayIcons(); });
// Жёсткая ошибка <audio> (DECODE/NETWORK/SRC_NOT_SUPPORTED) — лог + авто-следующий
// (особенно важно для волны: когда трек не отдаётся сервером, не зависаем).
audio.addEventListener("error", async () => {
    const err = audio.error;
    const codes = { 1: "ABORTED", 2: "NETWORK", 3: "DECODE", 4: "SRC_NOT_SUPPORTED" };
    const code = err ? codes[err.code] || ("err"+err.code) : "unknown";
    verr("audio error", code, "track=", state.track && state.track.title, "src=", audio.currentSrc || audio.src);
    // Сетевая ошибка стрима — попробуем сыграть из локального blob (на случай,
    // если сервер 502 / нет интернета, но трек скачан).
    const isNet = (code === "NETWORK" || code === "SRC_NOT_SUPPORTED");
    const playingFromApi = (audio.currentSrc || "").includes("/api/stream");
    if (isNet && playingFromApi && state.track) {
        try {
            const blobUrl = await offlineGetBlobUrl(state.track);
            if (blobUrl) {
                if (state._lastBlobUrl) { try { URL.revokeObjectURL(state._lastBlobUrl); } catch {} }
                state._lastBlobUrl = blobUrl;
                showToast("Сеть недоступна — играю скачанную копию");
                audio.src = blobUrl;
                try { await audio.play(); return; } catch {}
            }
        } catch {}
    }
    if (state.track && state.queue && state.queue.length > 1) {
        // Анти-каскад: если предыдущий скип был <1.5с назад — это лавина 503,
        // подождём 3с и попробуем тот же трек ещё раз (резолвер мог отойти).
        const _now = Date.now();
        const lastSkip = state._lastAutoSkipAt || 0;
        if (_now - lastSkip < 1500) {
            state._autoSkipChain = (state._autoSkipChain || 0) + 1;
            if (state._autoSkipChain >= 3) {
                // 3 трека подряд провалились — пауза, пользователь сам решит.
                state._autoSkipChain = 0;
                showToast("Сервис треков недоступен. Попробуйте через минуту.");
                return;
            }
        } else {
            state._autoSkipChain = 0;
        }
        state._lastAutoSkipAt = _now;
        showToast("Трек недоступен — следующий…");
        setTimeout(() => playNext(), 600);
    }
});
audio.addEventListener("stalled", () => vwarn("audio stalled", state.track && state.track.title));
audio.addEventListener("waiting", () => vlog("audio waiting (buffering)"));
audio.addEventListener("canplay", () => vlog("audio canplay", state.track && state.track.title));
audio.addEventListener("loadedmetadata", () => {
    state.duration = audio.duration || 0;
    const shown = state.duration;
    $("#time-total").textContent = fmtTime(shown);
    const fpTot = $("#fp-time-total"); if (fpTot) fpTot.textContent = fmtTime(shown);
    // Точное определение превью идёт по HEAD-запросу к /api/stream (X-Velora-Source).
    // Здесь длительностный фолбэк отключён — он слишком часто ошибался.
    syncLyrics();
});
audio.addEventListener("timeupdate", () => {
    const shownCur = audio.currentTime;
    $("#time-cur").textContent = fmtTime(shownCur);
    const fpCur = $("#fp-time-cur"); if (fpCur) fpCur.textContent = fmtTime(shownCur);
    const totalForBar = state.duration;
    if (totalForBar > 0) {
        const v = (shownCur/totalForBar)*1000;
        $("#seek").value = v; $("#fp-seek").value = v;
        // Заполняем «прошедшую» полосу под бегунком — визуально видно сколько уже сыграно.
        const pct = Math.max(0, Math.min(100, (shownCur/totalForBar)*100)).toFixed(2) + "%";
        const sp = $("#seekPlayed"); if (sp) sp.style.width = pct;
        const fsp = $("#fpSeekPlayed"); if (fsp) fsp.style.width = pct;
        // Кружок-бегунок в новом fp-seek двигаем к проценту прошедшего.
        const fpThumb = $("#fpSeekThumb"); if (fpThumb) fpThumb.style.left = pct;
        // Тонкая полоска прогресса в мини-плеере (мобильная версия).
        const ply = $("#player"); if (ply) ply.style.setProperty("--mini-progress", pct);
    }
    enforceGuestLimit();
    syncLyrics();
});
audio.addEventListener("ended", () => playNext());

// Визуализатор прогрузки трека (audio.buffered).
// Показывает сколько секунд уже скачал браузер — полупрозрачная полоса
// под бегунком seek. Берём диапазон, который содержит currentTime
// (или последний, если currentTime=0), это самый «честный» способ
// показать прогрузку, так как при seek-е buffered может быть «дырявым».
function updateBufferBar() {
    const bb = $("#seekBuffer"); const fb = $("#fpSeekBuffer");
    if (!bb && !fb) return;
    const dur = state.duration || audio.duration || 0;
    if (!dur || !isFinite(dur)) {
        if (bb) bb.style.width = "0%";
        if (fb) fb.style.width = "0%";
        return;
    }
    let end = 0;
    try {
        const br = audio.buffered;
        const ct = audio.currentTime;
        for (let i = 0; i < br.length; i++) {
            if (br.start(i) <= ct + 0.5 && br.end(i) >= end) end = br.end(i);
        }
        // Если currentTime=0 и нет совпавшего диапазона — берём максимум.
        if (end === 0 && br.length) end = br.end(br.length - 1);
    } catch {}
    const totalForBar = dur;
    const pct = Math.max(0, Math.min(100, (end / totalForBar) * 100));
    const w = pct.toFixed(2) + "%";
    if (bb) bb.style.width = w;
    if (fb) fb.style.width = w;
}
audio.addEventListener("progress", updateBufferBar);
audio.addEventListener("loadedmetadata", updateBufferBar);
audio.addEventListener("timeupdate", updateBufferBar);
audio.addEventListener("emptied", () => {
    const bb = $("#seekBuffer"); if (bb) bb.style.width = "0%";
    const fb = $("#fpSeekBuffer"); if (fb) fb.style.width = "0%";
    const sp = $("#seekPlayed"); if (sp) sp.style.width = "0%";
    const fsp = $("#fpSeekPlayed"); if (fsp) fsp.style.width = "0%";
    const fpThumb = $("#fpSeekThumb"); if (fpThumb) fpThumb.style.left = "0%";
});

// =================================================================
// ГОСТЕВОЙ ЛИМИТ — отключён (раньше резал 30 сек, теперь все слушают полностью)
// =================================================================
const GUEST_LIMIT = 0; // 0 — без лимита
let guestLimitFired = false;
function enforceGuestLimit() { /* no-op */ }
function openGuestModal() {
    const m = $("#guestModal"); if (m) m.hidden = false;
}
function closeGuestModal() {
    const m = $("#guestModal"); if (m) m.hidden = true;
}
$("#guestModal")?.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]") || e.target === e.currentTarget) closeGuestModal();
});
$("#guestRegister")?.addEventListener("click", () => { closeGuestModal(); openAuth("register"); });
$("#guestLogin")?.addEventListener("click", () => { closeGuestModal(); openAuth("login"); });
// При смене трека сбрасываем флажок (на всякий случай).
audio.addEventListener("loadeddata", () => { guestLimitFired = false; });


$("#playBtn").onclick = () => audio.paused ? audio.play().catch(()=>{}) : audio.pause();
$("#fpPlayBtn").onclick = () => audio.paused ? audio.play().catch(()=>{}) : audio.pause();
$("#prevBtn").onclick = $("#fpPrevBtn").onclick = playPrev;
$("#nextBtn").onclick = $("#fpNextBtn").onclick = playNext;
const onShuffle = () => { state.shuffle = !state.shuffle;
    $("#shuffleBtn").classList.toggle("active", state.shuffle);
    $("#fpShuffleBtn").classList.toggle("active", state.shuffle);
    try { localStorage.setItem("velora_shuffle", state.shuffle ? "1":"0"); } catch{}
    showToast(state.shuffle?"Перемешивание включено":"Перемешивание выключено");
};
$("#shuffleBtn").onclick = onShuffle; $("#fpShuffleBtn").onclick = onShuffle;
const updateRepeatUi = () => {
    const cls = state.repeat !== "off";
    $("#repeatBtn").classList.toggle("active", cls);
    $("#fpRepeatBtn").classList.toggle("active", cls);
    const iconHref = state.repeat === "one" ? "#i-repeat-one" : "#i-repeat";
    $("#repeatBtn").querySelector("use")?.setAttribute("href", iconHref);
    $("#fpRepeatBtn").querySelector("use")?.setAttribute("href", iconHref);
    $("#repeatBtn").title = state.repeat === "one" ? "Повтор: одного трека" : (state.repeat === "all" ? "Повтор: очереди" : "Повтор");
};
const onRepeat = () => {
    state.repeat = state.repeat==="off"?"all":state.repeat==="all"?"one":"off";
    updateRepeatUi();
    try { localStorage.setItem("velora_repeat", state.repeat); } catch{}
    showToast("Повтор: "+(state.repeat==="off"?"выкл":state.repeat==="one"?"одного трека":"очереди"));
};
$("#repeatBtn").onclick = onRepeat; $("#fpRepeatBtn").onclick = onRepeat;
window._updateRepeatUi = updateRepeatUi;

$("#likeBtn").onclick = () => state.track && toggleLike(state.track);
$("#fpLikeBtn").onclick = () => state.track && toggleLike(state.track);
// Авто-инъекция кнопки «Дизлайк» — на случай, если SW отдаёт устаревший
// index.html без неё. Вставляем сразу после кнопки повтора.
(function ensureDislikeBtn(){
    if (document.getElementById("dislikeBtn")) return;
    const repeatBtn = document.getElementById("repeatBtn");
    if (!repeatBtn) return;
    const btn = document.createElement("button");
    btn.id = "dislikeBtn";
    btn.className = "icon-btn";
    btn.title = "Не нравится";
    btn.innerHTML = '<svg class="ic"><use href="#i-dislike"/></svg>';
    repeatBtn.insertAdjacentElement("afterend", btn);
})();
document.getElementById("dislikeBtn")?.addEventListener("click", () => state.track && toggleDislike(state.track));
function updateDislikeBtn() {
    const btn = $("#dislikeBtn"); if (!btn) return;
    const t = state.track;
    if (!t) { btn.classList.remove("active"); return; }
    const k = (t.source||"")+"|"+(t.source_id||"");
    btn.classList.toggle("active", state.dislikedTrackKeys && state.dislikedTrackKeys.has(k));
}
window._updateDislikeBtn = updateDislikeBtn;

$("#seek").addEventListener("input", e => { if (state.duration) audio.currentTime = (e.target.value/1000)*state.duration; });
$("#fp-seek").addEventListener("input", e => { if (state.duration) audio.currentTime = (e.target.value/1000)*state.duration; });

$("#vol").addEventListener("input", e => {
    const pct = Math.max(0, Math.min(100, Number(e.target.value)));
    audio.volume = pct/100;
    // EQ отключён — muted=false всегда (кроме 0%).
    audio.muted = (pct === 0);
    setVolumeUi(pct);
});
audio.volume = 0.7;
// fullscreen volume slider
$("#fpVol")?.addEventListener("input", e => {
    const pct = Math.max(0, Math.min(100, Number(e.target.value)));
    audio.volume = pct/100;
    audio.muted = (pct === 0);
    setVolumeUi(pct);
});
function setVolumeUi(pct) {
    const v = $("#vol"); const fv = $("#fpVol");
    if (v) { v.value = pct; v.style.setProperty("--v", pct); }
    if (fv) { fv.value = pct; fv.style.setProperty("--v", pct); }
    const t = $("#volPct"); if (t) t.textContent = pct + "%";
    const ft = $("#fpVolPct"); if (ft) ft.textContent = pct + "%";
    setIcon($("#volIcon"), pct === 0 ? "mute" : "vol");
    try { localStorage.setItem("velora_vol", String(pct)); } catch {}
}
// стартовая громкость из localStorage
// EQ отключён — просто гарантируем muted=false на старте.
try { audio.muted = false; } catch {}
try {
    const saved = Number(localStorage.getItem("velora_vol"));
    if (Number.isFinite(saved) && saved >= 0 && saved <= 100) {
        audio.volume = saved/100;
        setVolumeUi(saved);
    } else {
        setVolumeUi(70);
    }
} catch { setVolumeUi(70); }

// Восстановление EQ из localStorage — ОТКЛЮЧЕНО (эквалайзер вырезан).
// Сохранённые значения velora_eq в localStorage больше не читаются.
function saveEqPrefs() {
    try {
        localStorage.setItem("velora_eq", JSON.stringify({
            enabled: !!state.eq.enabled,
            preset: state.eq.preset,
            gains: state.eq.gains,
        }));
    } catch {}
}
// Восстановление качества и lyricsFollow
try {
    const q = localStorage.getItem("velora_quality");
    const sel = $("#qualitySel");
    if (sel && q) sel.value = q;
    if (sel) sel.onchange = () => { try { localStorage.setItem("velora_quality", sel.value); } catch {} };
} catch {}
try {
    state.karaokeFollow = localStorage.getItem("velora_lyrics_follow") !== "0";
    if (localStorage.getItem("velora_reduce_motion") === "1") document.body.classList.add("reduce-motion");
    // Запомненный язык лирики (ru / en / other) — восстанавливаем при старте.
    const _lp = localStorage.getItem("velora_lyrics_lang");
    if (_lp) state._lyricsLangPref = _lp;
} catch {}
$("#volBtn")?.addEventListener("click", () => {
    audio.muted = !audio.muted;
    setIcon($("#volIcon"), audio.muted ? "mute" : "vol");
});
$("#fpVolBtn")?.addEventListener("click", () => {
    audio.muted = !audio.muted;
});

// Полноэкран
$("#fullBtn").onclick = () => {
    state.fullOpen = true;
    const fp = $("#fullplayer");
    fp.hidden = false;
    // Авто-текст: если выкл. в настройках — скрываем лирику. По умолчанию выкл.
    const autoLyrics = localStorage.getItem("velora_auto_lyrics") === "1";
    fp.classList.toggle("lyrics-hidden", !autoLyrics);
    const _tool = $("#fpToolLyrics"); if (_tool) _tool.classList.toggle("active", autoLyrics);
    const _tgl = $("#fpLyricsToggle"); if (_tgl) _tgl.classList.toggle("active", autoLyrics);
    // Подсказка: первые 2.5с показываем сикбар + контролы (как Я.Музыка/Spotify),
    // чтобы пользователь сразу увидел, где они находятся и куда наводить.
    fp.classList.add("show-hint");
    clearTimeout(fp._hintTimer);
    fp._hintTimer = setTimeout(() => fp.classList.remove("show-hint"), 2500);
    updateLyricsLayout();
};
$("#fullClose").onclick = () => { state.fullOpen = false; $("#fullplayer").hidden = true; };
$("#fpHotClose")?.addEventListener("click", () => { state.fullOpen = false; $("#fullplayer").hidden = true; });
$("#np-cover").onclick = () => $("#fullBtn").click();

// === MOBILE: тап по любому месту мини-плеера (кроме play/next) → fullplayer ===
(function () {
    const playerEl = document.getElementById("player");
    if (!playerEl) return;
    playerEl.addEventListener("click", (e) => {
        if (window.matchMedia("(min-width: 901px)").matches) return;
        // игнорируем клики по интерактивным детям (play/next/etc.)
        if (e.target.closest("button, input, .icon-btn, .play-btn")) return;
        // открыть fullplayer
        const btn = document.getElementById("fullBtn");
        if (btn) btn.click();
    }, { passive: true });
})();

// === MOBILE: обновление --mini-progress (тонкая полоска поверх мини-плеера) ===
(function () {
    const audio = document.getElementById("audio");
    const playerEl = document.getElementById("player");
    if (!audio || !playerEl) return;
    function tick() {
        if (audio.duration && isFinite(audio.duration)) {
            const pct = Math.max(0, Math.min(100, (audio.currentTime / audio.duration) * 100));
            playerEl.style.setProperty("--mini-progress", pct.toFixed(2) + "%");
        }
    }
    audio.addEventListener("timeupdate", tick);
    audio.addEventListener("loadedmetadata", tick);
    audio.addEventListener("seeked", tick);
})();

// === MOBILE: swipe-down на fullplayer → закрыть ===
(function () {
    const fp = document.getElementById("fullplayer");
    if (!fp) return;
    let startY = null, lastY = null, startT = 0, dragging = false;
    fp.addEventListener("touchstart", (e) => {
        if (window.matchMedia("(min-width: 901px)").matches) return;
        if (e.touches.length !== 1) return;
        // не запускаем свайп с input/range, чтобы не ломать сикбар
        if (e.target.closest('input[type="range"], .fp-seek')) return;
        startY = e.touches[0].clientY;
        lastY = startY;
        startT = Date.now();
        dragging = true;
        fp.style.transition = "none";
    }, { passive: true });
    fp.addEventListener("touchmove", (e) => {
        if (!dragging || startY == null) return;
        const y = e.touches[0].clientY;
        const dy = y - startY;
        lastY = y;
        if (dy > 0) {
            fp.style.transform = `translateY(${dy}px)`;
            fp.style.opacity = String(Math.max(0.4, 1 - dy / 600));
        }
    }, { passive: true });
    function endDrag() {
        if (!dragging) return;
        dragging = false;
        const dy = (lastY ?? startY) - startY;
        const dt = Date.now() - startT;
        const fast = dt < 350 && dy > 60;
        const far = dy > 140;
        fp.style.transition = "transform .22s ease, opacity .22s ease";
        if (fast || far) {
            fp.style.transform = "translateY(100%)";
            fp.style.opacity = "0";
            setTimeout(() => {
                state.fullOpen = false;
                fp.hidden = true;
                fp.style.transform = "";
                fp.style.opacity = "";
                fp.style.transition = "";
            }, 200);
        } else {
            fp.style.transform = "";
            fp.style.opacity = "";
        }
        startY = null; lastY = null;
    }
    fp.addEventListener("touchend", endDrag, { passive: true });
    fp.addEventListener("touchcancel", endDrag, { passive: true });
})();

// + в плейлист — мини-плеер и фуллплеер.
function _addCurrentToPlaylist(anchor) {
    const t = state.track;
    if (!t) return showToast("Нет активного трека");
    openTrackMenu(t, anchor);
}
$("#npAddBtn")?.addEventListener("click", (e) => { e.stopPropagation(); _addCurrentToPlaylist(e.currentTarget); });
$("#fpAddBtn")?.addEventListener("click", (e) => { e.stopPropagation(); _addCurrentToPlaylist(e.currentTarget); });

// Мобильное меню «⋯»: дублирует доп.действия мини-плеера на телефоне.
const _npMoreBtn = $("#npMoreBtn");
const _npMorePop = $("#npMorePop");
function _closeNpMore() {
    if (!_npMorePop) return;
    _npMorePop.hidden = true;
    _npMoreBtn?.setAttribute("aria-expanded", "false");
}
function _toggleNpMore() {
    if (!_npMorePop) return;
    const willOpen = _npMorePop.hidden;
    _npMorePop.hidden = !willOpen;
    _npMoreBtn?.setAttribute("aria-expanded", willOpen ? "true" : "false");
    if (willOpen) {
        // Подсветка активных пунктов: лайк/скачивание состояния.
        const t = state.track;
        const liked = t && state.likedKeys && state.likedKeys.has(trackKey(t));
        _npMorePop.querySelector('[data-mp="add"]')?.classList.toggle("is-active", !!liked);
        const dlActive = t && state.offline?.downloaded?.has(trackKey(t));
        _npMorePop.querySelector('[data-mp="dl"]')?.classList.toggle("is-active", !!dlActive);
    }
}
_npMoreBtn?.addEventListener("click", (e) => { e.stopPropagation(); _toggleNpMore(); });
document.addEventListener("click", (e) => {
    if (!_npMorePop || _npMorePop.hidden) return;
    if (_npMorePop.contains(e.target) || _npMoreBtn?.contains(e.target)) return;
    _closeNpMore();
});
_npMorePop?.addEventListener("click", (e) => {
    const it = e.target.closest(".np-more-item"); if (!it) return;
    const act = it.dataset.mp;
    _closeNpMore();
    if (act === "add") _addCurrentToPlaylist(_npMoreBtn);
    else if (act === "dl") $("#dlBtn")?.click();
    else if (act === "lyrics") $("#lyricsBtn")?.click();
    else if (act === "queue") $("#queueBtn")?.click();
    else if (act === "full") $("#fullBtn")?.click();
});

// Полноэкран: клик по названию трека → закрыть фуллплеер и открыть страницу/поиск трека.
$("#fp-track-name")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const t = state.track; if (!t) return;
    state.fullOpen = false; $("#fullplayer").hidden = true;
    if (t.album_id) return openAlbum(t.album_id);
    $("#q").value = ((t.artist||"") + " " + (t.title||"")).trim();
    navigate("search");
});

// Кнопки скачивания: мини-плеер + полноэкран.
async function dlCurrentTrack() {
    const t = state.track; if (!t) return showToast("Нет активного трека");
    const k = (t.source||"")+"|"+(t.source_id||"");
    if (state.offline.downloaded.has(k)) {
        if (!confirm("Удалить «" + (t.title||"трек") + "» из загрузок?")) return;
        await deleteDownload(t);
        showToast("Удалено из загрузок");
    } else {
        showToast("Скачиваем…");
        try { await downloadTrack(t); showToast("Сохранено в офлайн"); }
        catch (e) { showToast("Не удалось: " + (e?.message || e)); return; }
    }
    renderNowPlaying();
    refreshTrackRows();
}
$("#dlBtn")?.addEventListener("click", dlCurrentTrack);
$("#fpToolDownload")?.addEventListener("click", dlCurrentTrack);
// Клик по названию трека в нижнем плеере → открыть АЛЬБОМ (не артиста).
$("#np-title").addEventListener("click", (e) => {
    e.stopPropagation();
    const t = state.track; if (!t) return;
    if (t.album_id) return openAlbum(t.album_id);
    // Фолбэк — поиск по «Артист Название».
    $("#q").value = ((t.artist||"") + " " + (t.title||"")).trim();
    navigate("search");
});
$("#np-artist").addEventListener("click", (e) => {
    e.stopPropagation();
    const t = state.track; if (!t) return;
    // Если кликнули по конкретному chip артиста — открываем его, иначе главного.
    const chip = e.target.closest(".np-artist-chip");
    if (chip && chip.dataset.artistId) {
        return openArtist(t.source||"deezer", chip.dataset.artistId, chip.textContent.trim());
    }
    if (chip && chip.textContent.trim()) {
        $("#q").value = chip.textContent.trim(); return navigate("search");
    }
    if (!t.artist) return;
    if (t.artist_id) openArtist(t.source||"deezer", t.artist_id, t.artist);
    else { $("#q").value = t.artist; navigate("search"); }
});
$("#fp-track-artist").addEventListener("click", (e) => {
    e.stopPropagation();
    const t = state.track; if (!t || !t.artist) return;
    const chip = e.target.closest(".np-artist-chip");
    state.fullOpen = false; $("#fullplayer").hidden = true;
    if (chip && chip.dataset.artistId) {
        return openArtist(t.source||"deezer", chip.dataset.artistId, chip.textContent.trim());
    }
    if (chip && chip.textContent.trim()) {
        $("#q").value = chip.textContent.trim(); return navigate("search");
    }
    if (t.artist_id) openArtist(t.source||"deezer", t.artist_id, t.artist);
    else { $("#q").value = t.artist; navigate("search"); }
});

// fp-cover hover tools
$("#fpToolQueue")?.addEventListener("click", () => toggleQueue());
$("#fpToolLyrics")?.addEventListener("click", () => toggleFpLyrics());
$("#fpToolEq")?.addEventListener("click", () => openEq());
$("#fpToolDislike")?.addEventListener("click", () => state.track && toggleDislike(state.track));

// На тач-устройствах: тап по обложке открывает/закрывает панель инструментов
// (queue/lyrics/dl/eq/dislike). Раньше панель висела всегда и пользователь
// принимал её за «дублирующую нижнюю панель плеера».
(function setupFpCardTouchTools() {
    const card = document.querySelector("#fullplayer .fp-card");
    if (!card) return;
    card.addEventListener("click", (e) => {
        if (!document.body.classList.contains("is-touch")) return;
        // Если кликнули по самому инструменту — не переключаем (он сработает сам).
        if (e.target.closest(".fp-tool, .fp-corner")) return;
        card.classList.toggle("tools-shown");
    });
    document.addEventListener("click", (e) => {
        if (!document.body.classList.contains("is-touch")) return;
        if (!card.classList.contains("tools-shown")) return;
        if (!card.contains(e.target)) card.classList.remove("tools-shown");
    });
})();

// fp three-dot menu
function closeFpMore() { const m = $("#fpMoreMenu"); if (m) m.hidden = true; }
$("#fpMoreBtn").onclick = (e) => {
    e.stopPropagation();
    const m = $("#fpMoreMenu"); if (!m) return;
    m.hidden = !m.hidden;
    const ff = $("#fpLyricsFollow"); if (ff) ff.checked = !!state.lyricsFollow;
};
document.addEventListener("click", (e) => {
    const m = $("#fpMoreMenu"); if (!m || m.hidden) return;
    if (!m.contains(e.target) && !$("#fpMoreBtn").contains(e.target)) m.hidden = true;
});
$("#fpLyricsFollow")?.addEventListener("change", (e) => {
    state.lyricsFollow = e.target.checked;
    showToast(state.lyricsFollow ? "Текст будет следовать за музыкой" : "Авто-прокрутка отключена");
});
$("#fpMoreMenu")?.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mm]"); if (!b) return;
    const act = b.dataset.mm;
    const t = state.track;
    if (act === "goto-artist" && t) {
        if (t.artist_id) openArtist(t.source||"deezer", t.artist_id, t.artist);
        else { $("#q").value = t.artist || ""; navigate("search"); }
        state.fullOpen = false; $("#fullplayer").hidden = true; closeFpMore();
    } else if (act === "share" && t) {
        const url = location.origin + "/?play=" + encodeURIComponent((t.source||"")+":"+(t.source_id||""));
        navigator.clipboard?.writeText(url); showToast("Ссылка скопирована"); closeFpMore();
    } else if (act === "dislike" && t) {
        toggleDislike(t); closeFpMore();
    }
});

// hover tooltip с временем над seek-баром
function attachSeekTooltip(seekEl, tipEl) {
    if (!seekEl) return;
    if (!tipEl) {
        tipEl = document.createElement("div");
        tipEl.className = seekEl.id === "fp-seek" ? "fp-seek-tooltip" : "seek-tooltip";
        tipEl.hidden = true;
        seekEl.parentElement.appendChild(tipEl);
    }
    // Для нового fp-seek родитель .fp-seek-track имеет position:relative —
    // tooltip позиционируется относительно него (left=координата курсора).
    const onMove = (ev) => {
        if (!state.duration) return;
        const r = seekEl.getBoundingClientRect();
        const rel = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
        const sec = rel * state.duration;
        tipEl.textContent = fmtTime(sec);
        const parent = tipEl.parentElement;
        const pr = parent.getBoundingClientRect();
        tipEl.style.left = (ev.clientX - pr.left) + "px";
        tipEl.hidden = false;
    };
    seekEl.addEventListener("mousemove", onMove);
    seekEl.addEventListener("mouseleave", () => { tipEl.hidden = true; });
}
attachSeekTooltip($("#seek"), $("#seekTooltip"));
attachSeekTooltip($("#fp-seek"), $("#fpSeekTooltip"));

async function toggleDislike(t) {
    if (!state.me) return openAuth();
    const k = (t.source||"")+"|"+(t.source_id||"");
    state.dislikedTrackKeys = state.dislikedTrackKeys || new Set();
    try {
        if (state.dislikedTrackKeys.has(k)) {
            await api("/api/dislikes", { method: "DELETE", body: { source: t.source, id: t.source_id } });
            state.dislikedTrackKeys.delete(k);
            showToast("Убрано из дизлайков");
        } else {
            await api("/api/dislikes", { method: "POST", body: t });
            state.dislikedTrackKeys.add(k);
            showToast("Не покажется в волне");
            // сразу перейти к следующему треку
            if (state.track && state.track.source_id === t.source_id) playNext();
        }
        $("#fpToolDislike")?.classList.toggle("active", state.dislikedTrackKeys.has(k));
        updateDislikeBtn();
    } catch (e) { showToast(e.message); }
}

async function toggleDownload(t, rowEl) {
    const k = trackKey(t);
    try {
        if (state.offline.downloaded.has(k)) {
            await deleteDownload(t);
            showToast("Удалено из загрузок");
        } else {
            const btn = rowEl ? rowEl.querySelector("[data-act=download]") : null;
            if (btn) { btn.disabled = true; btn.classList.add("is-loading"); }
            try {
                await downloadTrack(t, (frac) => {
                    if (btn) btn.style.setProperty("--p", Math.round((frac||0) * 100) + "%");
                });
                showToast("Скачано в офлайн");
            } finally {
                if (btn) { btn.disabled = false; btn.classList.remove("is-loading"); btn.style.removeProperty("--p"); }
            }
        }
        // Обновляем иконку в DOM
        if (rowEl) {
            const btn = rowEl.querySelector("[data-act=download]");
            const downloaded = state.offline.downloaded.has(k);
            if (btn) {
                btn.classList.toggle("is-downloaded", downloaded);
                btn.title = downloaded ? "Удалить из загрузок" : "Скачать в офлайн";
                const u = btn.querySelector("use");
                if (u) u.setAttribute("href", "#i-" + (downloaded ? "check" : "download"));
            }
        }
    } catch (e) { showToast("Не удалось скачать: " + e.message); }
}

// Очередь
const toggleQueue = () => { state.queueOpen = !state.queueOpen; $("#queuePanel").classList.toggle("open", state.queueOpen); if (state.queueOpen) renderQueue(); };
$("#queueBtn").onclick = toggleQueue; $("#fpQueueBtn").onclick = toggleQueue;
$("#queueClose").onclick = () => { state.queueOpen = false; $("#queuePanel").classList.remove("open"); };
function renderQueue() {
    const body = $("#queueBody");
    if (!state.queue.length) { body.innerHTML = `<div class="hint">Очередь пуста.</div>`; return; }
    body.innerHTML = state.queue.map((t, i) => `
        <div class="queue-item ${i===state.qi?'playing':''}" data-i="${i}">
            <img src="${t.album_cover||""}" alt="">
            <div class="meta">
                <div class="ttl">${escapeHtml(t.title)}${t.explicit?' <span class="explicit-badge">E</span>':''}</div>
                <div class="art">${escapeHtml(t.artist)}</div>
            </div>
            <span class="t-dur">${fmtTime(t.duration)}</span>
        </div>`).join("");
    body.onclick = (e) => {
        const it = e.target.closest("[data-i]"); if (!it) return;
        state.qi = Number(it.dataset.i); playCurrent();
    };
}

// =================================================================
// LIKES
// =================================================================
async function toggleLike(t) {
    if (offlineBlocked("Лайк")) return;
    if (!state.me) return openAuth();
    const k = (t.source||"")+"|"+(t.source_id||"");
    try {
        if (state.likedKeys.has(k)) {
            await api("/api/likes", { method: "DELETE", body: { source: t.source, id: t.source_id }});
            state.likedKeys.delete(k); state.likedById.delete(k);
        } else {
            await api("/api/likes", { method: "POST", body: t });
            state.likedKeys.add(k); state.likedById.set(k, t);
            // Авто-скачивание лайков: если включено в настройках — ставим фоновую загрузку
            // в офлайн. Не блокируем лайк, ошибки скачивания гасим тихо — лайк важнее.
            if (localStorage.getItem("velora_auto_dl_likes") === "1") {
                if (!state.offline?.downloaded?.has(trackKey(t))) {
                    (async () => {
                        try {
                            await downloadTrack(t);
                            // Не спамим тостами при массовых лайках — показываем один на трек.
                            showToast("Сохранёно в офлайн: " + (t.title || "трек"));
                        } catch (err) {
                            console.warn("[auto-dl] failed:", err);
                        }
                    })();
                }
            }
        }
        refreshLikedUI();
    } catch (e) { showToast(e.message); }
}
function refreshLikedUI() {
    if (state.track) {
        const k = (state.track.source||"")+"|"+(state.track.source_id||"");
        const liked = state.likedKeys.has(k);
        $("#likeBtn").classList.toggle("is-liked", liked);
        setIcon($("#likeBtn").querySelector(".ic"), liked?"heart-fill":"heart");
        $("#fpLikeBtn").classList.toggle("is-liked", liked);
        setIcon($("#fpLikeIcon"), liked?"heart-fill":"heart");
    }
    $$(".track-row").forEach(r => {
        const k = r.dataset.key;
        const liked = state.likedKeys.has(k);
        const btn = r.querySelector("[data-act=like]"); if (!btn) return;
        btn.classList.toggle("is-liked", liked);
        setIcon(btn.querySelector(".ic"), liked?"heart-fill":"heart");
    });
}

// =================================================================
// LYRICS
// =================================================================
async function loadLyrics() {
    state.lyrics = null; state.lyricsActive = -1;
    // Сбрасываем кэш _lastIdx у обоих контейнеров, иначе при смене трека
    // прошлая «passed»-разметка остаётся и подсветка прыгает.
    const _kb = $("#karaokeBody"); if (_kb) _kb._lastIdx = -1;
    const _fl = $("#fpLyrics"); if (_fl) _fl._lastIdx = -1;
    // Пока идёт запрос — показываем лоадер, а не «текст не найден»
    const t = state.track; if (!t) { renderLyrics(); return; }
    const body = $("#karaokeBody"); const fp = $("#fpLyrics");
    // В офлайне — тексты недоступны, не пилим сеть.
    if (isOfflineMode()) { renderLyrics(); return; }
    const loadingHtml = `<div class="lyrics-loading"><div class="dots"><span></span><span></span><span></span><span></span></div></div>`;
    if (body) body.innerHTML = loadingHtml;
    if (fp) fp.innerHTML = loadingHtml;
    // Защита от гонки: если пока шёл запрос, пользователь переключил трек,
    // ответ не должен перезатереть текущий state.lyrics.
    const myGen = state._playGen;
    state._lyricsKey = (t.source||"")+"|"+(t.source_id||"")+"|"+(t.title||"")+"|"+(t.artist||"");
    const wantKey = state._lyricsKey;
    let res = null;
    try {
        res = await api(`/api/lyrics?artist=${encodeURIComponent(t.artist||"")}&title=${encodeURIComponent(t.title||"")}&duration=${t.duration||0}`, { silent: true, silent404: true });
    } catch {}
    if (myGen !== state._playGen || wantKey !== state._lyricsKey) return;
    const cur = state.track;
    if (!cur || (cur.source_id||"") !== (t.source_id||"") || (cur.title||"") !== (t.title||"")) return;
    state.lyrics = res;
    // Стартовый язык: пользовательский preferences > primary > первый доступный.
    if (res && res.variants && Object.keys(res.variants).length) {
        const langs = Object.keys(res.variants);
        const pref = state._lyricsLangPref;
        if (pref && langs.includes(pref)) state._lyricsLang = pref;
        else state._lyricsLang = res.primary || langs[0];
    } else {
        state._lyricsLang = "";
    }
    renderLyrics();
}
// Возвращает текущий активный variant с учётом выбранного языка.
function _activeLyricsVariant() {
    const L = state.lyrics; if (!L) return null;
    if (L.variants && state._lyricsLang && L.variants[state._lyricsLang]) {
        return L.variants[state._lyricsLang];
    }
    // Совместимость со старым форматом ответа.
    return { lines: L.lines || [], synced: !!L.synced, plain: L.plain || "", auto_synced: !!L.auto_synced };
}
// Регэксп для распознавания заголовков секций: [Verse 1: Eminem], [Hook], [Куплет 2: Сява]
const _SECTION_HEAD_RX = /^\s*\[[^\]]+\]\s*$/;
function renderLyrics() {
    const body = $("#karaokeBody"); const fp = $("#fpLyrics");
    if (!body && !fp) return;
    const variant = _activeLyricsVariant();
    const lines = (variant && Array.isArray(variant.lines)) ? variant.lines : [];
    const plain = (variant && variant.plain) ? variant.plain : "";
    const hasContent = lines.length > 0 || plain.trim().length > 0;
    if (!variant || !hasContent) {
        if (body) body.innerHTML = `<div class="muted">Текст не найден.</div>`;
        if (fp) fp.innerHTML = `<div class="fp-lyrics-empty"><div class="dots"><span></span><span></span><span></span><span></span></div></div>`;
        // Скрываем переключатель: для этого трека вариантов нет.
        const _kbar = $("#karaokeLangBar"); if (_kbar) { _kbar.hidden = true; _kbar.innerHTML = ""; }
        const _fbar = $("#fpLangBar"); if (_fbar) { _fbar.hidden = true; _fbar.innerHTML = ""; }
        updateLyricsLayout();
        return;
    }
    const previewMode = !!state._isPreview;
    // Бейдж «Авто-синхронизация» — если строки расставлены приблизительно.
    const autoBadge = (variant.auto_synced)
        ? `<div class="lrc-auto-badge" title="Текст не размечен по времени — синхронизация приблизительная">≈ авто-синхронизация</div>`
        : "";

    // Хелпер: рендерит строку или заголовок секции.
    const renderOne = (l, i) => {
        const text = l.text || "";
        if (_SECTION_HEAD_RX.test(text)) {
            // Заголовок: [Verse 1: Eminem] — без таймстемпа на клик, просто разделитель.
            return `<div class="lrc-section" data-t="${l.t}">${escapeHtml(text)}</div>`;
        }
        if (!text.trim()) {
            return `<div class="lrc-line lrc-empty" data-i="${i}" data-t="${l.t}">&nbsp;</div>`;
        }
        return `<div class="lrc-line" data-i="${i}" data-t="${l.t}">${escapeHtml(text)}</div>`;
    };

    if (variant.synced && lines.length > 0 && !previewMode) {
        const html = autoBadge + lines.map(renderOne).join("");
        if (body) body.innerHTML = html;
        if (fp) fp.innerHTML = html;
    } else {
        // Полностью без синхры: рендерим plain как passed-строки (читать можно, но без подсветки).
        const text = (plain || lines.map(l=>l.text).join("\n"));
        const renderPlain = (s) => {
            if (_SECTION_HEAD_RX.test(s)) return `<div class="lrc-section">${escapeHtml(s)}</div>`;
            return `<div class="lrc-line passed">${escapeHtml(s)}</div>`;
        };
        const html = text.split("\n").map(renderPlain).join("");
        if (body) body.innerHTML = html;
        if (fp) fp.innerHTML = html;
    }
    // Языковые переключатели — в ОТДЕЛЬНЫх контейнерах (не внутри скролл-зоны),
    // чтобы не уезжали вверх вместе с текстом и не сливались с лирикой.
    _renderLangBar($("#karaokeLangBar"));
    _renderLangBar($("#fpLangBar"));
    const seekFromLine = (e) => {
        // Клик по бейджу — игнорируем seek.
        if (e.target.closest(".lrc-auto-badge")) return;
        const line = e.target.closest("[data-t]"); if (!line) return;
        const t = parseFloat(line.dataset.t);
        if (Number.isFinite(t)) {
            try { audio.currentTime = t; audio.play().catch(()=>{}); } catch {}
        }
    };
    if (body) body.onclick = seekFromLine;
    if (fp) fp.onclick = seekFromLine;
    // Привязываем детектор пользовательского скролла (1.5с пауза автоследования).
    attachLyricsScrollDetector(body);
    attachLyricsScrollDetector(fp);
    updateLyricsLayout();
}
// Рендерит языковые кнопки во внешнем контейнере (над лирикой).
function _renderLangBar(bar) {
    if (!bar) return;
    const L = state.lyrics;
    const variants = L && L.variants ? L.variants : null;
    const langs = variants ? Object.keys(variants) : [];
    if (langs.length < 2) {
        bar.hidden = true; bar.innerHTML = ""; return;
    }
    const labels = { ru: "Рус", en: "Eng", other: "Ориг" };
    const cur = state._lyricsLang;
    bar.hidden = false;
    bar.innerHTML = langs.map(l =>
        `<button class="lrc-lang-btn ${l===cur?'is-active':''}" data-lang="${l}" type="button">${labels[l]||l.toUpperCase()}</button>`
    ).join("");
    bar.querySelectorAll(".lrc-lang-btn").forEach(btn => {
        btn.onclick = (e) => {
            e.stopPropagation();
            const lang = btn.dataset.lang;
            if (!lang || lang === state._lyricsLang) return;
            state._lyricsLang = lang;
            state._lyricsLangPref = lang;
            try { localStorage.setItem("velora_lyrics_lang", lang); } catch {}
            // Полный сброс sync-состояния, иначе подсветка багается после смены.
            state.lyricsActive = -1;
            const _kb = $("#karaokeBody"); if (_kb) { _kb._lastIdx = -1; _kb._userScrollAt = 0; }
            const _fl = $("#fpLyrics"); if (_fl) { _fl._lastIdx = -1; _fl._userScrollAt = 0; }
            renderLyrics();
            // Два вызова: один сразу (подсветить), второй на след. фрейме (после
            // перерисовки DOM, чтобы автоскролл сработал).
            syncLyrics();
            requestAnimationFrame(syncLyrics);
        };
    });
}
function updateLyricsLayout() {
    const fp = $("#fullplayer");
    if (!fp) return;
    const v = _activeLyricsVariant();
    fp.classList.toggle("no-lyrics", !v || (!v.synced && !v.plain));
}
function syncLyrics() {
    const variant = _activeLyricsVariant();
    if (!variant || !variant.synced || !variant.lines) return;
    // Секции ([Verse: ...]) — это разделители, а не «строки для подсветки».
    // Чтобы idx совпадал с индексом DOM-нод .lrc-line (которые создаются только
    // для НЕ-секций), фильтруем их здесь же.
    const lines = variant.lines.filter(l => !_SECTION_HEAD_RX.test(l.text || ""));
    if (!lines.length) return;
    // Микро-опережение: 50мс (≈ 1 кадр), чтобы строка переключалась
    // ровно на её таймкоде, а не «бежала» впереди аудио. Раньше было
    // 0.2с — заметно расходилось у пользователей с короткими треками
    // (lazzy 2wice и пр.) где плотность строк высокая.
    const cur = audio.currentTime + 0.05;
    let idx = -1;
    for (let i = 0; i < lines.length; i++) if (lines[i].t <= cur) idx = i; else break;
    const idxChanged = (idx !== state.lyricsActive);
    if (idxChanged) state.lyricsActive = idx;
    const follow = state.karaokeFollow !== false;
    const now = performance.now();
    // ВАЖНО: проходим каждый контейнер отдельно — общий $$ объединял индексы
    // двух списков (karaoke + fullscreen), и в фуллскрине подсветка не срабатывала.
    const apply = (root) => {
        if (!root) return;
        const els = root.querySelectorAll(".lrc-line");
        // Снимаем active только с предыдущей активной (а не со всех — иначе
        // браузер ререндерит сотни нод каждые 50мс и DOM лагает).
        if (idxChanged) {
            const prevActive = root.querySelector(".lrc-line.active");
            if (prevActive) prevActive.classList.remove("active");
            // Помечаем passed только новые строки между prevIdx и idx.
            const prevIdx = root._lastIdx ?? -1;
            if (idx > prevIdx) {
                for (let i = prevIdx + 1; i <= idx; i++) {
                    if (els[i]) els[i].classList.add("passed");
                }
            } else if (idx < prevIdx) {
                for (let i = idx + 1; i <= prevIdx; i++) {
                    if (els[i]) els[i].classList.remove("passed");
                }
            }
            root._lastIdx = idx;
            const target = els[idx];
            if (target) target.classList.add("active");
        }
        // Авто-скролл (даже если индекс не сменился — на случай возврата
        // после ручного скролла пользователя).
        const target = els[idx];
        if (!target || !follow) return;
        // Если пользователь недавно скроллил вручную — пауза 1.5с.
        const lastUserScroll = root._userScrollAt || 0;
        if (now - lastUserScroll < 1500) return;
        // Плавный скролл только если строка вышла из видимой зоны
        // (иначе scrollIntoView перебивает предыдущую анимацию и текст «дёргается»).
        const r = target.getBoundingClientRect();
        const rr = root.getBoundingClientRect();
        const margin = rr.height * 0.25;
        if (r.top < rr.top + margin || r.bottom > rr.bottom - margin) {
            // Помечаем что следующий scroll-event — наш программный, не пользовательский.
            root._programmaticScroll = now;
            target.scrollIntoView({ block: "center", behavior: "smooth" });
        }
    };
    apply($("#karaokeBody"));
    apply($("#fpLyrics"));
}

// Привязываем детектор пользовательского скролла один раз (idempotent).
function attachLyricsScrollDetector(root) {
    if (!root || root._lyricsScrollBound) return;
    root._lyricsScrollBound = true;
    root.addEventListener("scroll", () => {
        const now = performance.now();
        // Если этот scroll-event инициирован программой (scrollIntoView),
        // не считаем его пользовательским. Программные скроллы могут «эхом»
        // прийти в течение ~600мс из-за smooth-анимации.
        if (root._programmaticScroll && now - root._programmaticScroll < 600) return;
        root._userScrollAt = now;
    }, { passive: true });
    // Также реагируем на wheel/touch — самый надёжный сигнал «человек крутит».
    const mark = () => { root._userScrollAt = performance.now(); };
    root.addEventListener("wheel", mark, { passive: true });
    root.addEventListener("touchstart", mark, { passive: true });
    root.addEventListener("touchmove", mark, { passive: true });
}
$("#lyricsBtn").onclick = () => { state.karaokeOpen = !state.karaokeOpen; $("#karaoke").classList.toggle("open", state.karaokeOpen); };
$("#closeKaraoke").onclick = () => { state.karaokeOpen = false; $("#karaoke").classList.remove("open"); };
// В полноэкранном режиме «Текст» прячет/показывает правую колонку с лирикой,
// а НЕ открывает боковую karaoke-панель (которая дублировала бы лирику).
function toggleFpLyrics() {
    const fp = $("#fullplayer"); if (!fp) return;
    const hidden = fp.classList.toggle("lyrics-hidden");
    const tool = $("#fpToolLyrics"); if (tool) tool.classList.toggle("active", !hidden);
    const tgl = $("#fpLyricsToggle"); if (tgl) tgl.classList.toggle("active", !hidden);
}
$("#fpLyricsToggle").onclick = toggleFpLyrics;

// =================================================================
// EQ — ВРЕМЕННО ОТКЛЮЧЁН (заглушки)
// =================================================================
// Полная функциональность эквалайзера (BiquadFilter-цепочка через
// captureStream, визуализация, пресеты, сохранение настроек) удалена/застаблена,
// потому что:
//   - Web Audio Graph (captureStream + AudioContext.resume) дают рассинхрон
//     состояний при перезагрузке → периодическая тишина при «размученной» иконке.
//   - Кастомные вертикальные слайдеры в Chromium ведут себя непредсказуемо.
// При вызове любой EQ-точки входа показываем модалку «Скоро…».
// Когда вернём — раскомментировать оригинальный блок ниже (отмечен EQ-LEGACY).
// Все функции-заглушки сохранены, чтобы остальной код (вызывы applyEqRoute из
// громкости и пр.) продолжал работать без изменений.
// -----------------------------------------------------------------
function showComingSoon(title) {
    const t = String(title || "Эта функция");
    showToast(t + " — скоро…");
}
// Заглушки: всегда no-op, EQ всегда выключен.
function ensureAudioGraph() { return false; }
function applyEqRoute()    { try { audio.muted = false; } catch {} }
function applyEq()         { /* no-op: EQ disabled */ }
function buildEqUI()       { /* no-op: EQ disabled */ }
function updateEqBand()    { /* no-op: EQ disabled */ }
function startEqVis()      { /* no-op: EQ disabled */ }
function teardownAudioGraph() { /* no-op: EQ disabled */ }
function saveEqPrefs()     { /* no-op: EQ disabled */ }
async function resumeAudioCtx() { /* no-op: EQ disabled */ }
// Точка входа из шапки/полноэкрана/настроек — показываем «Скоро…».
function openEq() {
    showComingSoon("Эквалайзер");
}
// Гарантированно держим EQ выключённым.
state.eq.enabled = false;
// Привязки кнопок (заглушка вместо реальной модалки).
$("#eqBtn") && ($("#eqBtn").onclick = openEq);
// Слушатели на элементы EQ-модалки больше не нужны — их в шаблоне нет смысла дёргать,
// но если они существуют, защищаемся через optional chaining.
$("#eqEnabled")?.addEventListener("change", openEq);
$("#eqPreset")?.addEventListener("change", openEq);
$("#eqModal")?.addEventListener("click", e => {
    if (e.target.closest("[data-close]") || e.target === e.currentTarget) {
        $("#eqModal").hidden = true;
    }
});

/* === EQ-LEGACY (оригинальный код, оставлен на будущее, не выполняется) ======
const EQ_FREQS = [25, 40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000, 16000];
const EQ_PRESETS = {
    flat:       [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    bass:       [7,6,6,5,4,3,2,1,0,-1,-1,-1,0,0,0],
    vocal:      [-3,-2,-2,-1,0,1,2,3,4,4,3,2,1,0,-1],
    rock:       [5,4,3,2,1,0,-1,-1,0,1,2,3,3,4,4],
    electronic: [6,5,4,3,1,0,-1,-2,-1,0,1,2,2,3,4],
    acoustic:   [3,3,3,2,2,1,1,1,2,2,2,2,1,1,0],
    hiphop:     [6,6,5,4,3,1,-1,-1,0,1,2,2,3,3,3]
};
const EQ_RANGE = 12;
// (Остальная реализация удалена — см. историю git.)
============================================================================ */

// =================================================================
// КЛАВИАТУРА (KeyboardEvent.code — на любую раскладку)
// =================================================================
document.addEventListener("keydown", (e) => {
    if (["INPUT","TEXTAREA","SELECT"].includes(e.target.tagName)) return;
    if (e.code === "Space") { e.preventDefault(); audio.paused ? audio.play().catch(()=>{}) : audio.pause(); }
    if (e.code === "ArrowRight" && (e.ctrlKey||e.metaKey)) { e.preventDefault(); playNext(); }
    if (e.code === "ArrowLeft" && (e.ctrlKey||e.metaKey)) { e.preventDefault(); playPrev(); }
    if (e.code === "Escape") {
        if (state.fullOpen) { state.fullOpen = false; $("#fullplayer").hidden = true; }
        else if (state.karaokeOpen) { state.karaokeOpen = false; $("#karaoke").classList.remove("open"); }
    }
});

// =================================================================
// СТАРТ
// =================================================================
(async function init() {
    // ===== iOS Safari: ранние фиксы UA =====
    try {
        const ua = navigator.userAgent || "";
        const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
        const isStandalone = window.matchMedia?.("(display-mode: standalone)").matches || window.navigator.standalone === true;
        if (isIOS) document.body.classList.add("is-ios");
        if (isStandalone) document.body.classList.add("is-standalone");
        // На iOS обязательно playsinline + preload=metadata, иначе аудио уходит в полноэкранный QuickTime.
        const a = document.getElementById("audio");
        if (a) {
            a.setAttribute("playsinline", "");
            a.setAttribute("webkit-playsinline", "");
            a.setAttribute("x-webkit-airplay", "allow");
            try { a.preload = "metadata"; } catch {}
        }
        // Фикс «100vh» на старых iOS, где dvh ещё нет.
        const setVh = () => {
            try { document.documentElement.style.setProperty("--vh", `${window.innerHeight * 0.01}px`); } catch {}
        };
        setVh();
        window.addEventListener("resize", setVh, { passive: true });
        window.addEventListener("orientationchange", setVh, { passive: true });
        // Разблокируем AudioContext по первому жесту (iOS требует user gesture).
        const unlock = () => {
            try {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (Ctx && !window._audioCtxUnlocked) {
                    const c = new Ctx();
                    c.resume?.().catch(()=>{});
                    window._audioCtxUnlocked = true;
                }
                // ВАЖНО: НЕ трогаем audio.muted в unlock — раньше тут было
                //   a.muted = true; a.play().then(()=>{ a.muted = false; }).catch(()=>{});
                // Если play() отклонялся (например, нет src), .then() не вызывался и
                // <audio> навсегда оставался muted=true. Юзер видел «звук есть»,
                // но слышал тишину, пока не дёргал слайдер громкости.
                // На iOS Safari unlock AudioContext'а уже достаточно — muted-фокус не нужен.
            } catch {}
            document.removeEventListener("touchend", unlock, true);
            document.removeEventListener("click", unlock, true);
        };
        document.addEventListener("touchend", unlock, { capture: true, once: false, passive: true });
        document.addEventListener("click", unlock, { capture: true, once: false });
    } catch {}

    await loadMe();
    await offlineLoadIndex();
    applyOfflineUi();
    // Routing: если пришли по /p/<slug> — открываем публичный плейлист.
    const path = location.pathname;
    const m = path.match(/^\/p\/([^/]+)\/?$/);
    const mu = path.match(/^\/u\/([^/]+)\/?$/);
    // PWA shortcut: /?go=<view>
    const goParam = new URLSearchParams(location.search).get("go");
    if (m) {
        try {
            const data = await fetch(`/api/p/${encodeURIComponent(m[1])}`).then(r => {
                if (r.status === 404) throw new Error("not_found");
                if (r.status === 403) throw new Error("private");
                return r.json();
            });
            state.publicPlaylist = data;
            state.currentView = "publicPlaylist";
            renderPublicPlaylist();
        } catch (e) {
            renderNotFound(e.message === "private" ? "Этот плейлист приватный." : null);
        }
    } else if (mu) {
        await openPublicUser(mu[1]);
    } else if (path !== "/" && path !== "/index.html" && !path.startsWith("/static/") && !path.startsWith("/api/")) {
        // Любой другой неизвестный путь → 404 SPA
        renderNotFound();
    } else if (goParam && ["home","wave","charts","library","history","settings","offline"].includes(goParam)) {
        navigate(goParam);
    } else {
        renderView();
    }
    // Service Worker (offline-кэш статики и SPA-каркаса).
    if ("serviceWorker" in navigator && location.protocol !== "file:") {
        try {
            const reg = await navigator.serviceWorker.register("/sw.js");
            // Если найдена устаревшая версия — обновляем сразу и забираем управление.
            try { reg.update?.(); } catch {}
            // Когда новый воркер активен — однократно перезагружаем страницу,
            // чтобы получить свежий index.html и app.js.
            navigator.serviceWorker.addEventListener("controllerchange", () => {
                if (window.__velora_sw_reloaded) return;
                window.__velora_sw_reloaded = true;
                try { location.reload(); } catch {}
            });
        } catch {}
    }
    // Браузерные «Назад/Вперёд» для /u/<slug>, /p/<slug> и обычных вкладок.
    window.addEventListener("popstate", async () => {
        const p = location.pathname;
        const mp = p.match(/^\/p\/([^/]+)\/?$/);
        const mu = p.match(/^\/u\/([^/]+)\/?$/);
        if (mu) return openPublicUser(mu[1]);
        if (mp) {
            try {
                const data = await fetch(`/api/p/${encodeURIComponent(mp[1])}`).then(r => r.json());
                state.publicPlaylist = data;
                state.currentView = "publicPlaylist";
                renderPublicPlaylist();
            } catch { renderNotFound(); }
            return;
        }
        if (p === "/" || p === "/index.html") {
            state.currentView = state.currentView === "publicUser" || state.currentView === "publicPlaylist" ? "home" : (state.currentView || "home");
            renderView();
        }
    });
    // PWA install prompt.
    try { setupInstallPrompt(); } catch {}
    // Помечаем устройство как touch — это используется CSS, чтобы НЕ применять
    // hover-only поведение на мобильных (где hover не существует).
    if (matchMedia("(hover: none)").matches || ("ontouchstart" in window)) {
        document.body.classList.add("is-touch");
    }
})();

// =================================================================
// PWA INSTALL PROMPT
// =================================================================
let _deferredInstall = null;
function setupInstallPrompt() {
    window.addEventListener("beforeinstallprompt", (e) => {
        e.preventDefault();
        _deferredInstall = e;
        // Подсветим кнопку «Установить» в шапке сайта (если есть в настройках) и
        // показываем небольшой тост-приглашение раз в неделю.
        const last = Number(localStorage.getItem("velora_pwa_prompt") || 0);
        if (Date.now() - last > 7 * 24 * 3600 * 1000) {
            localStorage.setItem("velora_pwa_prompt", String(Date.now()));
            showInstallToast();
        }
    });
    window.addEventListener("appinstalled", () => {
        _deferredInstall = null;
        showToast("Velora установлена. Иконка появится на рабочем столе.");
    });
}
function showInstallToast() {
    if (!_deferredInstall) return;
    const t = document.createElement("div");
    t.className = "install-toast";
    t.innerHTML = `
        <div class="install-toast-body">
            <div><b>Установить Velora</b><br><small>Полноэкранное приложение на рабочий стол</small></div>
            <div class="install-toast-actions">
                <button class="btn-secondary" data-act="later">Позже</button>
                <button class="btn-primary" data-act="install">Установить</button>
            </div>
        </div>`;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add("visible"));
    t.addEventListener("click", async (e) => {
        const b = e.target.closest("button"); if (!b) return;
        if (b.dataset.act === "install" && _deferredInstall) {
            try { _deferredInstall.prompt(); await _deferredInstall.userChoice; } catch {}
            _deferredInstall = null;
        }
        t.classList.remove("visible");
        setTimeout(() => t.remove(), 300);
    });
    setTimeout(() => { if (document.body.contains(t)) { t.classList.remove("visible"); setTimeout(()=>t.remove(), 300); } }, 12000);
}
async function triggerInstall() {
    if (!_deferredInstall) {
        // iOS не поддерживает beforeinstallprompt — показываем инструкцию.
        const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
        if (isIOS) {
            showToast("На iOS: кнопка «Поделиться» → «На экран Домой»");
        } else {
            showToast("Установка недоступна — приложение уже установлено или браузер не поддерживает.");
        }
        return;
    }
    try { _deferredInstall.prompt(); await _deferredInstall.userChoice; } catch {}
    _deferredInstall = null;
}
window.veloraInstall = triggerInstall;

function renderNotFound(msg) {
    const v = $("#view");
    v.innerHTML = `
        <div class="not-found">
            <div class="nf-code">404</div>
            <div class="nf-title">${escapeHtml(msg || "Страница не найдена")}</div>
            <div class="nf-sub">Возможно, ссылка устарела или ресурс был удалён.</div>
            <button class="btn-primary" data-go="home">На главную</button>
        </div>`;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) { history.replaceState(null, "", "/"); navigate(g.dataset.go); }
    };
}

function renderPublicPlaylist() {
    const v = $("#view");
    const p = state.publicPlaylist || {};
    const tracks = asTracks(p.items || []);
    const owner = p.owner || {};
    v.innerHTML = `
        <div class="playlist-hero">
            ${p.cover ? `<img class="pl-cover" src="${escapeHtml(p.cover)}" alt="">` : `<div class="pl-cover placeholder"><svg class="ic"><use href="#i-library"/></svg></div>`}
            <div class="pl-info">
                <div class="kicker">Публичный плейлист</div>
                <h1>${escapeHtml(p.name||"")}</h1>
                ${p.description ? `<div class="pl-desc">${escapeHtml(p.description)}</div>` : ""}
                <div class="pl-owner">Автор: <b>${escapeHtml(owner.display_name || owner.username || "—")}</b></div>
                <div class="pl-stats">${tracks.length} ${tracks.length==1?"трек":"треков"}</div>
                <button class="btn-primary" id="ppPlay"><svg class="ic"><use href="#i-play"/></svg> Слушать</button>
            </div>
        </div>
        ${tracks.length ? `<div class="track-list" id="ppList">${tracks.map(trackRowHtml).join("")}</div>` :
            `<div class="hint">Плейлист пуст.</div>`}
    `;
    if (tracks.length) {
        bindTrackList($("#ppList"), tracks);
        $("#ppPlay").onclick = () => playQueue(tracks, 0, { from_view: "playlist" });
    }
}

// =================================================================
// ПУБЛИЧНЫЕ ПРОФИЛИ + ПОДПИСКИ
// =================================================================
// Детерминированный градиент из строки (slug/username) — используется как
// заглушка для аватара / баннера, когда пользователь приватный или фото нет.
function _hashCode(s) {
    let h = 0; s = String(s||"");
    for (let i = 0; i < s.length; i++) { h = ((h<<5) - h) + s.charCodeAt(i); h |= 0; }
    return Math.abs(h);
}
function gradientFromSeed(seed) {
    const h = _hashCode(seed);
    // Монохромный сайт → используем оттенки серого + лёгкий accent-сдвиг.
    const a = (h % 360);
    const b = ((h >> 8) % 360);
    return `linear-gradient(135deg, hsl(${a},22%,18%) 0%, hsl(${b},18%,8%) 100%)`;
}
// CSS url() для data-URI: одинарные кавычки + экранирование одинарных
// внутри (на случай, если что-то прокрадётся). Двойные кавычки нельзя —
// они ломают HTML-атрибут style="...".
function _cssUrl(u) {
    return `url('${String(u).replace(/'/g, "%27")}')`;
}
function _avatarBg(u) {
    if (u.avatar) return `background-image:${_cssUrl(u.avatar)};background-size:cover;background-position:center`;
    return `background:${gradientFromSeed(u.seed||u.slug||u.username||"x")}`;
}
function _bannerBg(u) {
    if (u.banner) return `background-image:${_cssUrl(u.banner)};background-size:cover;background-position:center`;
    return `background:${gradientFromSeed("banner-"+(u.seed||u.slug||u.username||"x"))}`;
}

async function openPublicUser(slug) {
    const v = $("#view");
    v.innerHTML = `<div class="hint">Загружаем профиль…</div>`;
    state.currentView = "publicUser";
    try {
        const data = await fetch(`/api/u/${encodeURIComponent(slug)}`).then(r => {
            if (r.status === 404) throw new Error("not_found");
            return r.json();
        });
        state.publicUser = data;
        // Если это я сам — лучше показать собственную страницу профиля
        if (data.is_self) return navigate("profile");
        renderPublicUser();
    } catch (e) {
        renderNotFound(e.message === "not_found" ? "Профиль не найден." : "Не удалось загрузить профиль.");
    }
}

function renderPublicUser() {
    const v = $("#view");
    const u = state.publicUser || {};
    const isPriv = !!u.is_private;
    const dn = u.display_name || u.username || "—";
    const initial = (dn || "?").trim().charAt(0).toUpperCase();
    const followBtnHtml = state.me && !u.is_self
        ? `<button class="btn-${u.am_following?"secondary":"primary"}" id="upFollow">
              ${u.am_following ? "✓ Вы подписаны" : "Подписаться"}
           </button>`
        : (!state.me ? `<button class="btn-primary" id="upLogin">Войти, чтобы подписаться</button>` : "");
    // Аватарка: если нет → инициал на градиенте
    const avInner = u.avatar ? "" : `<span class="pavatar-letter">${escapeHtml(initial)}</span>`;
    const bioBlock = isPriv
        ? `<div class="pbio muted">Профиль приватный. Описание скрыто.</div>`
        : (u.bio === null
            ? `<div class="pbio muted">Описание скрыто настройками профиля.</div>`
            : `<div class="pbio">${escapeHtml(u.bio || "Пользователь не добавил описание.")}</div>`);
    const metaChips = [];
    if (!isPriv && u.location) metaChips.push(`<span class="pchip">📍 ${escapeHtml(u.location)}</span>`);
    if (!isPriv && u.website) metaChips.push(`<a class="pchip" href="${escapeHtml(u.website)}" target="_blank" rel="noopener">🔗 ${escapeHtml(u.website.replace(/^https?:\/\//,''))}</a>`);
    if (!isPriv && u.dob) metaChips.push(`<span class="pchip" title="${escapeHtml(u.dob)}">🎂 ${escapeHtml(_fmtDob(u.dob))}</span>`);
    const stats = u.stats;
    const statsHtml = stats
        ? `<div class="profile-stats">
              <div class="profile-stat"><div class="num">${stats.likes}</div><div class="lbl">Любимые</div></div>
              <div class="profile-stat"><div class="num">${stats.playlists}</div><div class="lbl">Плейлисты</div></div>
              <div class="profile-stat"><div class="num">${u.followers||0}</div><div class="lbl">Подписчики</div></div>
              <div class="profile-stat"><div class="num">${u.following||0}</div><div class="lbl">Подписки</div></div>
           </div>`
        : `<div class="profile-stats">
              <div class="profile-stat"><div class="num">${u.followers||0}</div><div class="lbl">Подписчики</div></div>
              <div class="profile-stat"><div class="num">${u.following||0}</div><div class="lbl">Подписки</div></div>
           </div>`;
    const playlistsHtml = (u.playlists && u.playlists.length)
        ? `<h2 class="section-title">Публичные плейлисты</h2>
           <div class="cards">${u.playlists.map(p => `
               <div class="card card-playlist" data-pslug="${escapeHtml(p.slug||p.id)}">
                   <div class="card-cover ${p.cover?'':'placeholder'}"${p.cover?` style="background-image:url(${escapeHtml(p.cover)});background-size:cover;background-position:center"`:""}>
                       ${p.cover?"":`<svg class="ic ic-large"><use href="#i-library"/></svg>`}
                   </div>
                   <div class="c-title">${escapeHtml(p.name)}</div>
                   <div class="c-sub">${p.count||0} ${p.count==1?"трек":"треков"}</div>
               </div>`).join("")}</div>`
        : (isPriv ? "" : `<div class="hint">Публичных плейлистов пока нет.</div>`);
    v.innerHTML = `
        <div class="profile-page is-public ${isPriv?"is-private":""}">
            <div class="profile-cover" id="upCover" style="${_bannerBg(u)}">
                ${isPriv?'<div class="cover-hint">Приватный профиль</div>':''}
            </div>
            <div class="profile-card">
                <div class="pavatar" id="upAvatar" style="${_avatarBg(u)}">${avInner}</div>
                <div class="pinfo">
                    <h1>${escapeHtml(dn)}</h1>
                    <div class="phandle">@${escapeHtml(u.username||"")}${isPriv?' · <span class="badge-private">🔒 приватный</span>':''}</div>
                    ${bioBlock}
                    ${metaChips.length?`<div class="pmeta-row">${metaChips.join("")}</div>`:""}
                </div>
                <div class="profile-actions">
                    ${followBtnHtml}
                    <button class="btn-secondary" id="upShare" title="Скопировать ссылку"><svg class="ic"><use href="#i-share"/></svg></button>
                </div>
            </div>
            ${statsHtml}
            <div id="wallMount"></div>
            ${playlistsHtml}
        </div>`;
    $("#upShare").onclick = async () => {
        const url = `${location.origin}/u/${u.slug||u.username}`;
        try { await navigator.clipboard.writeText(url); showToast("Ссылка скопирована"); }
        catch { showToast(url); }
    };
    // Кликабельные аватар и баннер на чужом профиле — открыть в лайтбоксе.
    $("#upAvatar")?.addEventListener("click", () => {
        if (u.avatar) openImageLightbox(u.avatar);
    });
    $("#upCover")?.addEventListener("click", () => {
        if (u.banner) openImageLightbox(u.banner);
    });
    // Стена (комментарии посетителей).
    if (!isPriv) renderWall(u.slug || u.username, $("#wallMount"));
    const fb = $("#upFollow");
    if (fb) fb.onclick = async () => {
        try {
            const method = u.am_following ? "DELETE" : "POST";
            await api(`/api/u/${encodeURIComponent(u.slug||u.username)}/follow`, { method });
            u.am_following = !u.am_following;
            u.followers += u.am_following ? 1 : -1;
            renderPublicUser();
        } catch (e) { showToast(e.message); }
    };
    const lb = $("#upLogin"); if (lb) lb.onclick = () => openAuth();
    v.querySelectorAll("[data-pslug]").forEach(c => {
        c.onclick = () => {
            const slug = c.dataset.pslug;
            history.pushState(null, "", `/p/${slug}`);
            fetch(`/api/p/${encodeURIComponent(slug)}`).then(r => r.json()).then(data => {
                state.publicPlaylist = data;
                state.currentView = "publicPlaylist";
                renderPublicPlaylist();
            }).catch(()=>showToast("Не удалось открыть плейлист"));
        };
    });
}

// =================================================================
// СТРАНИЦА ПОДПИСОК (мои подписки / подписчики)
// =================================================================
async function renderFollowsPage() {
    if (!state.me) return openAuth();
    state.currentView = "follows";
    const v = $("#view");
    const tab = state.followsTab || "following";
    v.innerHTML = `
        <button class="settings-back" data-go="settings">
            <span class="back-circle"><svg class="ic"><use href="#i-chev-left"/></svg></span>
            Подписки
        </button>
        <div class="tabs-row">
            <button class="tab-btn ${tab==="following"?"active":""}" data-tab="following">Я подписан</button>
            <button class="tab-btn ${tab==="followers"?"active":""}" data-tab="followers">Подписчики</button>
        </div>
        <div id="followsList"><div class="hint">Загружаем…</div></div>`;
    v.onclick = (e) => {
        const g = e.target.closest("[data-go]"); if (g) return navigate(g.dataset.go);
        const tb = e.target.closest("[data-tab]");
        if (tb) { state.followsTab = tb.dataset.tab; renderFollowsPage(); }
    };
    try {
        const list = await api(`/api/me/follows?kind=${encodeURIComponent(tab)}`);
        const host = $("#followsList");
        if (!list.length) { host.innerHTML = `<div class="hint">${tab==="following"?"Вы пока ни на кого не подписаны. Откройте чей-то профиль (например, по ссылке /u/&lt;ник&gt;) и нажмите «Подписаться».":"У вас пока нет подписчиков."}</div>`; return; }
        host.innerHTML = `<div class="follows-grid">${list.map(u => `
            <div class="follow-item" data-slug="${escapeHtml(u.slug||u.username)}">
                <div class="follow-av" style="${_avatarBg(u)}">${u.avatar?"":escapeHtml((u.display_name||u.username||"?").charAt(0).toUpperCase())}</div>
                <div class="follow-meta">
                    <div class="follow-name">${escapeHtml(u.display_name||u.username)}${u.is_private?' <span class="badge-private">🔒</span>':''}</div>
                    <div class="follow-sub">@${escapeHtml(u.username||"")}</div>
                </div>
            </div>`).join("")}</div>`;
        host.querySelectorAll("[data-slug]").forEach(el => el.onclick = () => {
            const s = el.dataset.slug;
            history.pushState(null, "", `/u/${s}`);
            openPublicUser(s);
        });
    } catch (e) { $("#followsList").innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
}

// =================================================================
// АВАТАРКА / БАННЕР: 2-шаговая модалка с предпросмотром и кропом
// =================================================================
const UPLOAD_PRESETS = {
    avatar: {
        title: "Аватарка профиля",
        subtitle: "Квадратное изображение",
        recommended: "Рекомендуется <b>512 × 512 px</b><br>Максимум 3 МБ · JPG / PNG / WEBP",
        maxSize: 3 * 1024 * 1024,
        aspect: 1,
        outW: 512, outH: 512,
    },
    banner: {
        title: "Обложка профиля",
        subtitle: "Широкий баннер",
        recommended: "Рекомендуется <b>1500 × 500 px</b><br>Максимум 4 МБ · JPG / PNG / WEBP / GIF",
        maxSize: 4 * 1024 * 1024,
        aspect: 3,
        outW: 1500, outH: 500,
    },
    favCover: {
        title: "Обложка «Мне нравится»",
        subtitle: "Квадратное изображение",
        recommended: "Рекомендуется <b>800 × 800 px</b><br>Максимум 4 МБ · JPG / PNG / WEBP",
        maxSize: 4 * 1024 * 1024,
        aspect: 1,
        outW: 800, outH: 800,
    },
};

function openImageUpload(target) {
    state.uploadTarget = target;
    state.uploadImage = null;
    state._iuCrop = null;
    const m = $("#imageUploadModal");
    if (!m) return showToast("Окно загрузки недоступно");
    const p = UPLOAD_PRESETS[target] || UPLOAD_PRESETS.avatar;
    $("#iuTitle").textContent = p.title;
    $("#iuSubtitle").textContent = p.subtitle;
    $("#iuRecs").innerHTML = p.recommended;
    // ВАЖНО: восстанавливаем содержимое drop-зоны на каждое открытие модалки —
    // _iuLoadFile её перезаписывает на «Обработка…», иначе при втором открытии
    // увидим зависший спиннер.
    const dz = $("#iuDrop");
    if (dz) {
        dz.classList.remove("is-drag");
        dz.innerHTML = `
            <svg class="ic ic-large"><use href="#i-image"/></svg>
            <div class="iu-drop-title">Перетащите фото сюда</div>
            <div class="iu-drop-sub">или нажмите, чтобы выбрать файл</div>
            <input type="file" id="iuFile" accept="image/png,image/jpeg,image/jpg,image/webp,image/gif" hidden>`;
        // повторно навешиваем onchange (input был пересоздан)
        const fi = $("#iuFile");
        if (fi) fi.onchange = (e) => { const f = e.target.files[0]; if (f) _iuLoadFile(f); };
    }
    $("#iuStep1").hidden = false;
    $("#iuStep2").hidden = true;
    $("#iuSave").hidden = true;
    $("#iuCropImg").src = "";
    m.hidden = false;
}
function closeImageUpload() {
    const m = $("#imageUploadModal"); if (m) m.hidden = true;
    state.uploadTarget = null;
    state.uploadImage = null;
    state._iuCrop = null;
}

async function _iuLoadFile(file) {
    const target = state.uploadTarget;
    const p = UPLOAD_PRESETS[target] || UPLOAD_PRESETS.avatar;
    if (!file) return;
    if (!/^image\//.test(file.type)) return showToast("Это не изображение");
    if (file.size > p.maxSize) return showToast(`Файл больше ${(p.maxSize/(1024*1024)).toFixed(0)} МБ`);
    const dz = $("#iuDrop");
    if (dz) dz.innerHTML = `<div class="iu-loading"><div class="iu-spinner"></div><div>Обработка…</div></div>`;
    const dataUrl = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = () => rej(new Error("read_failed"));
        r.readAsDataURL(file);
    });
    const img = new Image();
    img.onload = () => {
        state.uploadImage = img;
        $("#iuStep1").hidden = true;
        $("#iuStep2").hidden = false;
        $("#iuSave").hidden = false;
        const cropImg = $("#iuCropImg");
        cropImg.src = dataUrl;
        _initCrop(cropImg, p.aspect);
    };
    img.onerror = () => showToast("Не удалось открыть изображение");
    img.src = dataUrl;
}

// Свой простой кроппер: drag = pan, wheel/буттоны = zoom, фиксированное окно
// заданного aspect, обрезка через canvas. Без внешних библиотек.
function _initCrop(imgEl, aspect) {
    const stage = $("#iuStage");
    if (!stage) return;
    // Подгоним «окно кропа» под размеры stage сохранив aspect.
    const stageRect = stage.getBoundingClientRect();
    const maxW = stageRect.width - 16;
    const maxH = stageRect.height - 16;
    let cropW, cropH;
    if (maxW / aspect <= maxH) { cropW = maxW; cropH = maxW / aspect; }
    else { cropH = maxH; cropW = maxH * aspect; }
    const frame = $("#iuFrame");
    frame.style.width = cropW + "px";
    frame.style.height = cropH + "px";
    // Стартовая трансформация: вписываем картинку, чтобы она покрывала окно (cover)
    const natW = imgEl.naturalWidth;
    const natH = imgEl.naturalHeight;
    const scale = Math.max(cropW / natW, cropH / natH);
    state._iuCrop = {
        cropW, cropH,
        natW, natH,
        scale,
        minScale: scale,
        maxScale: scale * 6,
        tx: 0, ty: 0,
    };
    _iuApplyTransform();
    // Drag handlers
    let dragging = false, lx = 0, ly = 0;
    imgEl.onpointerdown = (e) => {
        if (!state._iuCrop) return;
        dragging = true; lx = e.clientX; ly = e.clientY;
        try { imgEl.setPointerCapture(e.pointerId); } catch {}
    };
    imgEl.onpointermove = (e) => {
        if (!dragging) return;
        state._iuCrop.tx += (e.clientX - lx);
        state._iuCrop.ty += (e.clientY - ly);
        lx = e.clientX; ly = e.clientY;
        _iuApplyTransform();
    };
    imgEl.onpointerup = imgEl.onpointercancel = () => { dragging = false; };
    // Zoom (wheel)
    stage.onwheel = (e) => {
        if (!state._iuCrop) return;
        e.preventDefault();
        const delta = -e.deltaY * 0.0015;
        const c = state._iuCrop;
        const newScale = Math.max(c.minScale, Math.min(c.maxScale, c.scale * (1 + delta)));
        const k = newScale / c.scale;
        c.tx *= k; c.ty *= k; c.scale = newScale;
        _iuApplyTransform();
    };
    // Zoom buttons
    $("#iuZoomIn").onclick = () => _iuZoom(1.2);
    $("#iuZoomOut").onclick = () => _iuZoom(1/1.2);
    $("#iuReset").onclick = () => {
        const c = state._iuCrop; if (!c) return;
        c.scale = c.minScale; c.tx = 0; c.ty = 0;
        _iuApplyTransform();
    };
}
function _iuZoom(factor) {
    const c = state._iuCrop; if (!c) return;
    const ns = Math.max(c.minScale, Math.min(c.maxScale, c.scale * factor));
    const k = ns / c.scale; c.tx *= k; c.ty *= k; c.scale = ns;
    _iuApplyTransform();
}
function _iuApplyTransform() {
    const c = state._iuCrop; if (!c) return;
    const img = $("#iuCropImg");
    img.style.transform = `translate(calc(-50% + ${c.tx}px), calc(-50% + ${c.ty}px)) scale(${c.scale})`;
    // Превью
    _iuRenderPreview();
}
function _iuRenderPreview() {
    const c = state._iuCrop; if (!c) return;
    const target = state.uploadTarget;
    const p = UPLOAD_PRESETS[target] || UPLOAD_PRESETS.avatar;
    const cv = $("#iuPreview");
    if (!cv) return;
    const W = cv.width, H = cv.height;
    const ctx = cv.getContext("2d");
    ctx.clearRect(0,0,W,H);
    // Скейлим мини-превью пропорционально окну
    const scaleK = Math.min(W / c.cropW, H / c.cropH);
    const pw = c.cropW * scaleK, ph = c.cropH * scaleK;
    const px = (W - pw) / 2, py = (H - ph) / 2;
    ctx.save();
    ctx.beginPath();
    if (target === "avatar") {
        const r = Math.min(pw,ph) * 0.18;
        _iuRoundRect(ctx, px, py, pw, ph, r);
    } else {
        _iuRoundRect(ctx, px, py, pw, ph, 14);
    }
    ctx.clip();
    // фон
    ctx.fillStyle = "#0e0e10"; ctx.fillRect(px, py, pw, ph);
    // изображение
    const img = $("#iuCropImg");
    const drawW = img.naturalWidth * c.scale * scaleK;
    const drawH = img.naturalHeight * c.scale * scaleK;
    const dx = px + pw/2 - drawW/2 + c.tx * scaleK;
    const dy = py + ph/2 - drawH/2 + c.ty * scaleK;
    ctx.drawImage(img, dx, dy, drawW, drawH);
    ctx.restore();
}
function _iuRoundRect(ctx, x, y, w, h, r) {
    ctx.moveTo(x+r, y);
    ctx.arcTo(x+w, y,   x+w, y+h, r);
    ctx.arcTo(x+w, y+h, x,   y+h, r);
    ctx.arcTo(x,   y+h, x,   y,   r);
    ctx.arcTo(x,   y,   x+w, y,   r);
}
async function _iuSave() {
    const c = state._iuCrop; if (!c) return;
    const target = state.uploadTarget;
    const p = UPLOAD_PRESETS[target] || UPLOAD_PRESETS.avatar;
    const img = $("#iuCropImg");
    // Рисуем итоговый кроп в выходное разрешение
    const cv = document.createElement("canvas");
    cv.width = p.outW; cv.height = p.outH;
    const ctx = cv.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    const k = p.outW / c.cropW;
    const drawW = img.naturalWidth * c.scale * k;
    const drawH = img.naturalHeight * c.scale * k;
    const dx = p.outW/2 - drawW/2 + c.tx * k;
    const dy = p.outH/2 - drawH/2 + c.ty * k;
    ctx.fillStyle = "#0e0e10"; ctx.fillRect(0, 0, p.outW, p.outH);
    ctx.drawImage(img, dx, dy, drawW, drawH);
    const dataUrl = cv.toDataURL("image/jpeg", 0.92);
    try {
        // banner и avatar — оба идут через /api/profile.
        // favCover — локально сохраняем (per-user), т.к. «Мне нравится» не отдельная сущность.
        if (target === "favCover") {
            const key = "velora_likes_meta_" + (state.me?.id || state.me?.username || "u");
            const meta = JSON.parse(localStorage.getItem(key) || "{}");
            meta.cover = dataUrl;
            localStorage.setItem(key, JSON.stringify(meta));
            showToast("Обложка обновлена");
            closeImageUpload();
            if (typeof renderFavoritesPage === "function" && state.currentView === "favorites") {
                renderFavoritesPage();
            }
            return;
        }
        // Загружаем картинку как файл на сервер: всем виден один и тот же
        // публичный URL /api/img/<id>, а размер БД не пухнет.
        let url = dataUrl;
        try {
            const up = await api("/api/upload/image", {
                method: "POST",
                body: { data_url: dataUrl, kind: target },
            });
            if (up && up.url) url = up.url;
        } catch (upErr) {
            console.warn("upload failed, fallback to data URL", upErr);
        }
        await api("/api/profile", { method: "POST", body: { [target]: url } });
        state.me[target] = url;
        renderUserPill(); renderProfilePage();
        showToast(target === "avatar" ? "Аватар обновлён" : "Обложка обновлена");
        closeImageUpload();
    } catch (e) { showToast(e.message); }
}

// Привязки модалки (один раз, после загрузки DOM)
(function _setupImageUploadModal() {
    const m = $("#imageUploadModal"); if (!m) return;
    m.addEventListener("click", (e) => {
        if (e.target.closest("[data-iu-close]") || e.target === m) closeImageUpload();
    });
    const dz = $("#iuDrop");
    const fi = $("#iuFile");
    if (dz && fi) {
        dz.onclick = () => fi.click();
        fi.onchange = (e) => { const f = e.target.files[0]; if (f) _iuLoadFile(f); };
        dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("is-drag"); });
        dz.addEventListener("dragleave", () => dz.classList.remove("is-drag"));
        dz.addEventListener("drop", (e) => {
            e.preventDefault(); dz.classList.remove("is-drag");
            const f = e.dataTransfer.files[0]; if (f) _iuLoadFile(f);
        });
    }
    $("#iuSave")?.addEventListener("click", _iuSave);
})();

// =================================================================
// КОНТЕКСТНОЕ МЕНЮ ТРЕКА — «Добавить в плейлист»
// =================================================================
function openTrackMenu(track, anchor) {
    if (!track) return;
    if (!state.me) return openAuth();
    let menu = document.getElementById("trackMenu");
    if (!menu) {
        menu = document.createElement("div");
        menu.id = "trackMenu";
        menu.className = "track-menu";
        menu.hidden = true;
        document.body.appendChild(menu);
        document.addEventListener("click", (e) => {
            if (menu.hidden) return;
            if (menu.contains(e.target)) return;
            if (e.target.closest && e.target.closest("[data-act=more]")) return;
            menu.hidden = true;
        });
        window.addEventListener("scroll", () => { menu.hidden = true; }, true);
    }
    const pls = Array.isArray(state.playlists) ? state.playlists : [];
    const safeTitle = escapeHtml(track.title || "трек");
    const safeArtist = escapeHtml(track.artist || "");
    menu.innerHTML = `
        <div class="tm-head">
            <div class="tm-t">${safeTitle}</div>
            <div class="tm-a">${safeArtist}</div>
        </div>
        <div class="tm-section">Добавить в плейлист</div>
        <div class="tm-items">
            ${pls.length ? pls.map(p => `
                <button class="tm-item" data-pid="${p.id}">
                    <span class="tm-cover" ${p.cover?`style="background-image:${_cssUrl(p.cover)}"`:""}></span>
                    <span class="tm-name">${escapeHtml(p.name)}</span>
                    <span class="tm-count">${p.count||0}</span>
                </button>`).join("") : `<div class="tm-empty">Нет ни одного плейлиста</div>`}
        </div>
        <div class="tm-foot">
            <button class="tm-create"><svg class="ic"><use href="#i-plus"/></svg> Создать новый плейлист</button>
        </div>`;
    menu.hidden = false;
    // Позиционируем рядом с кнопкой
    const r = anchor.getBoundingClientRect();
    const mw = 280;
    const left = Math.max(8, Math.min(window.scrollX + r.right - mw, window.scrollX + window.innerWidth - mw - 8));
    let top = window.scrollY + r.bottom + 6;
    // Если меню не помещается снизу — открываем вверх
    const mh = menu.offsetHeight || 320;
    if (r.bottom + mh + 12 > window.innerHeight) {
        top = window.scrollY + r.top - mh - 6;
    }
    menu.style.left = left + "px";
    menu.style.top = top + "px";
    menu.style.width = mw + "px";

    menu.querySelectorAll(".tm-item").forEach(b => {
        b.onclick = async (e) => {
            e.stopPropagation();
            const pid = Number(b.dataset.pid);
            try {
                await api(`/api/playlists/${pid}/add`, { method: "POST", body: {
                    id: track.source_id,
                    source: track.source,
                    title: track.title || "",
                    artist: track.artist || "",
                    album: track.album || "",
                    cover_big: track.album_cover || "",
                    cover_small: track.album_cover || "",
                    duration: track.duration || 0,
                    explicit: !!track.explicit,
                }});
                menu.hidden = true;
                const pl = state.playlists.find(p => p.id === pid);
                showToast(`Добавлено в «${pl?.name || "плейлист"}»`);
                await loadPlaylists();
            } catch (err) { showToast(err.message); }
        };
    });
    const createBtn = menu.querySelector(".tm-create");
    if (createBtn) createBtn.onclick = async (e) => {
        e.stopPropagation();
        const name = prompt("Название нового плейлиста:");
        if (!name || !name.trim()) return;
        try {
            const r = await api("/api/playlists", { method: "POST", body: { name: name.trim() } });
            const pid = r.id || r.playlist?.id;
            if (pid) {
                await api(`/api/playlists/${pid}/add`, { method: "POST", body: {
                    id: track.source_id, source: track.source,
                    title: track.title || "", artist: track.artist || "",
                    album: track.album || "", cover_big: track.album_cover || "",
                    duration: track.duration || 0, explicit: !!track.explicit,
                }});
            }
            menu.hidden = true;
            await loadPlaylists();
            showToast(`Создан «${name.trim()}» — трек добавлен`);
        } catch (err) { showToast(err.message); }
    };
}

// === Track menu (Add to playlist) ===

// =================================================================
// АДМИНКА удалена с веб-сайта. Управление предложками и помощниками
// теперь живёт только в Telegram-боте (@saylont). Оставшиеся
// серверные роуты /api/admin/* по-прежнему доступны и используются
// ботом, но из веб-UI к ним больше не обращаемся.
// =================================================================
// (admin UI code removed; bot @saylont owns this surface now)
