"""Dibujo de los íconos y textos de la bandeja (independiente de Windows)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .util import parse_time

# Mismos umbrales y colores que los medidores del dashboard de ejemplo.
GREEN, AMBER, RED, GRAY = (67, 160, 71), (255, 166, 0), (219, 68, 55), (97, 97, 97)
# Franja inferior que identifica a cada IA.
BRAND = {"claude": (217, 119, 87), "codex": (90, 160, 255), "cursor": (190, 190, 190)}
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
STALE_SECONDS = 20 * 60
TIP_MAX = 127  # límite de Windows para el tooltip de la bandeja


def level_color(margin: float | None) -> tuple[int, int, int]:
    if margin is None:
        return GRAY
    if margin >= 50:
        return GREEN
    if margin >= 20:
        return AMBER
    return RED


def icon_text(item: dict | None) -> str:
    if not item or item.get("margin") is None:
        return "?" if not item or item.get("status") in (None, "ok") else "!"
    return str(int(round(float(item["margin"]))))


def _font(px: int):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        path = Path(windir) / "Fonts" / name
        if path.is_file():
            return ImageFont.truetype(str(path), px)
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
        if Path(path).is_file():
            return ImageFont.truetype(path, px)
    return ImageFont.load_default(px)


def render_icon(key: str, item: dict | None, size: int, stale: bool = False) -> Image.Image:
    """Ícono cuadrado: fondo según el margen, número grande y franja de la IA."""
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg = GRAY if stale else level_color(item.get("margin") if item else None)
    band_h = max(scale * 2, int(big * 0.17))
    radius = int(big * 0.2)
    draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=BRAND.get(key, GRAY) + (255,))
    draw.rounded_rectangle([0, 0, big - 1, big - 1 - band_h], radius=radius, fill=bg + (255,))
    draw.rectangle([0, big - 1 - band_h - radius, big - 1, big - 1 - band_h], fill=bg + (255,))

    text = icon_text(item)
    fg = (30, 30, 30) if bg == AMBER else (255, 255, 255)
    area_h = big - band_h
    px = int(area_h * 0.92)
    while px > 6:
        font = _font(px)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= big * 0.94 and bottom - top <= area_h * 0.78:
            break
        px -= scale
    x = (big - (right - left)) / 2 - left
    y = (area_h - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=fg + (255,))
    return img.resize((size, size), Image.LANCZOS)


def _when(value, now: dt.datetime) -> str:
    when = parse_time(value)
    if when is None:
        return ""
    local = when.astimezone()
    today = now.astimezone().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if (local.date() - today).days < 7:
        return f"{WEEKDAYS[local.weekday()]} {local.strftime('%H:%M')}"
    return local.strftime("%d/%m")


def _pct(value) -> str:
    return "–" if value is None else f"{float(value):.0f} %"


def tooltip(key: str, item: dict | None, best: str | None, stale: bool, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    label = (item or {}).get("label") or key.capitalize()
    if stale or not item:
        return f"{label}: sin datos (¿servicio AI Usage Bridge detenido?)"[:TIP_MAX]
    star = " ★" if best and best == label else ""
    lines = [f"{label}: {_pct(item.get('margin'))} libre{star}"]
    if item.get("status") != "ok":
        lines.append(f"Estado: {item.get('status_text') or item.get('status')}")
    if item.get("h5_used") is not None or item.get("week_used") is not None:
        reset = _when(item.get("h5_reset"), now)
        lines.append(f"5 h: {_pct(item.get('h5_used'))}" + (f" · reset {reset}" if reset else ""))
        reset = _when(item.get("week_reset"), now)
        lines.append(f"Semana: {_pct(item.get('week_used'))}" + (f" · reset {reset}" if reset else ""))
    if item.get("included_used") is not None:
        end = _when(item.get("cycle_reset"), now)
        lines.append(f"Incluido: {_pct(item.get('included_used'))}" + (f" · hasta {end}" if end else ""))
        if item.get("ondemand_limit"):
            lines.append(f"On-demand: {item.get('ondemand_used') or 0:.2f} / {item['ondemand_limit']:.0f} USD")
    text = "\n".join(lines)
    return text if len(text) <= TIP_MAX else text[: TIP_MAX - 1] + "…"


def is_stale(status: dict | None, now: dt.datetime | None = None) -> bool:
    if not status or status.get("stopped"):
        return True
    updated = parse_time(status.get("updated"))
    now = now or dt.datetime.now(dt.timezone.utc)
    return updated is None or (now - updated).total_seconds() > STALE_SECONDS
