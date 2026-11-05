"""Cursor (planes individuales): % del plan incluido y gasto on-demand.

Lee el token de sesión que Cursor guarda en su base local (state.vscdb) y consulta
el mismo endpoint que usa el dashboard web. No es una API pública.

Desde Cursor 3.27 el token se guarda cifrado (``cursor-safe-storage:v1:``); se
descifra en memoria con la clave del usuario (ver ``winsecret``).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from .. import winsecret
from ..util import iso, jwt_claims, now_utc, num, parse_time, pct
from .base import (
    COMMON_ENTITIES,
    CredentialsMissing,
    NoSession,
    Provider,
    ProviderError,
    TokenExpired,
    check_response,
    pct_entity,
    ts_entity,
)

SUMMARY_URL = "https://cursor.com/api/usage-summary"
KEYS = ("cursorAuth/accessToken", "cursorAuth/cachedScopedProfile")
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

    def __init__(self, cfg: dict, http):
        super().__init__(cfg, http)
        self._safe = winsecret.SafeStorage(self.local_state_path)

    @property
    def local_state_path(self) -> Path:
        if self.cfg.get("local_state"):
            return Path(os.path.expandvars(self.cfg["local_state"]))
        # ...\Cursor\User\globalStorage\state.vscdb -> ...\Cursor\Local State
        parents = self.path.parents
        return (parents[2] if len(parents) > 2 else self.path.parent) / "Local State"

    def _query(self, db: Path, uri: bool) -> dict[str, str]:
        target = db.resolve().as_uri() + "?mode=ro" if uri else str(db)
        con = sqlite3.connect(target, uri=uri, timeout=5)
        try:
            marks = ",".join("?" * len(KEYS))
            rows = con.execute(f"SELECT key, value FROM ItemTable WHERE key IN ({marks})", KEYS).fetchall()
        finally:
            con.close()
        return {k: (v.decode() if isinstance(v, bytes) else str(v)) for k, v in rows if v}

    def _read_items(self) -> dict[str, str]:
        if not self.path.is_file():
            raise CredentialsMissing(f"No existe {self.path}. ¿Cursor está instalado y logueado?")
        try:
            # Lectura directa en solo lectura (la base puede pesar más de 1 GB).
            return self._query(self.path, uri=True)
        except sqlite3.Error:
            pass
        # Respaldo: copia de la base y su WAL (si Cursor la tiene bloqueada).
        with tempfile.TemporaryDirectory(prefix="aiub-") as tmp:
            dst = Path(tmp) / "state.vscdb"
            shutil.copy2(self.path, dst)
            for suffix in ("-wal", "-shm"):
                side = self.path.with_name(self.path.name + suffix)
                if side.is_file():
                    shutil.copy2(side, dst.with_name(dst.name + suffix))
            return self._query(dst, uri=False)

    def _read_token(self) -> tuple[str, str]:
        """Devuelve (token, userId)."""
        if self.cfg.get("session_token"):
            token = self.cfg["session_token"]
            items = {}
        else:
            items = self._read_items()
            token = items.get("cursorAuth/accessToken", "").strip().strip('"')
            if not token:
                raise CredentialsMissing("Cursor no tiene sesión iniciada (cursorAuth/accessToken vacío)")
            if winsecret.is_encrypted(token):
                try:
                    token = self._safe.decrypt(token).strip().strip('"')
                except winsecret.NoUserSession as exc:
                    raise NoSession(str(exc)) from exc
                except winsecret.SecretError as exc:
                    raise ProviderError(f"Cursor: {exc}") from exc
        user_id = str(jwt_claims(token).get("sub", "")).split("|")[-1]
        if not user_id:
            # Respaldo: Cursor también guarda el perfil con authId = "issuer|userId".
            try:
                profile = json.loads(items.get("cursorAuth/cachedScopedProfile", "{}"))
                user_id = str(profile.get("authId", "")).split("|")[-1]
            except (ValueError, AttributeError):
                user_id = ""
        if not user_id:
            raise CredentialsMissing("No pude obtener el userId de la sesión de Cursor")
        return token, user_id

    def fetch_raw(self) -> dict:
        token, user_id = self._read_token()
        exp = jwt_claims(token).get("exp")
        if exp and float(exp) < time.time() + 60:
            raise TokenExpired("El token de Cursor venció. Abrí Cursor para que lo renueve.")
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
