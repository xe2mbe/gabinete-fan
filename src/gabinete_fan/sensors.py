"""Lectura de los DS18B20 (bus 1-Wire) y del sensor interno de la Raspberry Pi."""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Reading:
    label: str
    celsius: float | None
    ok: bool
    error: str = ""


class DS18B20:
    """Un sensor del bus 1-Wire, leido via w1_slave del kernel.

    Cuenta fallos consecutivos: el controlador escala a FALLO (ventilador al 100%)
    cuando se supera max_errors, en vez de dar por buena la ultima lectura.
    """

    def __init__(self, sensor_id: str, label: str, bus_path: str,
                 sane_min: float, sane_max: float, max_errors: int):
        self.sensor_id = sensor_id
        self.label = label
        self.path = os.path.join(bus_path, sensor_id, "w1_slave")
        self.sane_min = sane_min
        self.sane_max = sane_max
        self.max_errors = max_errors
        self.consecutive_errors = 0
        self.last_good: float | None = None

    # Un w1_slave truncado o un CRC malo suelen ser transitorios: releer de
    # inmediato casi siempre funciona. Sin esto, un sensor con 2-3% de fallos
    # aislados llena el journal sin que haya nada roto.
    RETRIES = 2

    def read(self) -> Reading:
        last_error = ""
        for intento in range(self.RETRIES):
            try:
                celsius = self._read_raw()
            except (OSError, ValueError) as exc:
                last_error = str(exc)
                if intento == 0:
                    log.debug("%s: reintentando tras %s", self.label, exc)
                continue

            if intento:
                log.debug("%s: bien al reintento %d", self.label, intento + 1)
            self.consecutive_errors = 0
            self.last_good = celsius
            return Reading(self.label, celsius, ok=True)

        self.consecutive_errors += 1
        log.warning("%s: lectura fallida tras %d intentos (%d ciclos seguidos): %s",
                    self.label, self.RETRIES, self.consecutive_errors, last_error)
        return Reading(self.label, self.last_good,
                       ok=self.consecutive_errors <= self.max_errors,
                       error=last_error)

    def _read_raw(self) -> float:
        with open(self.path, "r", encoding="ascii") as fh:
            lines = fh.read().splitlines()

        # Formato del kernel: linea 1 termina en "YES"/"NO" (CRC), linea 2 trae "t=<mC>".
        if len(lines) < 2:
            raise ValueError("w1_slave truncado")
        if not lines[0].strip().endswith("YES"):
            raise ValueError("CRC invalido")

        marker = lines[1].find("t=")
        if marker < 0:
            raise ValueError("sin campo t=")
        millicelsius = int(lines[1][marker + 2:])

        # 85.0 C es el valor de power-on del DS18B20: significa "conversion no hecha",
        # tipicamente por alimentacion parasita o ruido de RF en el bus.
        if millicelsius == 85000:
            raise ValueError("85.0 C = conversion no realizada (revisa alimentacion/RF)")

        celsius = millicelsius / 1000.0
        if not self.sane_min <= celsius <= self.sane_max:
            raise ValueError(f"{celsius} C fuera del rango plausible")
        return celsius


class SensorSet:
    """Los dos DS18B20 del gabinete mas la temperatura de CPU de la Pi."""

    CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"

    def __init__(self, cfg: dict):
        common = dict(
            bus_path=cfg.get("bus_path", "/sys/bus/w1/devices"),
            sane_min=cfg.get("sane_min_c", -20.0),
            sane_max=cfg.get("sane_max_c", 110.0),
            max_errors=cfg.get("max_consecutive_errors", 3),
        )
        self.vhf = DS18B20(cfg["vhf"]["id"], cfg["vhf"].get("label", "Radio VHF"), **common)
        self.tx10m = DS18B20(cfg["tx10m"]["id"], cfg["tx10m"].get("label", "TX 10M"), **common)
        self.radios = (self.vhf, self.tx10m)

    def read_all(self) -> dict[str, Reading]:
        return {"vhf": self.vhf.read(), "tx10m": self.tx10m.read()}

    def cpu_temp(self) -> float | None:
        try:
            with open(self.CPU_TEMP_PATH, "r", encoding="ascii") as fh:
                return int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            return None

    @staticmethod
    def throttled() -> int | None:
        """Palabra de throttling del SoC, o None si vcgencmd no esta.

        Los bits 16-19 son historicos desde el ultimo arranque y los 0-3 son el
        estado actual. El que importa aqui es el 19 / el 3: limite termico suave,
        que en la Pi 3B+ baja el reloj de 1.4 a 1.2 GHz al pasar 60 C. Es la
        medida objetiva de si el gabinete le esta costando rendimiento a la Pi.
        """
        import subprocess
        try:
            salida = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
            return int(salida.split("=")[1], 16)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    @staticmethod
    def discover(bus_path: str = "/sys/bus/w1/devices") -> list[str]:
        """IDs de los DS18B20 presentes en el bus (familia 28-*)."""
        return sorted(os.path.basename(p) for p in glob.glob(os.path.join(bus_path, "28-*")))
