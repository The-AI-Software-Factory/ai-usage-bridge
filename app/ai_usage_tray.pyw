"""Lanzador de la app de bandeja de AI Usage Bridge (se ejecuta con pythonw, sin consola)."""

import sys
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
for sub in ("app", "lib"):
    path = str(INSTALL_DIR / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

from ai_usage_bridge.tray import main  # noqa: E402

raise SystemExit(main([str(INSTALL_DIR)]))
