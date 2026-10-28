"""Pruebas de los parsers con respuestas de ejemplo (formato documentado por la comunidad)."""

import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

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
    con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', 'abc.def.ghi')")
    con.commit()
    con.close()
    assert CursorProvider({"state_db": str(db)}, S)._read_token() == "abc.def.ghi"


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
