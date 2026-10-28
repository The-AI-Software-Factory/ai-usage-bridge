"""Publicación en MQTT con Home Assistant MQTT Discovery."""

from __future__ import annotations

import json
import logging
import socket
import threading

import paho.mqtt.client as mqtt

from . import __version__

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, mcfg: dict, devices: list[tuple[str, str, str, list[dict]]], announce: bool = True):
        """devices: lista de (clave, nombre de dispositivo, modelo, entidades).

        announce=False conecta sin publicar discovery ni "online" (para pruebas y borrado).
        """
        self.cfg = mcfg
        self.announce = announce
        self.base = str(mcfg.get("base_topic", "ai_usage")).strip("/")
        self.discovery = str(mcfg.get("discovery_prefix", "homeassistant")).strip("/")
        self.avail_topic = f"{self.base}/status"
        self.devices = devices
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()
        self.connected = threading.Event()

        client_id = mcfg.get("client_id") or f"ai-usage-bridge-{socket.gethostname()}".lower()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if mcfg.get("username"):
            self.client.username_pw_set(mcfg["username"], mcfg.get("password") or None)
        if mcfg.get("tls"):
            self.client.tls_set()
            if not mcfg.get("tls_verify", True):
                self.client.tls_insecure_set(True)
        self.client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- conexión -------------------------------------------------------
    def start(self) -> None:
        host, port = self.cfg["host"], int(self.cfg.get("port", 1883))
        log.info("Conectando a MQTT %s:%s", host, port)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        if self.connected.is_set() and self.announce:
            self.client.publish(self.avail_topic, "offline", qos=1, retain=True).wait_for_publish(5)
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT rechazó la conexión: %s", reason_code)
            return
        log.info("MQTT conectado")
        if self.announce:
            client.subscribe(f"{self.discovery}/status", qos=1)
            self.publish_discovery()
            client.publish(self.avail_topic, "online", qos=1, retain=True)
            with self._lock:
                for topic, payload in self._last.items():
                    client.publish(topic, payload, qos=1, retain=True)
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected.clear()
        if reason_code != 0:
            log.warning("MQTT desconectado (%s); reintentando", reason_code)

    def _on_message(self, client, userdata, msg):
        # Home Assistant reinició: volvemos a anunciar entidades y estados.
        if msg.topic == f"{self.discovery}/status" and msg.payload == b"online":
            log.info("Home Assistant online: republicando discovery")
            self.publish_discovery()
            client.publish(self.avail_topic, "online", qos=1, retain=True)
            with self._lock:
                for topic, payload in self._last.items():
                    client.publish(topic, payload, qos=1, retain=True)

    # ---- discovery ------------------------------------------------------
    def state_topic(self, key: str) -> str:
        return f"{self.base}/{key}/state"

    def discovery_messages(self) -> list[tuple[str, dict]]:
        messages = []
        for dev_key, dev_name, model, entities in self.devices:
            device = {
                "identifiers": [f"ai_usage_bridge_{dev_key}"],
                "name": dev_name,
                "manufacturer": "AI Usage Bridge",
                "model": model,
                "sw_version": __version__,
            }
            state_topic = self.state_topic(dev_key)
            for ent in entities:
                uid = f"ai_usage_{dev_key}_{ent['key']}"
                cfg = {
                    "name": ent["name"],
                    "unique_id": uid,
                    "state_topic": state_topic,
                    "value_template": "{{ value_json.%s }}" % ent["key"],
                    "availability_topic": self.avail_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "device": device,
                }
                for src, dst in (
                    ("unit", "unit_of_measurement"),
                    ("device_class", "device_class"),
                    ("state_class", "state_class"),
                    ("icon", "icon"),
                    ("category", "entity_category"),
                ):
                    if ent.get(src):
                        cfg[dst] = ent[src]
                if ent.get("attributes"):
                    cfg["json_attributes_topic"] = state_topic
                    cfg["json_attributes_template"] = "{{ value_json.attributes | tojson }}"
                topic = f"{self.discovery}/sensor/ai_usage_{dev_key}/{ent['key']}/config"
                messages.append((topic, cfg))
        return messages

    def publish_discovery(self) -> None:
        for topic, cfg in self.discovery_messages():
            self.client.publish(topic, json.dumps(cfg, ensure_ascii=False), qos=1, retain=True)

    def remove_discovery(self) -> None:
        """Borra entidades y estados retenidos (se usa al desinstalar)."""
        infos = [self.client.publish(t, "", qos=1, retain=True) for t, _ in self.discovery_messages()]
        for dev_key, *_ in self.devices:
            infos.append(self.client.publish(self.state_topic(dev_key), "", qos=1, retain=True))
        infos.append(self.client.publish(self.avail_topic, "", qos=1, retain=True))
        for info in infos:
            info.wait_for_publish(5)

    # ---- estados --------------------------------------------------------
    def publish_state(self, key: str, state: dict) -> None:
        topic = self.state_topic(key)
        payload = json.dumps(state, ensure_ascii=False, default=str)
        with self._lock:
            self._last[topic] = payload
        if self.connected.is_set():
            self.client.publish(topic, payload, qos=1, retain=True)
