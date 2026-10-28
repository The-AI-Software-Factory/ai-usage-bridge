"""Claude (Pro/Max): uso de las ventanas de 5 h y semanal.

Usa el endpoint interno que consulta Claude Code, con el token OAuth que Claude
Code guarda en %USERPROFILE%\\.claude\\.credentials.json. No es una API pública.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import logging

import requests

from ..util import iso, now_utc, num, parse_time, pct
from .base import (
    COMMON_ENTITIES,
    CredentialsMissing,
    Provider,
    ProviderError,
    RateLimited,
    TokenExpired,
    check_response,
    pct_entity,
    ts_entity,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Endpoint actual de Claude Code y el anterior como respaldo.
TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
log = logging.getLogger(__name__)
RELOGIN_HINT = (
    "Abrí una terminal, ejecutá 'claude' y luego '/login' una vez; "
    "con refresh_token_enabled = true el servicio lo mantiene renovado"
)


class ClaudeProvider(Provider):
    key = "claude"
    name = "AI Claude"
    model = "Claude (Pro/Max)"
    default_interval = 300
    windows = [
        ("h5_used", "h5_reset"),
        ("week_used", "week_reset"),
        ("week_opus_used", "week_opus_reset"),
        ("week_sonnet_used", "week_sonnet_reset"),
    ]
    entities = [
        pct_entity("h5_used", "Uso 5h", "mdi:timer-sand"),
        ts_entity("h5_reset", "Reset 5h"),
        pct_entity("week_used", "Uso semanal", "mdi:calendar-week"),
        ts_entity("week_reset", "Reset semanal"),
        pct_entity("week_opus_used", "Uso semanal Opus"),
        ts_entity("week_opus_reset", "Reset semanal Opus"),
        pct_entity("week_sonnet_used", "Uso semanal Sonnet"),
        ts_entity("week_sonnet_reset", "Reset semanal Sonnet"),
        {"key": "extra_used", "name": "Créditos extra usados", "state_class": "measurement", "icon": "mdi:cash-plus"},
        *COMMON_ENTITIES,
    ]

    @property
    def path(self) -> Path:
        return Path(os.path.expandvars(self.cfg.get("credentials_path", "")))

    def _read_creds(self) -> tuple[dict, dict]:
        if not self.path.is_file():
            raise CredentialsMissing(f"No existe {self.path}. ¿Claude Code está instalado y logueado?")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        if not oauth.get("accessToken"):
            raise CredentialsMissing("El archivo de credenciales no tiene claudeAiOauth.accessToken")
        return data, oauth

    def _refresh(self, data: dict, oauth: dict) -> str:
        """Renueva el token y lo vuelve a escribir para que Claude Code siga funcionando."""
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            raise TokenExpired(f"El token de Claude Code venció y no hay refreshToken. {RELOGIN_HINT}")
        urls = [self.cfg["token_url"]] if self.cfg.get("token_url") else list(TOKEN_URLS)
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.cfg.get("client_id", CLAUDE_CODE_CLIENT_ID),
        }
        body = None
        last_error = "sin respuesta"
        for url in urls:
            try:
                resp = self.http.post(url, json=payload, timeout=self.timeout,
                                      headers={"Accept": "application/json"})
            except requests.RequestException as exc:
                last_error = f"{url}: {exc.__class__.__name__}"
                continue
            if resp.status_code == 429:
                try:
                    retry = float(resp.headers.get("retry-after", "900"))
                except ValueError:
                    retry = 900.0
                raise RateLimited("Claude: renovación de token limitada (HTTP 429)", max(retry, 300.0))
            if resp.status_code in (400, 401, 403):
                # invalid_grant: el refresh token ya no sirve (vencido o usado por otro proceso).
                raise TokenExpired(
                    f"Claude rechazó la renovación del token (HTTP {resp.status_code}). {RELOGIN_HINT}"
                )
            if resp.status_code >= 400:
                last_error = f"{url}: HTTP {resp.status_code}"
                continue
            try:
                body = resp.json()
            except ValueError:
                last_error = f"{url}: respuesta no JSON"
                continue
            if body.get("access_token"):
                break
            last_error = f"{url}: respuesta sin access_token"
            body = None
        if body is None:
            raise ProviderError(f"No se pudo renovar el token de Claude ({last_error})")
        log.info("claude: token de Claude Code renovado")
        # Si Claude Code renovó el token mientras tanto, usamos el suyo y no pisamos nada.
        current_data, current = self._read_creds()
        if current.get("refreshToken") != refresh_token:
            return current["accessToken"]
        current["accessToken"] = body["access_token"]
        if body.get("refresh_token"):
            current["refreshToken"] = body["refresh_token"]
        if body.get("expires_in"):
            current["expiresAt"] = int((time.time() + int(body["expires_in"])) * 1000)
        current_data["claudeAiOauth"] = current
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(current_data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return current["accessToken"]

    def fetch_raw(self) -> dict:
        data, oauth = self._read_creds()
        token = oauth["accessToken"]
        expires = parse_time(oauth.get("expiresAt"))
        refresh_enabled = bool(self.cfg.get("refresh_token_enabled", False))
        if expires is not None and expires.timestamp() < time.time() + 60:
            if not refresh_enabled:
                raise TokenExpired(
                    f"El token de Claude Code venció ({iso(expires)}). Si usás Claude desde la app "
                    "o la web, activá la renovación automática (menú Inicio > Configurar AI Usage "
                    "Bridge, o refresh_token_enabled = true en config.toml); si no, abrí Claude Code"
                )
            token = self._refresh(data, oauth)

        def call(tok: str):
            return self.http.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {tok}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                    "User-Agent": self.cfg.get("user_agent", "ai-usage-bridge/1.0"),
                },
                timeout=self.timeout,
            )

        resp = call(token)
        if resp.status_code == 401 and refresh_enabled:
            resp = call(self._refresh(*self._read_creds()))
        usage = check_response(resp, "Claude")
        return {
            "usage": usage,
            "subscriptionType": oauth.get("subscriptionType"),
            "rateLimitTier": oauth.get("rateLimitTier"),
        }

    def parse(self, raw: dict) -> dict:
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            raise ProviderError("Claude: formato de uso inesperado")
        state = self.empty_state()

        def window(name: str) -> tuple[float | None, str | None]:
            w = usage.get(name)
            if not isinstance(w, dict):
                return None, None
            return pct(w.get("utilization")), iso(parse_time(w.get("resets_at")))

        state["h5_used"], state["h5_reset"] = window("five_hour")
        state["week_used"], state["week_reset"] = window("seven_day")
        state["week_opus_used"], state["week_opus_reset"] = window("seven_day_opus")
        state["week_sonnet_used"], state["week_sonnet_reset"] = window("seven_day_sonnet")

        extra = usage.get("extra_usage") or {}
        if isinstance(extra, dict) and extra.get("is_enabled"):
            state["extra_used"] = num(extra.get("used_credits"))

        # Ventanas semanales por modelo que no tengan entidad propia (p. ej. modelos nuevos).
        per_model = {}
        for k, v in usage.items():
            if k.startswith("seven_day_") and isinstance(v, dict):
                per_model[k.removeprefix("seven_day_")] = {
                    "uso": pct(v.get("utilization")),
                    "reset": iso(parse_time(v.get("resets_at"))),
                }
        tier = raw.get("rateLimitTier") or ""
        plan = raw.get("subscriptionType") or "desconocido"
        if "20x" in tier:
            plan += " 20x"
        elif "5x" in tier:
            plan += " 5x"
        state["plan"] = plan
        state["attributes"] = {
            "semanal_por_modelo": per_model,
            "creditos_extra": extra if isinstance(extra, dict) else None,
            "leido": iso(now_utc()),
        }
        return state
