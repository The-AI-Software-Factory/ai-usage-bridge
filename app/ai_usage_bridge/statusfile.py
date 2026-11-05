"""Archivo de estado local para la app de bandeja.

El servicio escribe ``status.json`` junto a ``config.toml``. Tiene solo los
números de uso y la configuración de la bandeja: nunca tokens, contraseñas ni
mensajes de error con datos de sesión. Lo pueden leer los usuarios de la PC.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import __version__
from .util import iso, now_utc

FILENAME = "status.json"

# Campos de cada proveedor que se copian (el resto queda solo en MQTT).
FIELDS = (
    "status", "margin", "plan", "last_success",
    "h5_used", "h5_reset", "week_used", "week_reset",
    "included_used", "cycle_reset", "ondemand_used", "ondemand_limit",
)
LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}
STATUS_TEXT = {
    "ok": "ok",
    "token_vencido": "token vencido",
    "sin_credenciales": "sin sesión iniciada en la app",
    "sin_sesion": "esperando que inicies sesión en Windows",
    "rate_limited": "limitado por el proveedor",
    "error": "error al leer",
}


def build_status(states: dict[str, dict], summary: dict, tray_cfg: dict) -> dict:
    providers = {}
    for key, state in states.items():
        item = {f: state.get(f) for f in FIELDS}
        item["label"] = LABELS.get(key, key.capitalize())
        item["status_text"] = STATUS_TEXT.get(state.get("status"), state.get("status") or "")
        providers[key] = item
    return {
        "version": __version__,
        "updated": iso(now_utc()),
        "best": summary.get("best"),
        "providers": providers,
        "tray": {
            "enabled": bool(tray_cfg.get("enabled", True)),
            "show": list(tray_cfg.get("show") or list(providers)),
            "dashboard_url": str(tray_cfg.get("dashboard_url") or ""),
            "pin_icons": bool(tray_cfg.get("pin_icons", True)),
        },
    }


def write_status(path: Path, data: dict) -> None:
    """Escritura atómica (la bandeja nunca ve un archivo a medio escribir)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
