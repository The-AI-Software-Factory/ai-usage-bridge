"""Clase base de proveedores y errores comunes."""

from __future__ import annotations

import requests


class ProviderError(Exception):
    """Error genérico al consultar un proveedor."""

    status = "error"


class CredentialsMissing(ProviderError):
    status = "sin_credenciales"


class TokenExpired(ProviderError):
    status = "token_vencido"


class RateLimited(ProviderError):
    status = "rate_limited"

    def __init__(self, message: str, retry_after: float = 900.0):
        super().__init__(message)
        self.retry_after = retry_after


def check_response(resp: requests.Response, name: str) -> dict:
    """Valida la respuesta HTTP y devuelve el JSON."""
    if resp.status_code == 429:
        try:
            retry = float(resp.headers.get("retry-after", "900"))
        except ValueError:
            retry = 900.0
        raise RateLimited(f"{name}: HTTP 429, reintento en {int(retry)} s", max(retry, 300.0))
    if resp.status_code in (401, 403):
        raise TokenExpired(f"{name}: HTTP {resp.status_code} (token inválido o vencido)")
    if resp.status_code >= 400:
        raise ProviderError(f"{name}: HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(f"{name}: la respuesta no es JSON") from exc


class Provider:
    key = ""
    name = ""
    model = ""
    default_interval = 300
    # Pares (clave de uso %, clave de reset) que vuelven a 0 cuando pasa el reset.
    windows: list[tuple[str, str]] = []
    # Definición de entidades para Home Assistant.
    entities: list[dict] = []

    def __init__(self, cfg: dict, http: requests.Session):
        self.cfg = cfg
        self.http = http
        self.interval = int(cfg.get("interval_seconds", self.default_interval))
        self.timeout = float(cfg.get("timeout_seconds", 20))

    def empty_state(self) -> dict:
        state = {e["key"]: None for e in self.entities}
        state["attributes"] = {}
        return state

    def fetch_raw(self) -> dict:
        raise NotImplementedError

    def parse(self, raw: dict) -> dict:
        raise NotImplementedError

    def margin(self, state: dict) -> float | None:
        """Margen disponible 0-100 (cuanto más alto, más trabajo se le puede dar)."""
        values = [100.0 - state[k] for k, _ in self.windows if state.get(k) is not None]
        return round(min(values), 1) if values else None


# Entidades comunes a todos los proveedores.
COMMON_ENTITIES = [
    {"key": "margin", "name": "Margen", "unit": "%", "state_class": "measurement", "icon": "mdi:gauge"},
    {"key": "plan", "name": "Plan", "icon": "mdi:card-account-details-outline", "category": "diagnostic"},
    {"key": "status", "name": "Estado", "icon": "mdi:check-network-outline", "category": "diagnostic", "attributes": True},
    {"key": "last_success", "name": "Última lectura", "device_class": "timestamp", "category": "diagnostic"},
]


def pct_entity(key: str, name: str, icon: str = "mdi:percent") -> dict:
    return {"key": key, "name": name, "unit": "%", "state_class": "measurement", "icon": icon}


def ts_entity(key: str, name: str, icon: str = "mdi:timer-refresh-outline") -> dict:
    return {"key": key, "name": name, "device_class": "timestamp", "icon": icon}
