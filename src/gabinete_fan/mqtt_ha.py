"""Puente MQTT con Home Assistant: auto-discovery, telemetria y comandos."""

from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

DEVICE_ID = "gabinete_asl"

# Entidades que existieron y ya no. Los mensajes de discovery se publican con
# retain, asi que dejar de generarlos NO las borra de Home Assistant: siguen
# vivas en el broker. Hay que publicar una carga vacia sobre su topico.
# Al renombrar o quitar una entidad, agregala aqui.
RETIRADAS = [
    ("sensor", "tx_seconds_today"),   # -> tx_seconds_vhf / tx_seconds_tx10m
    ("sensor", "tx_duty_pct"),        # -> tx_duty_vhf / tx_duty_tx10m
    ("sensor", "tx_keyups_today"),    # -> tx_keyups_vhf / tx_keyups_tx10m
]


def _make_client(client_id: str) -> mqtt.Client:
    """paho-mqtt 2.x exige CallbackAPIVersion; Debian 12 aun trae la 1.6."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


class HomeAssistantBridge:
    """Publica sensores y expone los umbrales como entidades editables en HA.

    Todo se anuncia por MQTT discovery, asi que no hay nada que agregar al
    configuration.yaml de Home Assistant.
    """

    def __init__(self, cfg: dict, on_param_change):
        self.cfg = cfg
        self.base = cfg.get("base_topic", "gabinete")
        self.prefix = cfg.get("discovery_prefix", "homeassistant")
        self.on_param_change = on_param_change

        self.availability_topic = f"{self.base}/status"
        self.state_topic = f"{self.base}/state"
        self.command_topic = f"{self.base}/set"

        self.client = _make_client(cfg.get("client_id", "gabinete-fan"))
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password"))

        # LWT: si el tunel WireGuard o la Pi caen, HA marca el dispositivo como
        # no disponible, que es tambien la senal de que el respaldo tomo el mando.
        self.client.will_set(self.availability_topic, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

    def start(self) -> None:
        self.client.connect_async(
            self.cfg["host"],
            int(self.cfg.get("port", 1883)),
            int(self.cfg.get("keepalive", 30)),
        )
        self.client.loop_start()

    def stop(self) -> None:
        try:
            info = self.client.publish(self.availability_topic, "offline", retain=True)
            info.wait_for_publish(2)
        except Exception as exc:
            log.warning("no se pudo anunciar la desconexion: %s", exc)
        self.client.loop_stop()
        self.client.disconnect()

    # -- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if rc != 0:
            log.error("conexion MQTT rechazada (rc=%s)", rc)
            return
        self.connected = True
        log.info("conectado al broker MQTT %s:%s",
                 self.cfg["host"], self.cfg.get("port", 1883))
        client.subscribe(f"{self.command_topic}/#")
        client.publish(self.availability_topic, "online", retain=True)
        self.retirar_obsoletas()
        self.publish_discovery()

    def _on_message(self, client, userdata, msg) -> None:
        key = msg.topic.rsplit("/", 1)[-1]
        raw = msg.payload.decode("utf-8", errors="replace").strip()
        value: object = raw
        if key != "mode":
            try:
                value = float(raw)
            except ValueError:
                log.warning("valor no numerico para %s: %r", key, raw)
                return
        ok, message = self.on_param_change(key, value)
        if not ok:
            log.warning("Home Assistant intento fijar %s=%r y fue rechazado: %s",
                        key, value, message)

    # -- discovery ---------------------------------------------------------

    def _device(self) -> dict:
        return {
            "identifiers": [DEVICE_ID],
            "name": "Gabinete ASL",
            "manufacturer": "XE2MBE",
            "model": "Control termico VHF / TX 10M",
        }

    def _publish_entity(self, component: str, object_id: str, config: dict) -> None:
        config.update({
            "unique_id": f"{DEVICE_ID}_{object_id}",
            "device": self._device(),
            "availability_topic": self.availability_topic,
            "state_topic": self.state_topic,
        })
        topic = f"{self.prefix}/{component}/{DEVICE_ID}/{object_id}/config"
        self.client.publish(topic, json.dumps(config), retain=True)

    def retirar_obsoletas(self) -> None:
        """Borra de Home Assistant las entidades que ya no se publican."""
        for componente, object_id in RETIRADAS:
            topico = f"{self.prefix}/{componente}/{DEVICE_ID}/{object_id}/config"
            self.client.publish(topico, "", retain=True)
        if RETIRADAS:
            log.info("retiradas %d entidades obsoletas de Home Assistant", len(RETIRADAS))

    def publish_discovery(self) -> None:
        sensors = [
            ("temp_vhf", "Temperatura VHF", "temperature", "°C",
             "temp_vhf", "mdi:radio"),
            ("temp_tx10m", "Temperatura TX 10M", "temperature", "°C",
             "temp_tx10m", "mdi:radio-tower"),
            ("temp_cpu", "Temperatura CPU Pi", "temperature", "°C",
             "temp_cpu", "mdi:raspberry-pi"),
            ("fan_duty", "Ventilador duty", None, "%", "fan_duty", "mdi:fan"),
            ("fan_rpm", "Ventilador RPM", None, "rpm", "fan_rpm", "mdi:fan-speed-3"),
            ("purge_remaining", "Purga restante", "duration", "s",
             "purge_remaining", "mdi:timer-sand"),
            ("tx_seconds_vhf", "VHF · tiempo en TX hoy", "duration", "s",
             "tx_seconds_vhf", "mdi:radio-tower"),
            ("tx_duty_vhf", "VHF · ciclo de trabajo", None, "%",
             "tx_duty_vhf", "mdi:percent-outline"),
            ("tx_keyups_vhf", "VHF · keyups hoy", None, None,
             "tx_keyups_vhf", "mdi:counter"),
            ("tx_seconds_tx10m", "TX 10M · tiempo en TX hoy", "duration", "s",
             "tx_seconds_tx10m", "mdi:radio-tower"),
            ("tx_duty_tx10m", "TX 10M · ciclo de trabajo", None, "%",
             "tx_duty_tx10m", "mdi:percent-outline"),
            ("tx_keyups_tx10m", "TX 10M · keyups hoy", None, None,
             "tx_keyups_tx10m", "mdi:counter"),
        ]
        for object_id, name, device_class, unit, field, icon in sensors:
            config = {
                "name": name,
                "value_template": "{{ value_json." + field + " }}",
                "icon": icon,
            }
            if device_class:
                config["device_class"] = device_class
                config["state_class"] = "measurement"
            if unit:
                config["unit_of_measurement"] = unit
            self._publish_entity("sensor", object_id, config)

        self._publish_entity("sensor", "estado", {
            "name": "Estado",
            "value_template": "{{ value_json.state }}",
            "icon": "mdi:state-machine",
        })
        self._publish_entity("sensor", "motivo", {
            "name": "Motivo",
            "value_template": "{{ value_json.reason }}",
            "icon": "mdi:information-outline",
            "entity_category": "diagnostic",
        })

        self._publish_entity("binary_sensor", "control_pi", {
            "name": "Control desde la Pi",
            "value_template": "{{ value_json.pi_in_control }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "running",
            "icon": "mdi:shield-check",
        })
        self._publish_entity("binary_sensor", "ventilador_trabado", {
            "name": "Ventilador trabado",
            "value_template": "{{ value_json.fan_stalled }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "icon": "mdi:fan-alert",
        })
        self._publish_entity("binary_sensor", "cpu_throttling", {
            "name": "CPU frenada por calor",
            "value_template": "{{ value_json.cpu_throttling }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "icon": "mdi:speedometer-slow",
        })
        self._publish_entity("binary_sensor", "cpu_throttled_ever", {
            "name": "CPU se freno alguna vez",
            "value_template": "{{ value_json.cpu_throttled_ever }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "icon": "mdi:history",
        })
        # Bajo voltaje. En la torre aparecio a los 20 minutos de instalado y no
        # se veia por ningun lado: es la falla que corrompe la microSD.
        self._publish_entity("binary_sensor", "bajo_voltaje", {
            "name": "Bajo voltaje",
            "value_template": "{{ value_json.undervoltage }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "icon": "mdi:flash-alert",
        })
        self._publish_entity("binary_sensor", "bajo_voltaje_ever", {
            "name": "Hubo bajo voltaje",
            "value_template": "{{ value_json.undervoltage_ever }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "icon": "mdi:flash-alert-outline",
        })
        self._publish_entity("binary_sensor", "frenada_ahora", {
            "name": "CPU frenada (cualquier causa)",
            "value_template": "{{ value_json.throttled_now }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "icon": "mdi:speedometer-slow",
        })
        self._publish_entity("binary_sensor", "frenada_ever", {
            "name": "Hubo frenado de CPU",
            "value_template": "{{ value_json.throttled_ever }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "entity_category": "diagnostic",
            "icon": "mdi:history",
        })
        # La palabra cruda, para que ningun bit vuelva a quedar invisible.
        self._publish_entity("sensor", "throttled_word", {
            "name": "Palabra de throttling",
            "value_template": "{{ value_json.throttled_word }}",
            "icon": "mdi:hexadecimal",
            "entity_category": "diagnostic",
        })

        self._publish_entity("binary_sensor", "falla_sensor", {
            "name": "Falla de sensor",
            "value_template": "{{ value_json.sensor_fault }}",
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "problem",
            "entity_category": "diagnostic",
        })

        numbers = [
            ("t_on", "Umbral de arranque", 25, 80, 0.5, "°C",
             "mdi:thermometer-chevron-up"),
            ("t_critical", "Umbral critico", 30, 90, 0.5, "°C",
             "mdi:thermometer-alert"),
            ("t_off", "Umbral de paro", 20, 75, 0.5, "°C",
             "mdi:thermometer-chevron-down"),
            ("hysteresis", "Histeresis", 0.5, 10, 0.5, "°C", "mdi:sine-wave"),
            ("purge_minutes", "Tiempo de purga", 0, 30, 0.5, "min", "mdi:timer-sand"),
            ("min_duty", "Duty al arrancar la rampa", 0, 100, 5, "%", "mdi:fan-chevron-down"),
            ("t_cpu", "CPU · arranque", 40, 58, 0.5, "°C", "mdi:raspberry-pi"),
            ("t_cpu_off", "CPU · paro", 35, 57, 0.5, "°C", "mdi:raspberry-pi"),
            ("manual_duty", "Duty manual", 0, 100, 5, "%", "mdi:fan-chevron-up"),
        ]
        for object_id, name, lo, hi, step, unit, icon in numbers:
            self._publish_entity("number", object_id, {
                "name": name,
                "command_topic": f"{self.command_topic}/{object_id}",
                "value_template": "{{ value_json.params." + object_id + " }}",
                "min": lo,
                "max": hi,
                "step": step,
                "unit_of_measurement": unit,
                "mode": "box",
                "icon": icon,
                "entity_category": "config",
            })

        self._publish_entity("select", "mode", {
            "name": "Modo",
            "command_topic": f"{self.command_topic}/mode",
            "value_template": "{{ value_json.params.mode }}",
            "options": ["auto", "manual", "respaldo"],
            "icon": "mdi:tune",
            "entity_category": "config",
        })
        log.info("configuracion de discovery publicada bajo %s/", self.prefix)

    # -- telemetria --------------------------------------------------------

    def publish_state(self, payload: dict) -> None:
        self.client.publish(self.state_topic, json.dumps(payload), retain=False)
