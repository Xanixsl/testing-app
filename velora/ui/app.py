"""Главное окно Velora Sound."""
from __future__ import annotations

import threading
from typing import Callable

import customtkinter as ctk
from PIL import Image

from velora.api import deezer
from velora.api.resolver import resolve_stream
from velora.images import load_image
from velora.models import Artist, Track
from velora.player import Player

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


def _fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def _run_bg(func: Callable, *args, on_done: Callable | None = None) -> None:
    def runner() -> None:
        try:
            result = func(*args)
            print(f"[bg] {func.__name__}({args!r}) -> {type(result).__name__} len={len(result) if hasattr(result, '__len__') else 'n/a'}", flush=True)
        except Exception as exc:  # pragma: no cover
            import traceback
            print(f"[bg] {func.__name__}({args!r}) FAILED: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            result = exc
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                import traceback
                traceback.print_exc()

    threading.Thread(target=runner, daemon=True).start()


class TrackRow(ctk.CTkFrame):
    """Строка трека в списке."""

    def __init__(
        self,
        master,
        track: Track,
        on_play: Callable[[Track], None],
        on_artist: Callable[[str], None],
    ) -> None:
        super().__init__(master, fg_color=("#1f1f24", "#1f1f24"), corner_radius=8)
        self.track = track
        self.on_play = on_play
        self.on_artist = on_artist

        self.cover_label = ctk.CTkLabel(self, text="", width=48, height=48)
        self.cover_label.grid(row=0, column=0, rowspan=2, padx=8, pady=6)

        self.title_label = ctk.CTkLabel(
            self, text=track.title, anchor="w", font=ctk.CTkFont(size=14, weight="bold")
        )
        self.title_label.grid(row=0, column=1, sticky="w", padx=4)

        self.artist_label = ctk.CTkLabel(
            self,
            text=f"{track.artist}  •  {track.album}".strip(" •"),
            anchor="w",
            text_color="#9aa0a6",
            cursor="hand2",
        )
        self.artist_label.grid(row=1, column=1, sticky="w", padx=4)
        self.artist_label.bind(
            "<Button-1>", lambda _e: track.artist_id and on_artist(track.artist_id)
        )

        self.dur_label = ctk.CTkLabel(
            self, text=_fmt_time(track.duration * 1000), text_color="#9aa0a6", width=60
        )
        self.dur_label.grid(row=0, column=2, rowspan=2, padx=8)

        self.play_btn = ctk.CTkButton(
            self, text="▶", width=44, command=lambda: on_play(track)
        )
        self.play_btn.grid(row=0, column=3, rowspan=2, padx=8, pady=6)

        self.grid_columnconfigure(1, weight=1)
        _run_bg(load_image, track.cover_small or track.cover_big, (48, 48), on_done=self._set_cover)

    def _set_cover(self, img: Image.Image | None) -> None:
        if isinstance(img, Image.Image):
            self.after(0, lambda: self._apply_cover(img))

    def _apply_cover(self, img: Image.Image) -> None:
        ck_img = ctk.CTkImage(light_image=img, dark_image=img, size=(48, 48))
        self.cover_label.configure(image=ck_img, text="")
        self.cover_label.image = ck_img


class ArtistRow(ctk.CTkFrame):
    def __init__(self, master, artist: Artist, on_open: Callable[[str], None]) -> None:
        super().__init__(master, fg_color=("#1f1f24", "#1f1f24"), corner_radius=8)
        self.artist = artist
        self.pic_label = ctk.CTkLabel(self, text="", width=64, height=64)
        self.pic_label.grid(row=0, column=0, rowspan=2, padx=8, pady=6)
        ctk.CTkLabel(
            self, text=artist.name, anchor="w", font=ctk.CTkFont(size=15, weight="bold")
        ).grid(row=0, column=1, sticky="w", padx=4)
        ctk.CTkLabel(
            self,
            text=f"Поклонников: {artist.fans:,}  •  Альбомов: {artist.nb_album}",
            anchor="w",
            text_color="#9aa0a6",
        ).grid(row=1, column=1, sticky="w", padx=4)
        ctk.CTkButton(self, text="Открыть", width=90, command=lambda: on_open(artist.id)).grid(
            row=0, column=2, rowspan=2, padx=8
        )
        self.grid_columnconfigure(1, weight=1)
        _run_bg(load_image, artist.picture_small or artist.picture_big, (64, 64), on_done=self._set_pic)

    def _set_pic(self, img: Image.Image | None) -> None:
        if isinstance(img, Image.Image):
            self.after(0, lambda: self._apply(img))

    def _apply(self, img: Image.Image) -> None:
        ck_img = ctk.CTkImage(light_image=img, dark_image=img, size=(64, 64))
        self.pic_label.configure(image=ck_img, text="")
        self.pic_label.image = ck_img


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Velora Sound")
        self.geometry("1180x720")
        self.minsize(960, 600)

        self.player = Player()
        self.player.on_end(self._next_track)

        self.queue: list[Track] = []
        self.queue_index: int = -1
        self._seeking = False

        self._build_topbar()
        self._build_body()
        self._build_player_bar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._tick)

    # ------------------------------------------------------------------ UI
    def _build_topbar(self) -> None:
        top = ctk.CTkFrame(self, height=64, corner_radius=0)
        top.pack(side="top", fill="x")
        ctk.CTkLabel(
            top,
            text="🎵 Velora Sound",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16)

        self.mode_seg = ctk.CTkSegmentedButton(
            top,
            values=["Треки", "Артисты"],
            command=self._on_mode_change,
        )
        self.mode_seg.set("Треки")
        self.mode_seg.pack(side="left", padx=10)

        self.search_var = ctk.StringVar()
        entry = ctk.CTkEntry(
            top,
            textvariable=self.search_var,
            placeholder_text="Поиск исполнителя, трека, альбома…",
            width=420,
        )
        entry.pack(side="left", padx=8, pady=12, fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self._do_search())

        ctk.CTkButton(top, text="Найти", width=100, command=self._do_search).pack(
            side="left", padx=8
        )

    def _build_body(self) -> None:
        body = ctk.CTkFrame(self, corner_radius=0)
        body.pack(side="top", fill="both", expand=True)
        body.grid_columnconfigure(0, weight=3, uniform="b")
        body.grid_columnconfigure(1, weight=2, uniform="b")
        body.grid_rowconfigure(0, weight=1)

        # --- список результатов
        self.results = ctk.CTkScrollableFrame(body, label_text="Результаты поиска")
        self.results.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        # --- карточка артиста / Now Playing
        self.right = ctk.CTkScrollableFrame(body, label_text="Сейчас играет")
        self.right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

        self._render_now_playing(None)

    def _build_player_bar(self) -> None:
        bar = ctk.CTkFrame(self, height=88, corner_radius=0)
        bar.pack(side="bottom", fill="x")

        self.np_cover = ctk.CTkLabel(bar, text="", width=64, height=64)
        self.np_cover.grid(row=0, column=0, rowspan=2, padx=10, pady=10)

        self.np_title = ctk.CTkLabel(
            bar, text="—", anchor="w", font=ctk.CTkFont(size=14, weight="bold")
        )
        self.np_title.grid(row=0, column=1, sticky="we", padx=4)
        self.np_artist = ctk.CTkLabel(bar, text="", anchor="w", text_color="#9aa0a6")
        self.np_artist.grid(row=1, column=1, sticky="we", padx=4)

        controls = ctk.CTkFrame(bar, fg_color="transparent")
        controls.grid(row=0, column=2, rowspan=2, padx=10)
        ctk.CTkButton(controls, text="⏮", width=42, command=self._prev_track).pack(side="left", padx=2)
        self.play_btn = ctk.CTkButton(controls, text="▶", width=52, command=self._toggle_play)
        self.play_btn.pack(side="left", padx=2)
        ctk.CTkButton(controls, text="⏭", width=42, command=self._next_track).pack(side="left", padx=2)

        seek_box = ctk.CTkFrame(bar, fg_color="transparent")
        seek_box.grid(row=0, column=3, rowspan=2, sticky="we", padx=10)
        self.time_cur = ctk.CTkLabel(seek_box, text="0:00", width=40, text_color="#9aa0a6")
        self.time_cur.pack(side="left")
        self.seek = ctk.CTkSlider(seek_box, from_=0, to=1000, command=self._on_seek_drag)
        self.seek.set(0)
        self.seek.bind("<Button-1>", lambda _e: self._set_seeking(True))
        self.seek.bind("<ButtonRelease-1>", self._on_seek_release)
        self.seek.pack(side="left", fill="x", expand=True, padx=8)
        self.time_total = ctk.CTkLabel(seek_box, text="0:00", width=40, text_color="#9aa0a6")
        self.time_total.pack(side="left")

        vol_box = ctk.CTkFrame(bar, fg_color="transparent")
        vol_box.grid(row=0, column=4, rowspan=2, padx=10)
        ctk.CTkLabel(vol_box, text="🔊").pack(side="left", padx=4)
        self.volume = ctk.CTkSlider(vol_box, from_=0, to=100, width=120, command=self._on_volume)
        self.volume.set(self.player.volume)
        self.volume.pack(side="left")

        bar.grid_columnconfigure(1, weight=1)
        bar.grid_columnconfigure(3, weight=2)

    # ------------------------------------------------------------- handlers
    def _on_mode_change(self, _value: str) -> None:
        if self.search_var.get().strip():
            self._do_search()

    def _do_search(self) -> None:
        query = self.search_var.get().strip()
        print(f"[search] query={query!r}", flush=True)
        if not query:
            return
        self._clear(self.results)
        info = ctk.CTkLabel(self.results, text="Ищу…", text_color="#9aa0a6")
        info.pack(pady=20)

        mode = self.mode_seg.get() if hasattr(self, "mode_seg") else "Треки"
        print(f"[search] mode={mode!r}", flush=True)

        if mode == "Артисты":
            _run_bg(deezer.search_artists, query, on_done=self._render_artists)
        else:
            _run_bg(deezer.search_tracks, query, on_done=self._render_tracks)

    def _find_segmented(self):
        return getattr(self, "mode_seg", None)

    def _render_tracks(self, result) -> None:
        def apply():
            self._clear(self.results)
            if isinstance(result, Exception):
                import traceback
                traceback.print_exception(type(result), result, result.__traceback__)
                ctk.CTkLabel(
                    self.results,
                    text=f"Ошибка: {type(result).__name__}: {result}",
                    text_color="#e57373",
                    wraplength=600,
                ).pack(pady=20, padx=10)
                return
            if not result:
                ctk.CTkLabel(
                    self.results,
                    text="Ничего не найдено.",
                    text_color="#e57373",
                ).pack(pady=20)
                return
            self.queue = list(result)
            for t in result:
                row = TrackRow(self.results, t, self._play_track, self._open_artist)
                row.pack(fill="x", pady=3, padx=2)

        self.after(0, apply)

    def _render_artists(self, result) -> None:
        def apply():
            self._clear(self.results)
            if isinstance(result, Exception):
                import traceback
                traceback.print_exception(type(result), result, result.__traceback__)
                ctk.CTkLabel(
                    self.results,
                    text=f"Ошибка: {type(result).__name__}: {result}",
                    text_color="#e57373",
                    wraplength=600,
                ).pack(pady=20, padx=10)
                return
            if not result:
                ctk.CTkLabel(
                    self.results,
                    text="Ничего не найдено.",
                    text_color="#e57373",
                ).pack(pady=20)
                return
            for a in result:
                row = ArtistRow(self.results, a, self._open_artist)
                row.pack(fill="x", pady=3, padx=2)

        self.after(0, apply)

    def _open_artist(self, artist_id: str) -> None:
        if not artist_id:
            return
        self._clear(self.right)
        ctk.CTkLabel(self.right, text="Загружаю карточку артиста…", text_color="#9aa0a6").pack(pady=20)
        _run_bg(deezer.get_artist, artist_id, on_done=lambda a: self.after(0, lambda: self._render_artist_card(a)))

    def _render_artist_card(self, artist) -> None:
        self._clear(self.right)
        if isinstance(artist, Exception) or artist is None:
            ctk.CTkLabel(self.right, text="Не удалось загрузить артиста.", text_color="#e57373").pack(pady=20)
            return

        header = ctk.CTkFrame(self.right, fg_color="transparent")
        header.pack(fill="x", pady=8)
        pic = ctk.CTkLabel(header, text="", width=140, height=140)
        pic.pack(side="left", padx=8)
        _run_bg(
            load_image,
            artist.picture_big or artist.picture_small,
            (140, 140),
            on_done=lambda img: self.after(0, lambda: self._set_image(pic, img, (140, 140))),
        )
        info = ctk.CTkFrame(header, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=8)
        ctk.CTkLabel(info, text=artist.name, font=ctk.CTkFont(size=22, weight="bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(
            info,
            text=f"Поклонников: {artist.fans:,}\nАльбомов: {artist.nb_album}",
            justify="left",
            text_color="#9aa0a6",
        ).pack(anchor="w", pady=4)

        if artist.top_tracks:
            ctk.CTkLabel(
                self.right, text="Популярные треки", font=ctk.CTkFont(size=15, weight="bold")
            ).pack(anchor="w", padx=8, pady=(10, 4))
            self.queue = list(artist.top_tracks)
            for t in artist.top_tracks:
                TrackRow(self.right, t, self._play_track, self._open_artist).pack(
                    fill="x", padx=4, pady=2
                )

        if artist.albums:
            ctk.CTkLabel(
                self.right, text="Альбомы", font=ctk.CTkFont(size=15, weight="bold")
            ).pack(anchor="w", padx=8, pady=(10, 4))
            grid = ctk.CTkFrame(self.right, fg_color="transparent")
            grid.pack(fill="x", padx=4)
            for i, al in enumerate(artist.albums):
                self._album_tile(grid, al, i)

    def _album_tile(self, parent, al: dict, index: int) -> None:
        col = index % 3
        row = index // 3
        tile = ctk.CTkFrame(parent, fg_color="#1f1f24", corner_radius=8)
        tile.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
        parent.grid_columnconfigure(col, weight=1)
        cover = ctk.CTkLabel(tile, text="", width=110, height=110)
        cover.pack(padx=6, pady=6)
        ctk.CTkLabel(
            tile, text=al["title"], wraplength=120, font=ctk.CTkFont(size=12, weight="bold")
        ).pack(padx=4)
        ctk.CTkLabel(tile, text=al.get("year", ""), text_color="#9aa0a6").pack()
        ctk.CTkButton(
            tile,
            text="Слушать",
            width=100,
            command=lambda aid=al["id"]: self._load_album(aid),
        ).pack(padx=4, pady=6)
        _run_bg(
            load_image,
            al["cover"],
            (110, 110),
            on_done=lambda img: self.after(0, lambda: self._set_image(cover, img, (110, 110))),
        )

    def _load_album(self, album_id: str) -> None:
        _run_bg(deezer.get_album_tracks, album_id, on_done=self._render_tracks)

    def _set_image(self, label, img, size) -> None:
        if isinstance(img, Image.Image):
            ck_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
            label.configure(image=ck_img, text="")
            label.image = ck_img

    # ----------------------------------------------------------- playback
    def _play_track(self, track: Track) -> None:
        # обновляем индекс в очереди
        if track in self.queue:
            self.queue_index = self.queue.index(track)
        else:
            self.queue = [track]
            self.queue_index = 0
        self._render_now_playing(track)
        self.np_title.configure(text=track.title)
        self.np_artist.configure(text=f"{track.artist} — {track.album}".strip(" —"))
        _run_bg(
            load_image,
            track.cover_big or track.cover_small,
            (64, 64),
            on_done=lambda img: self.after(0, lambda: self._set_image(self.np_cover, img, (64, 64))),
        )
        # резолвим полный поток в фоне
        query = f"{track.artist} - {track.title}"
        self.play_btn.configure(text="…")
        _run_bg(resolve_stream, query, on_done=self._start_stream_or_preview(track))

    def _start_stream_or_preview(self, track: Track):
        def cb(stream_url):
            url = stream_url if isinstance(stream_url, str) and stream_url else track.preview_url
            if not url:
                self.after(0, lambda: self.play_btn.configure(text="▶"))
                self.after(0, lambda: self.np_artist.configure(text="Источник недоступен"))
                return

            def go():
                self.player.play_url(url)
                self.play_btn.configure(text="⏸")

            self.after(0, go)

        return cb

    def _toggle_play(self) -> None:
        if self.queue_index < 0:
            return
        self.player.toggle_pause()
        self.play_btn.configure(text="⏸" if self.player.is_playing else "▶")

    def _next_track(self) -> None:
        if not self.queue:
            return
        self.queue_index = (self.queue_index + 1) % len(self.queue)
        self._play_track(self.queue[self.queue_index])

    def _prev_track(self) -> None:
        if not self.queue:
            return
        self.queue_index = (self.queue_index - 1) % len(self.queue)
        self._play_track(self.queue[self.queue_index])

    def _on_volume(self, value) -> None:
        self.player.set_volume(int(float(value)))

    def _set_seeking(self, value: bool) -> None:
        self._seeking = value

    def _on_seek_drag(self, _value) -> None:
        self._seeking = True

    def _on_seek_release(self, _event) -> None:
        try:
            value = float(self.seek.get())
            self.player.seek(value / 1000.0)
        finally:
            self._seeking = False

    # ----------------------------------------------------------- now playing
    def _render_now_playing(self, track: Track | None) -> None:
        # Не очищаем правую панель если там карточка артиста — она важнее.
        pass

    # ------------------------------------------------------------- helpers
    def _clear(self, frame) -> None:
        for c in frame.winfo_children():
            c.destroy()

    def _tick(self) -> None:
        try:
            length = self.player.length_ms
            cur = self.player.time_ms
            if length > 0:
                if not self._seeking:
                    self.seek.set((cur / length) * 1000.0)
                self.time_cur.configure(text=_fmt_time(cur))
                self.time_total.configure(text=_fmt_time(length))
        except Exception:
            pass
        self.after(500, self._tick)

    def _on_close(self) -> None:
        try:
            self.player.release()
        finally:
            self.destroy()


def run() -> None:
    app = App()
    app.mainloop()
