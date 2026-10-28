"""ChatGPT / Codex: límites de 5 h y semanal compartidos por Codex.

Usa el endpoint interno que consulta Codex, con el token de
%USERPROFILE%\\.codex\\auth.json. No es una API pública. Solo lectura: el token lo
renueva Codex cuando lo usás.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
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

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class CodexProvider(Provider):
    key = "codex"
    name = "AI Codex"
    model = "ChatGPT / Codex"
    default_interval = 120
    windows = [("h5_used", "h5_reset"), ("week_used", "week_reset")]
    entities = [
        pct_entity("h5_used", "Uso 5h", "mdi:timer-sand"),
        ts_entity("h5_reset", "Reset 5h"),
        pct_entity("week_used", "Uso semanal", "mdi:calendar-week"),
        ts_entity("week_reset", "Reset semanal"),
        {"key": "credits", "name": "Créditos", "state_class": "measurement", "icon": "mdi:cash"},
        *COMMON_ENTITIES,
    ]

    @property
    def path(self) -> Path:
        return Path(os.path.expandvars(self.cfg.get("auth_path", "")))

    def fetch_raw(self) -> dict:
        if not self.path.is_file():
            raise CredentialsMissing(f"No existe {self.path}. ¿Codex está instalado y logueado con ChatGPT?")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or {}
        token = tokens.get("access_token") or tokens.get("accessToken")
        if not token:
            raise CredentialsMissing("auth.json no tiene tokens.access_token (¿login con API key en vez de ChatGPT?)")
        account_id = tokens.get("account_id") or tokens.get("accountId")
        if not account_id:
            claims = jwt_claims(tokens.get("id_token") or token)
            account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        exp = jwt_claims(token).get("exp")
        if exp and float(exp) < time.time() + 60:
            raise TokenExpired("El token de Codex venció. Abrí Codex para que lo renueve.")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": self.cfg.get("user_agent", "ai-usage-bridge/1.0"),
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        resp = self.http.get(USAGE_URL, headers=headers, timeout=self.timeout)
        return {"usage": check_response(resp, "Codex")}

    @staticmethod
    def _window(w: dict) -> tuple[float | None, str | None, float | None]:
        used = pct(w.get("used_percent"))
        reset = parse_time(w.get("reset_at") or w.get("resets_at"))
        after = num(w.get("reset_after_seconds"))
        if reset is None and after is not None:
            reset = now_utc() + timedelta(seconds=after)
        secs = num(w.get("limit_window_seconds"))
        if secs is None and num(w.get("window_minutes")) is not None:
            secs = num(w.get("window_minutes")) * 60
        return used, iso(reset), secs

    def parse(self, raw: dict) -> dict:
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            raise ProviderError("Codex: formato de uso inesperado")
        state = self.empty_state()
        rl = usage.get("rate_limit") or usage.get("rate_limits") or {}
        named = [("primary", rl.get("primary_window") or rl.get("primary")),
                 ("secondary", rl.get("secondary_window") or rl.get("secondary"))]
        for slot, w in named:
            if not isinstance(w, dict):
                continue
            used, reset, secs = self._window(w)
            # Clasificamos por duración; si no viene, primaria = 5 h y secundaria = semanal.
            short = secs <= 6 * 3600 if secs is not None else slot == "primary"
            prefix = "h5" if short else "week"
            state[f"{prefix}_used"], state[f"{prefix}_reset"] = used, reset

        credits = usage.get("credits") or {}
        if isinstance(credits, dict):
            state["credits"] = num(credits.get("balance"))
        state["plan"] = usage.get("plan_type") or "desconocido"

        extra = {}
        for item in usage.get("additional_rate_limits") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("limit_name") or item.get("metered_feature") or item.get("name") or "extra"
            inner = item.get("rate_limit") or item
            extra[name] = {
                slot: {"uso": pct(w.get("used_percent")), "reset": iso(parse_time(w.get("reset_at")))}
                for slot, w in (("primary", inner.get("primary_window")), ("secondary", inner.get("secondary_window")))
                if isinstance(w, dict)
            }
        state["attributes"] = {
            "limite_alcanzado": rl.get("limit_reached"),
            "permitido": rl.get("allowed"),
            "limites_adicionales": extra,
            "creditos": credits if isinstance(credits, dict) else None,
            "leido": iso(now_utc()),
        }
        return state
