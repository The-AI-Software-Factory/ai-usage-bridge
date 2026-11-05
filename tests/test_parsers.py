"""Pruebas de los parsers con respuestas de ejemplo (formato documentado por la comunidad)."""

import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from ai_usage_bridge import summary  # noqa: E402
from ai_usage_bridge.main import Runner  # noqa: E402
from ai_usage_bridge.providers.claude import ClaudeProvider  # noqa: E402
from ai_usage_bridge.providers.codex import CodexProvider  # noqa: E402
from ai_usage_bridge.providers.cursor import CursorProvider  # noqa: E402
from ai_usage_bridge.publisher import Publisher  # noqa: E402
from ai_usage_bridge.util import rollover_windows  # noqa: E402
from ai_usage_bridge.providers.base import TokenExpired  # noqa: E402

def _jwt(claims):
    import base64 as _b
    enc = lambda d: _b.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


FUTURE = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat()
PAST = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
S = requests.Session()


def test_claude_parse():
    raw = {
        "usage": {
            "five_hour": {"utilization": 3.0, "resets_at": FUTURE},
            "seven_day": {"utilization": 13.0, "resets_at": FUTURE},
            "seven_day_opus": None,
            "seven_day_sonnet": {"utilization": 20.0, "resets_at": FUTURE},
            "seven_day_fable": {"utilization": 1.0, "resets_at": FUTURE},
            "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None},
        },
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_5x",
    }
    p = ClaudeProvider({}, S)
    st = p.parse(raw)
    assert st["h5_used"] == 3.0 and st["week_used"] == 13.0
    assert st["week_opus_used"] is None and st["week_sonnet_used"] == 20.0
    assert st["plan"] == "max 5x"
    assert "fable" in st["attributes"]["semanal_por_modelo"]
    assert p.margin(st) == 80.0  # limitado por Sonnet semanal


def test_codex_parse():
    raw = {
        "usage": {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 9, "limit_window_seconds": 18000, "reset_after_seconds": 17700},
                "secondary_window": {"used_percent": 1, "limit_window_seconds": 604800, "reset_at": int(time.time()) + 600000},
            },
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        }
    }
    p = CodexProvider({}, S)
    st = p.parse(raw)
    assert st["h5_used"] == 9.0 and st["week_used"] == 1.0
    assert st["h5_reset"] and st["week_reset"]
    assert st["credits"] == 0.0 and st["plan"] == "plus"
    assert p.margin(st) == 91.0


def test_cursor_parse_and_db(tmp_path):
    raw = {
        "usage": {
            "billingCycleStart": "2026-10-19T00:00:00.000Z",
            "billingCycleEnd": "2026-11-19T00:00:00.000Z",
            "membershipType": "pro_plus",
            "limitType": "user",
            "individualUsage": {
                "plan": {"enabled": True, "used": 3240, "limit": 6000, "autoPercentUsed": 54, "apiPercentUsed": 0, "totalPercentUsed": 54},
                "onDemand": {"enabled": True, "used": 0, "limit": 2000},
            },
        }
    }
    p = CursorProvider({}, S)
    st = p.parse(raw)
    assert st["included_used"] == 54.0 and st["ondemand_limit"] == 20.0
    assert p.margin(st) == 46.0

    # Lectura del token desde una base estilo state.vscdb
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (_jwt({"sub": "auth0|user_123"}),))
    con.commit()
    con.close()
    token, user_id = CursorProvider({"state_db": str(db)}, S)._read_token()
    assert user_id == "user_123" and token.count(".") == 2


def test_rollover_shared_reset():
    st = {"included_used": 90.0, "auto_used": 80.0, "api_used": 5.0, "cycle_reset": PAST, "attributes": {}}
    rollover_windows(st, CursorProvider.windows)
    assert st["included_used"] == 0.0 and st["auto_used"] == 0.0 and st["cycle_reset"] is None


def test_errors_keep_last_good(tmp_path):
    cfg = {"mqtt": {"host": "x"}, "claude": {"credentials_path": str(tmp_path / "nope.json")},
           "codex": {"auth_path": str(tmp_path / "nope.json")}, "cursor": {"state_db": str(tmp_path / "nope")}}
    r = Runner(cfg, publish=False)
    out = r.poll_all()
    assert all(out[k]["status"] == "sin_credenciales" for k in ("claude", "codex", "cursor"))
    assert out["summary"]["best"] == "Ninguno"


