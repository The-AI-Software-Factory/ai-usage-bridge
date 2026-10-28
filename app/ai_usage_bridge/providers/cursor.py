"""Cursor (planes individuales): % del plan incluido y gasto on-demand.

Lee el token de sesión que Cursor guarda en su base local (state.vscdb) y consulta
el mismo endpoint que usa el dashboard web. No es una API pública.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from ..util import iso, jwt_claims, now_utc, num, parse_time, pct
from .base import (
    COMMON_ENTITIES,
    CredentialsMissing,
    Provider,
    ProviderError,
    TokenExpired,
    check_response,
    pct_entity,
    ts_entity,
)

SUMMARY_URL = "https://cursor.com/api/usage-summary"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class CursorProvider(Provider):
    key = "cursor"
    name = "AI Cursor"
    model = "Cursor"
    default_interval = 300
    windows = [
        ("included_used", "cycle_reset"),
        ("auto_used", "cycle_reset"),
        ("api_used", "cycle_reset"),
    ]
    entities = [
        pct_entity("included_used", "Uso incluido", "mdi:chart-donut"),
        pct_entity("auto_used", "Uso modelos Cursor"),
        pct_entity("api_used", "Uso otros modelos"),
        {"key": "ondemand_used", "name": "On-demand usado", "unit": "USD", "state_class": "measurement", "icon": "mdi:cash-minus"},
        {"key": "ondemand_limit", "name": "On-demand límite", "unit": "USD", "icon": "mdi:cash-lock"},
        ts_entity("cycle_reset", "Fin de ciclo", "mdi:calendar-refresh"),
        *COMMON_ENTITIES,
    ]

    @property
    def path(self) -> Path:
        return Path(os.path.expandvars(self.cfg.get("state_db", "")))

    def _read_token(self) -> str:
        if self.cfg.get("session_token"):
            return self.cfg["session_token"]
        if not self.path.is_file():
            raise CredentialsMissing(f"No existe {self.path}. ¿Cursor está instalado y logueado?")
        # Copiamos la base (y su WAL) porque Cursor la mantiene abierta.
        with tempfile.TemporaryDirectory(prefix="aiub-") as tmp:
            dst = Path(tmp) / "state.vscdb"
            shutil.copy2(self.path, dst)
            for suffix in ("-wal", "-shm"):
                side = self.path.with_name(self.path.name + suffix)
                if side.is_file():
                    shutil.copy2(side, dst.with_name(dst.name + suffix))
            con = sqlite3.connect(dst)
            try:
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
                ).fetchone()
            finally:
                con.close()
        if not row or not row[0]:
            raise CredentialsMissing("Cursor no tiene sesión iniciada (cursorAuth/accessToken vacío)")
        value = row[0]
        return value.decode() if isinstance(value, bytes) else str(value)

    def fetch_raw(self) -> dict:
        token = self._read_token().strip().strip('"')
        claims = jwt_claims(token)
        exp = claims.get("exp")
        if exp and float(exp) < time.time() + 60:
            raise TokenExpired("El token de Cursor venció. Abrí Cursor para que lo renueve.")
        user_id = str(claims.get("sub", "")).split("|")[-1]
        if not user_id:
            raise CredentialsMissing("No pude obtener el userId del token de Cursor")
        resp = self.http.get(
            SUMMARY_URL,
            headers={
                "Cookie": f"WorkosCursorSessionToken={user_id}%3A%3A{token}",
                "Accept": "application/json",
                "Origin": "https://cursor.com",
                "Referer": "https://cursor.com/dashboard",
                "User-Agent": self.cfg.get("user_agent", BROWSER_UA),
            },
            timeout=self.timeout,
        )
        return {"usage": check_response(resp, "Cursor")}

    def parse(self, raw: dict) -> dict:
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            raise ProviderError("Cursor: formato de uso inesperado")
        state = self.empty_state()
        individual = usage.get("individualUsage") or {}
        plan = individual.get("plan") or {}

        total = num(plan.get("totalPercentUsed"))
        if total is None:
            used, limit = num(plan.get("used")), num(plan.get("limit"))
            total = used / limit * 100 if used is not None and limit else None
        state["included_used"] = pct(total)
        state["auto_used"] = pct(plan.get("autoPercentUsed"))
        state["api_used"] = pct(plan.get("apiPercentUsed"))

        divisor = 100.0 if self.cfg.get("amounts_in_cents", True) else 1.0
        on_demand = individual.get("onDemand") or {}
        od_used, od_limit = num(on_demand.get("used")), num(on_demand.get("limit"))
        state["ondemand_used"] = round(od_used / divisor, 2) if od_used is not None else None
        state["ondemand_limit"] = round(od_limit / divisor, 2) if od_limit is not None else None

        state["cycle_reset"] = iso(parse_time(usage.get("billingCycleEnd")))
        state["plan"] = usage.get("membershipType") or "desconocido"
        state["attributes"] = {
            "inicio_ciclo": iso(parse_time(usage.get("billingCycleStart"))),
            "on_demand_habilitado": on_demand.get("enabled"),
            "tipo_limite": usage.get("limitType"),
            "leido": iso(now_utc()),
        }
        return state

    def margin(self, state: dict) -> float | None:
        # Usamos el % incluido total; si no viene, el peor de los parciales.
        if state.get("included_used") is not None:
            return round(100.0 - state["included_used"], 1)
        return super().margin(state)
