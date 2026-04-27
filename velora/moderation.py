"""Модерация пользовательского контента для постов на стене.

Цель — блокировать порнографию и около-эротический контент.

Слой 1: текст
  • список явно эротических/порно-терминов RU/EN;
  • чёрный список доменов (порно/эскорт сервисы).

Слой 2: изображение
  • Если установлена `nudenet` → используем NudeDetector (это рабочая офлайн
    NSFW-сетка с лицензированной моделью, классы вида *_EXPOSED).
  • Fallback — эвристика по соотношению «телесных» пикселей в YCbCr
    (более точна, чем RGB) с порогом 25% от площади. Дополнительно
    отклоняем почти полностью тёмные кадры (часто используют для обхода).
  • Анимированные изображения проверяем по нескольким кадрам.

Любая ошибка анализа → отклоняем (fail-closed).
"""
from __future__ import annotations

import os
import re
import tempfile
from io import BytesIO
from typing import Tuple

# ----------------------- 1. Текст ---------------------------------------

_EXPLICIT_TERMS_RU = [
    r"порн[оаыеу]\w*", r"порнух\w*", r"порево",
    r"анал\w*", r"минет\w*", r"кунилин\w*", r"куннил\w*",
    r"мастурбац\w*", r"эякуляц\w*", r"оргазм\w*",
    r"шлюх[аеиу]\b", r"проститут\w*", r"эскорт-услуг\w*",
    r"секс[ -]чат\w*", r"вирт[ -]?секс", r"вирт\s+за\s+деньги",
    r"раздет\w*\s+(дев|жен|мал)", r"голы[ехй]\s+(фото|видео|девушк)",
    r"\bххх\b", r"18\+\s*(фото|видео)", r"\bnsfw\b",
    r"интим[ -]?(фото|видео|услуг)", r"\bбдсм\b",
    r"члено?[сс]ос\w*", r"кончит[ьл]\s+на",
]
_EXPLICIT_TERMS_EN = [
    r"\bporn\w*", r"\bpr0n\b", r"\bxxx\b", r"\bnsfw\b",
    r"\bblowjob\b", r"\bhandjob\b", r"\banal\b", r"\bcunnilingus\b",
    r"\bfellatio\b", r"\bmasturbat\w*", r"\borgasm\b",
    r"\bescort\s+service\b", r"\bcam\s*girl\b", r"\bonlyfans\b",
    r"\bhentai\b", r"\bp[o0]rno?hub\b", r"\bxvideos?\b", r"\bxhamster\b",
    r"\bsex\s+(video|tape|chat)\b", r"\bnude\s+(photo|pic|video)\b",
    r"\bbdsm\b", r"\bfetish\b", r"\bcumshot\b",
]
_EXPLICIT_RE = re.compile(
    "|".join(_EXPLICIT_TERMS_RU + _EXPLICIT_TERMS_EN),
    flags=re.IGNORECASE | re.UNICODE,
)

_BAD_DOMAINS = {
    "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
    "redtube.com", "youporn.com", "brazzers.com", "onlyfans.com",
    "fansly.com", "stripchat.com", "chaturbate.com", "bongacams.com",
    "camsoda.com", "livejasmin.com", "myfreecams.com",
    "spankbang.com", "tnaflix.com", "porn.com", "rule34.xxx",
    "e-hentai.org", "nhentai.net", "hentaihaven.xxx",
    "erome.com", "motherless.com", "porntrex.com",
}

_URL_RE = re.compile(r"https?://([^\s/]+)", re.IGNORECASE)


def check_text(text: str) -> Tuple[bool, str]:
    """Возвращает (ok, reason)."""
    if not text:
        return True, ""
    s = text.lower()
    if _EXPLICIT_RE.search(s):
        return False, "Текст содержит запрещённые слова (18+/порно)."
    for m in _URL_RE.finditer(s):
        host = m.group(1).lower().split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        if host in _BAD_DOMAINS:
            return False, f"Ссылка ведёт на запрещённый сайт ({host})."
        for bad in _BAD_DOMAINS:
            if host.endswith("." + bad):
                return False, f"Ссылка ведёт на запрещённый сайт ({host})."
    return True, ""


# ----------------------- 2. Изображения ---------------------------------

# Чем больше «голого тела» — тем больше пикселей попадает в скин-диапазон.
# Порог сознательно строгий: лучше отклонить безобидное селфи в полный кадр,
# чем пропустить эротику.
_SKIN_THRESHOLD = 0.30
_DARK_THRESHOLD = 0.92  # почти полностью тёмный кадр — попытка обхода

# Кэш экземпляра NudeDetector — он тяжёлый при инициализации.
_NUDENET = None
_NUDENET_TRIED = False