def test_summary():
    s = summary.build_summary({"claude": {"margin": 87, "status": "ok"}, "codex": {"margin": 76, "status": "ok"},
                               "cursor": {"margin": 46, "status": "ok"}})
    assert s["best"] == "Claude" and s["ranking"].startswith("Claude 87%")


def test_discovery_payloads():
    devices = [(c.key, c.name, c.model, c.entities) for c in (ClaudeProvider, CodexProvider, CursorProvider)]
    devices.append((summary.KEY, summary.NAME, summary.MODEL, summary.ENTITIES))
    msgs = Publisher({"host": "x"}, devices).discovery_messages()
    uids = [m[1]["unique_id"] for m in msgs]
    assert len(uids) == len(set(uids))
    json.dumps([m[1] for m in msgs])
    print(len(msgs), "entidades")


# --- Renovación del token de Claude -------------------------------------------

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, ""

    def json(self):
        return self._body


class _FakeHttp:
    def __init__(self, token_responses):
        self.token_responses = dict(token_responses)
        self.posts, self.gets = [], []

    def post(self, url, **kw):
        self.posts.append(url)
        return self.token_responses.get(url, _Resp(404))

    def get(self, url, headers=None, **kw):
        self.gets.append(headers["Authorization"])
        return _Resp(200, {"five_hour": {"utilization": 7, "resets_at": FUTURE}})


def _expired_creds(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "old", "refreshToken": "r1", "expiresAt": 1000,
        "subscriptionType": "max", "scopes": ["user:inference"]}, "otro": 1}))
    return path


def test_claude_refresh_ok(tmp_path):
    from ai_usage_bridge.providers.claude import TOKEN_URLS
    path = _expired_creds(tmp_path)
    http = _FakeHttp({TOKEN_URLS[0]: _Resp(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 28800})})
    p = ClaudeProvider({"credentials_path": str(path), "refresh_token_enabled": True}, http)
    st = p.parse(p.fetch_raw())
    assert st["h5_used"] == 7.0 and http.gets == ["Bearer new"]
    saved = json.loads(path.read_text())
    assert saved["claudeAiOauth"]["accessToken"] == "new" and saved["claudeAiOauth"]["refreshToken"] == "r2"
    assert saved["claudeAiOauth"]["scopes"] == ["user:inference"] and saved["otro"] == 1
    assert saved["claudeAiOauth"]["expiresAt"] > time.time() * 1000


def test_claude_refresh_fallback_url(tmp_path):
    from ai_usage_bridge.providers.claude import TOKEN_URLS
    http = _FakeHttp({TOKEN_URLS[1]: _Resp(200, {"access_token": "new2", "expires_in": 3600})})
    p = ClaudeProvider({"credentials_path": str(_expired_creds(tmp_path)), "refresh_token_enabled": True}, http)
    p.fetch_raw()
    assert http.posts == list(TOKEN_URLS) and http.gets == ["Bearer new2"]


def test_claude_refresh_rejected(tmp_path):
    from ai_usage_bridge.providers.claude import TOKEN_URLS
    http = _FakeHttp({TOKEN_URLS[0]: _Resp(400, {"error": "invalid_grant"})})
    path = _expired_creds(tmp_path)
    p = ClaudeProvider({"credentials_path": str(path), "refresh_token_enabled": True}, http)
    try:
        p.fetch_raw()
        raise AssertionError("debía fallar")
    except TokenExpired as exc:
        assert "/login" in str(exc)
    assert json.loads(path.read_text())["claudeAiOauth"]["accessToken"] == "old"
    # No reintenta el mismo refresh token rechazado...
    try:
        p.fetch_raw()
    except TokenExpired:
        pass
    assert len(http.posts) == 1
    # ...pero sí uno nuevo (después de /login).
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["refreshToken"] = "r-new"
    path.write_text(json.dumps(data))
    http.token_responses[TOKEN_URLS[0]] = _Resp(200, {"access_token": "fresh", "expires_in": 3600})
    p.fetch_raw()
    assert http.gets == ["Bearer fresh"]


