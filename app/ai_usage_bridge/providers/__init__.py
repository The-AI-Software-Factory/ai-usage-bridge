from __future__ import annotations

import requests

from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .cursor import CursorProvider

ALL = (ClaudeProvider, CodexProvider, CursorProvider)


def build_providers(cfg: dict, http: requests.Session) -> list[Provider]:
    providers = []
    for cls in ALL:
        section = cfg.get(cls.key, {})
        if section.get("enabled", True):
            providers.append(cls(section, http))
    return providers
