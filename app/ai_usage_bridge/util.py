"""Utilidades comunes: fechas, JWT, números y redacción de secretos."""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from typing import Any

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(value: Any) -> dt.datetime | None:
    """Acepta ISO 8601, epoch en segundos o milisegundos. Devuelve datetime UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # milisegundos
            seconds /= 1000.0
        return dt.datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_time(float(text))
    text = text.replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # más de 6 decimales
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def jwt_claims(token: str | None) -> dict:
    """Decodifica el payload de un JWT sin validar la firma (solo lectura de claims)."""
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: Any) -> float | None:
    """Porcentaje 0-100 redondeado a un decimal."""
    v = num(value)
    if v is None:
        return None
    return round(max(0.0, min(100.0, v)), 1)


_SECRET_KEYS = re.compile(
    r"token|secret|cookie|authorization|password|email|session|refresh|id_token|key",
    re.IGNORECASE,
)


def redact(obj: Any) -> Any:
    """Copia de obj con los valores de claves sensibles reemplazados por '***'."""
    if isinstance(obj, dict):
        return {
            k: ("***" if _SECRET_KEYS.search(str(k)) and isinstance(v, (str, int)) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def rollover_windows(state: dict, windows: list[tuple[str, str]]) -> None:
    """Si el reset de una ventana ya pasó, la uso vuelve a 0 (dato estimado)."""
    now = now_utc()
    expired = set()
    for _, reset_key in windows:
        reset = parse_time(state.get(reset_key))
        if reset is not None and reset <= now:
            expired.add(reset_key)
    estimated = []
    for used_key, reset_key in windows:
        if reset_key in expired and state.get(used_key) is not None:
            state[used_key] = 0.0
            estimated.append(used_key)
    for reset_key in expired:
        state[reset_key] = None
    if estimated:
        state.setdefault("attributes", {})["estimado_por_reset"] = estimated