def test_claude_expired_without_refresh(tmp_path):
    p = ClaudeProvider({"credentials_path": str(_expired_creds(tmp_path))}, _FakeHttp({}))
    try:
        p.fetch_raw()
        raise AssertionError("debía fallar")
    except TokenExpired as exc:
        assert "Configurar AI Usage Bridge" in str(exc)


def test_credentials_change_triggers_poll(tmp_path):
    cred = tmp_path / ".credentials.json"
    cfg = {"mqtt": {"host": "x"}, "claude": {"credentials_path": str(cred)},
           "codex": {"enabled": False}, "cursor": {"enabled": False}}
    r = Runner(cfg, publish=False)
    p = r.providers[0]
    r.poll(p)
    assert r.states["claude"]["status"] == "sin_credenciales"
    assert not r.credentials_changed(p)
    cred.write_text("{}")  # aparece el archivo (por ejemplo, después de /login)
    assert r.credentials_changed(p)
    assert not r.credentials_changed(p)


# --- Token cifrado de Cursor (cursor-safe-storage:v1, Cursor >= 3.27) ----------

def _cursor_install(tmp_path, token_value, profile=None, wrapped_key=b"DPAPI" + b"k" * 32):
    """Arma ...\\Cursor\\Local State y ...\\Cursor\\User\\globalStorage\\state.vscdb."""
    import base64 as _b
    gs = tmp_path / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True)
    (tmp_path / "Cursor" / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": _b.b64encode(wrapped_key).decode()}}))
    con = sqlite3.connect(gs / "state.vscdb")
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", (token_value,))
    if profile is not None:
        con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/cachedScopedProfile', ?)", (json.dumps(profile),))
    con.commit()
    con.close()
    return gs / "state.vscdb"


def _seal(key, plain):
    import base64 as _b, os as _os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = _os.urandom(12)
    blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, plain.encode(), None)
    return "cursor-safe-storage:v1:" + _b.b64encode(blob).decode()


def _fake_unprotect(data):
    return data  # en el test la "clave protegida" es la clave misma


def test_cursor_encrypted_token(tmp_path):
    from ai_usage_bridge import winsecret
    key = b"k" * 32
    db = _cursor_install(tmp_path, _seal(key, _jwt({"sub": "auth0|user_abc", "exp": time.time() + 3600})))
    p = CursorProvider({"state_db": str(db)}, S)
    p._safe = winsecret.SafeStorage(p.local_state_path, unprotect=_fake_unprotect)
    token, user_id = p._read_token()
    assert user_id == "user_abc" and token.startswith("ey")


def test_cursor_opaque_token_uses_cached_profile(tmp_path):
    from ai_usage_bridge import winsecret
    db = _cursor_install(tmp_path, _seal(b"k" * 32, "opaque-token"), profile={"authId": "google-oauth2|user_xyz"})
    p = CursorProvider({"state_db": str(db)}, S)
    p._safe = winsecret.SafeStorage(p.local_state_path, unprotect=_fake_unprotect)
    assert p._read_token() == ("opaque-token", "user_xyz")


def test_cursor_wrong_key_is_reported(tmp_path):
    from ai_usage_bridge import winsecret
    from ai_usage_bridge.providers.base import ProviderError
    db = _cursor_install(tmp_path, _seal(b"x" * 32, "t"))  # cifrado con otra clave
    p = CursorProvider({"state_db": str(db)}, S)
    p._safe = winsecret.SafeStorage(p.local_state_path, unprotect=_fake_unprotect)
    try:
        p._read_token()
        raise AssertionError("debía fallar")
    except ProviderError as exc:
        assert "descifrar" in str(exc)
    assert p._safe._key is None  # la clave en memoria se descarta


def test_cursor_no_session_status(tmp_path):
    from ai_usage_bridge import winsecret
    from ai_usage_bridge.providers.base import NoSession

    def no_session(data):
        raise winsecret.NoUserSession("sin sesión")

    db = _cursor_install(tmp_path, _seal(b"k" * 32, "t"))
    p = CursorProvider({"state_db": str(db)}, S)
    p._safe = winsecret.SafeStorage(p.local_state_path, unprotect=no_session)
    try:
        p._read_token()
        raise AssertionError("debía fallar")
    except NoSession as exc:
        assert exc.status == "sin_sesion"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI real solo en Windows")
