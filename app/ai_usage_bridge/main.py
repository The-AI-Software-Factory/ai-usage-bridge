"""Punto de entrada: loop del servicio y comandos de diagnóstico."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import tomllib
from pathlib import Path

import requests

from . import __version__, summary
from .providers import build_providers
from .providers.base import Provider, ProviderError, RateLimited
from .publisher import Publisher
from .util import iso, now_utc, redact, rollover_windows

log = logging.getLogger("ai_usage_bridge")


def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def setup_logging(cfg: dict, stream=sys.stdout) -> None:
    level_name = str(cfg.get("general", {}).get("log_level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=stream,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class Runner:
    def __init__(self, cfg: dict, publish: bool = True, announce: bool = True):
        self.cfg = cfg
        self.http = requests.Session()
        self.providers = build_providers(cfg, self.http)
        self.states: dict[str, dict] = {}
        self.last_good: dict[str, dict] = {}
        self.next_run = {p.key: 0.0 for p in self.providers}
        self.min_margin = float(cfg.get("general", {}).get("min_margin", 5))
        self.stop_event = threading.Event()
        self.publisher = None
        if publish:
            devices = [(p.key, p.name, p.model, p.entities) for p in self.providers]
            devices.append((summary.KEY, summary.NAME, summary.MODEL, summary.ENTITIES))
            self.publisher = Publisher(cfg["mqtt"], devices, announce=announce)

    def poll(self, p: Provider) -> dict:
        started = now_utc()
        delay = p.interval
        try:
            state = p.parse(p.fetch_raw())
            state["status"] = "ok"
            state["attributes"]["error"] = None
            state["last_success"] = iso(started)
            self.last_good[p.key] = json.loads(json.dumps(state))
            log.info("%s: ok (margen %s%%)", p.key, p.margin(state))
        except Exception as exc:  # noqa: BLE001 - el servicio no debe caerse por un proveedor
            status = getattr(exc, "status", "error")
            if isinstance(exc, RateLimited):
                delay = max(p.interval, exc.retry_after)
            elif status == "sin_credenciales":
                delay = max(p.interval, 600)
            if isinstance(exc, (ProviderError, requests.RequestException, ValueError, KeyError, OSError)):
                log.warning("%s: %s", p.key, exc)
            else:
                log.exception("%s: error inesperado", p.key)
            base = self.last_good.get(p.key)
            state = json.loads(json.dumps(base)) if base else p.empty_state()
            state["status"] = status
            state.setdefault("attributes", {})["error"] = str(exc)[:300]
        state["attributes"]["consultado"] = iso(started)
        rollover_windows(state, p.windows)
        state["margin"] = p.margin(state)
        self.states[p.key] = state
        self.next_run[p.key] = time.monotonic() + delay
        return state

    def poll_all(self) -> dict:
        for p in self.providers:
            self.poll(p)
        return {**self.states, summary.KEY: summary.build_summary(self.states, self.min_margin)}

    def run(self) -> None:
        assert self.publisher is not None
        if not str(self.cfg.get("mqtt", {}).get("host", "")).strip():
            # Recién instalado sin datos de MQTT: esperamos sin reiniciar en bucle.
            log.error("MQTT sin configurar: usá 'Configurar AI Usage Bridge' en el menú Inicio")
            self.stop_event.wait()
            return
        self.publisher.start()
        log.info("AI Usage Bridge %s iniciado (%s)", __version__, ", ".join(p.key for p in self.providers))
        while not self.stop_event.is_set():
            now = time.monotonic()
            changed = False
            for p in self.providers:
                if now >= self.next_run[p.key]:
                    self.publisher.publish_state(p.key, self.poll(p))
                    changed = True
            if changed:
                self.publisher.publish_state(
                    summary.KEY, summary.build_summary(self.states, self.min_margin)
                )
            self.stop_event.wait(1.0)
        log.info("Deteniendo")
        self.publisher.stop()


def _install_signal_handlers(runner: Runner) -> None:
    def handler(signum, frame):
        runner.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)


def cmd_dump(runner: Runner) -> int:
    for p in runner.providers:
        print(f"===== {p.key} =====")
        try:
            print(json.dumps(redact(p.fetch_raw()), indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")
    return 0


def cmd_test_mqtt(cfg: dict) -> int:
    if not str(cfg.get("mqtt", {}).get("host", "")).strip():
        print("MQTT sin configurar (falta el host)")
        return 1
    pub = Publisher(cfg["mqtt"], [], announce=False)
    pub.start()
    ok = pub.connected.wait(10)
    pub.stop()
    print("MQTT OK" if ok else "No se pudo conectar a MQTT (revisá host, puerto, usuario y contraseña)")
    return 0 if ok else 1


def cmd_remove_discovery(runner: Runner) -> int:
    if not str(runner.cfg.get("mqtt", {}).get("host", "")).strip():
        print("MQTT sin configurar: no hay entidades que borrar")
        return 0
    pub = runner.publisher
    pub.start()
    if not pub.connected.wait(10):
        print("No se pudo conectar a MQTT; no se borraron las entidades")
        return 1
    pub.remove_discovery()
    pub.client.disconnect()
    pub.client.loop_stop()
    print("Entidades de Home Assistant eliminadas")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_usage_bridge", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("AI_USAGE_CONFIG", "config.toml"))
    parser.add_argument("--once", action="store_true", help="consulta una vez e imprime el resultado")
    parser.add_argument("--publish", action="store_true", help="con --once, publica también en MQTT")
    parser.add_argument("--dump", action="store_true", help="imprime las respuestas crudas (sin secretos)")
    parser.add_argument("--test-mqtt", action="store_true", help="prueba la conexión al broker")
    parser.add_argument("--remove-discovery", action="store_true", help="borra las entidades de HA")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    # En los comandos de diagnóstico, stdout queda limpio para el resultado.
    service_mode = not (args.once or args.dump or args.test_mqtt or args.remove_discovery)
    setup_logging(cfg, sys.stdout if service_mode else sys.stderr)

    if args.test_mqtt:
        return cmd_test_mqtt(cfg)

    runner = Runner(
        cfg,
        publish=not (args.once and not args.publish) and not args.dump,
        announce=not args.remove_discovery,
    )
    if args.dump:
        return cmd_dump(runner)
    if args.remove_discovery:
        return cmd_remove_discovery(runner)
    if args.once:
        result = runner.poll_all()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if runner.publisher:
            runner.publisher.start()
            if runner.publisher.connected.wait(10):
                for key, state in result.items():
                    runner.publisher.publish_state(key, state)
                time.sleep(1)
            runner.publisher.client.disconnect()
            runner.publisher.client.loop_stop()
        return 0

    _install_signal_handlers(runner)
    runner.run()
    return 0
