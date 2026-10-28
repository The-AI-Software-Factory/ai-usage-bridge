"""Resumen global: a qué IA conviene mandarle la próxima tarea."""

from __future__ import annotations

from .util import iso, now_utc

KEY = "summary"
NAME = "AI Resumen"
MODEL = "Reparto de trabajo"

ENTITIES = [
    {"key": "best", "name": "Mejor destino", "icon": "mdi:robot-happy-outline"},
    {"key": "best_margin", "name": "Margen del mejor", "unit": "%", "state_class": "measurement", "icon": "mdi:gauge"},
    {"key": "ranking", "name": "Ranking", "icon": "mdi:format-list-numbered", "attributes": True},
]

LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}


def build_summary(states: dict[str, dict], min_margin: float = 5.0) -> dict:
    margins = {
        key: st.get("margin")
        for key, st in states.items()
        if st.get("margin") is not None and st.get("status") != "sin_credenciales"
    }
    ordered = sorted(margins.items(), key=lambda kv: kv[1], reverse=True)
    best_key, best_margin = ordered[0] if ordered else (None, None)
    best = LABELS.get(best_key, best_key) if best_key and best_margin >= min_margin else "Ninguno"
    ranking = " · ".join(f"{LABELS.get(k, k)} {v:.0f}%" for k, v in ordered) or "Sin datos"
    return {
        "best": best,
        "best_margin": best_margin,
        "ranking": ranking,
        "attributes": {
            "margenes": margins,
            "estados": {k: st.get("status") for k, st in states.items()},
            "actualizado": iso(now_utc()),
        },
    }