def test_cursor_encrypted_token_real_dpapi(tmp_path):
    """En Windows: clave protegida con DPAPI real, como la guarda Cursor."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    key = bytes(range(32))
    buf = ctypes.create_string_buffer(key, len(key))
    blob_in, blob_out = BLOB(len(key), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    assert ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    wrapped = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)

    db = _cursor_install(tmp_path, _seal(key, _jwt({"sub": "auth0|user_win"})), wrapped_key=b"DPAPI" + wrapped)
    token, user_id = CursorProvider({"state_db": str(db)}, S)._read_token()  # DPAPI real, sin simular
    assert user_id == "user_win"


# --- Bandeja y status.json ------------------------------------------------------

def test_status_file_has_no_secrets(tmp_path):
    from ai_usage_bridge import statusfile, summary as sm
    states = {
        "claude": {"status": "ok", "margin": 93.0, "h5_used": 3.0, "h5_reset": FUTURE, "plan": "max 5x",
                   "attributes": {"error": "Bearer sk-secret"}},
        "cursor": {"status": "sin_sesion", "margin": None, "attributes": {"error": "token xyz"}},
    }
    data = statusfile.build_status(states, sm.build_summary(states), {"dashboard_url": "http://ha:8123/ia"})
    path = tmp_path / "status.json"
    statusfile.write_status(path, data)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text and "xyz" not in text and "attributes" not in text
    loaded = json.loads(text)
    assert loaded["providers"]["claude"]["margin"] == 93.0
    assert loaded["providers"]["cursor"]["status_text"].startswith("esperando")
    assert loaded["tray"]["show"] == ["claude", "cursor"] and loaded["best"] == "Claude"


def test_tray_icons_and_tooltips():
    import datetime as dt
    from ai_usage_bridge import tray_render as tr
    assert tr.icon_text({"margin": 99.6}) == "100" and tr.icon_text({"margin": 41.1}) == "41"
    assert tr.icon_text(None) == "?" and tr.icon_text({"margin": None, "status": "token_vencido"}) == "!"
    assert tr.level_color(50) == tr.GREEN and tr.level_color(20) == tr.AMBER and tr.level_color(19.9) == tr.RED
    for size in (16, 20, 24, 32):
        img = tr.render_icon("claude", {"margin": 100}, size)
        assert img.size == (size, size) and img.getpixel((size // 2, size - 1))[3] > 0
    now = dt.datetime.now(dt.timezone.utc)
    item = {"label": "Claude", "status": "ok", "margin": 93.0, "h5_used": 3.0,
            "h5_reset": (now + dt.timedelta(hours=2)).isoformat(), "week_used": 7.0,
            "week_reset": (now + dt.timedelta(days=2)).isoformat()}
    tip = tr.tooltip("claude", item, "Claude", stale=False, now=now)
    assert tip.startswith("Claude: 93 % libre ★") and "5 h: 3 %" in tip and "Semana: 7 %" in tip
    assert len(tip) <= tr.TIP_MAX
    cur = {"label": "Cursor", "status": "ok", "margin": 41.1, "included_used": 58.9,
           "cycle_reset": (now + dt.timedelta(days=14)).isoformat(), "ondemand_used": 0.0, "ondemand_limit": 20.0}
    tip = tr.tooltip("cursor", cur, "Codex", stale=False, now=now)
    assert "Incluido: 59 %" in tip and "0.00 / 20 USD" in tip and "★" not in tip
    assert "sin datos" in tr.tooltip("codex", item, None, stale=True)
    assert tr.is_stale({"updated": now.isoformat()}, now) is False
    assert tr.is_stale({"updated": (now - dt.timedelta(hours=1)).isoformat()}, now) is True
    assert tr.is_stale({"updated": now.isoformat(), "stopped": True}, now) is True


def test_runner_writes_status(tmp_path):
    cfg = {"mqtt": {"host": ""}, "claude": {"credentials_path": str(tmp_path / "x")},
           "codex": {"enabled": False}, "cursor": {"enabled": False}, "tray": {"dashboard_url": "http://ha"}}
    r = Runner(cfg, publish=False, status_path=tmp_path / "status.json")
    r.poll(r.providers[0])
    r.write_status()
    data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert data["providers"]["claude"]["status"] == "sin_credenciales" and data["stopped"] is False