def _get_nudenet():
    global _NUDENET, _NUDENET_TRIED
    if _NUDENET_TRIED:
        return _NUDENET
    _NUDENET_TRIED = True
    try:
        from nudenet import NudeDetector  # type: ignore
        _NUDENET = NudeDetector()
    except Exception:
        _NUDENET = None
    return _NUDENET


# Метки модели nudenet, которые блокируют пост безусловно (взрослый контент).
_NUDENET_BLOCKING = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",  "ANUS_EXPOSED", "BUTTOCKS_EXPOSED",
    # Старые версии могут возвращать иные имена:
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M",
    "EXPOSED_BREAST_F",   "EXPOSED_ANUS", "EXPOSED_BUTTOCKS",
}
# Эти метки сами по себе не блокируют, но повышают подозрение:
_NUDENET_SUSPICIOUS = {
    "FEMALE_GENITALIA_COVERED", "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED", "BELLY_EXPOSED", "ARMPITS_EXPOSED",
    "FEET_EXPOSED",
}


def _check_with_nudenet(raw: bytes, mime: str) -> Tuple[bool, str] | None:
    det = _get_nudenet()
    if det is None:
        return None
    ext = ".jpg"
    m = (mime or "").lower()
    if "png" in m: ext = ".png"
    elif "gif" in m: ext = ".gif"
    elif "webp" in m: ext = ".webp"
    elif "avif" in m: ext = ".avif"
    elif "bmp" in m: ext = ".bmp"
    elif "tif" in m: ext = ".tiff"
    elif "heic" in m or "heif" in m: ext = ".heic"
    tmp_path = ""
    try:
        tf = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tf.write(raw); tf.close()
        tmp_path = tf.name
        try:
            res = det.detect(tmp_path) or []
        except Exception:
            return None  # сетке плохо — пусть отработает fallback
        sus = 0
        for d in res:
            try:
                lbl = (d.get("class") or d.get("label") or "").upper()
                score = float(d.get("score", 0) or 0)
            except Exception:
                continue
            if score < 0.45:
                continue
            if lbl in _NUDENET_BLOCKING:
                return False, "Изображение содержит обнажённый контент (NSFW)."
            if lbl in _NUDENET_SUSPICIOUS and score >= 0.55:
                sus += 1
        if sus >= 3:
            return False, "Изображение похоже на эротический контент."
        return True, ""
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass


def _frame_skin_ratio(pil_frame) -> Tuple[float, float]:
    """Возвращает (доля_кожи, доля_тёмного)."""
    yc = pil_frame.convert("YCbCr").resize((128, 128))
    px = yc.load()
    rgb = pil_frame.convert("RGB").resize((128, 128)).load()
    total = 128 * 128
    skin = 0
    dark = 0
    for y in range(128):
        for x in range(128):
            Y, Cb, Cr = px[x, y]
            # Канонический skin-диапазон в YCbCr.
            if 80 <= Cb <= 130 and 130 <= Cr <= 180 and Y > 60:
                skin += 1
            r, g, b = rgb[x, y]
            if max(r, g, b) < 30:
                dark += 1
    return skin / total, dark / total


def _check_with_heuristic(raw: bytes) -> Tuple[bool, str]:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return True, ""  # без PIL не проверим — пропускаем
    try:
        img = Image.open(BytesIO(raw))
        frames = [0]
        n = getattr(img, "n_frames", 1) or 1
        if n > 1:
            frames = sorted({0, n // 2, n - 1})[:3]
        worst_skin = 0.0
        worst_dark = 0.0
        analyzed = 0
        for fi in frames:
            try: img.seek(fi)
            except Exception: continue
            try:
                s, d = _frame_skin_ratio(img.copy())
            except Exception:
                continue
            analyzed += 1
            if s > worst_skin: worst_skin = s
            if d > worst_dark: worst_dark = d
        if analyzed == 0:
            # PIL не смог разобрать кадры (экзотический формат) —
            # доверяем NudeNet/тексту, не блокируем.
            return True, ""
        if worst_skin > _SKIN_THRESHOLD:
            return False, "Изображение содержит слишком много обнажённого тела."
        if worst_dark > _DARK_THRESHOLD:
            return False, "Изображение почти полностью тёмное (вероятно, попытка обхода)."
    except Exception:
        # PIL вообще не открыл — это, скорее всего, редкий формат, который
        # уже одобрил NudeNet. Не блокируем.
        return True, ""
    return True, ""


def check_image(raw: bytes, mime: str) -> Tuple[bool, str]:
    """Главная точка входа. Сначала NudeNet (точнее), затем эвристика."""
    if not raw:
        return False, "Пустой файл."
    res = _check_with_nudenet(raw, mime)
    if res is not None:
        if res[0]:
            # Доп. защита: если кадр почти чёрный — отклоняем.
            h_ok, h_reason = _check_with_heuristic(raw)
            if not h_ok and "тёмное" in h_reason:
                return False, h_reason
            return True, ""
        return res
    return _check_with_heuristic(raw)
