"""Estadisticas de transmision de AllStarLink.

Sirven para una sola cosa, pero importante: **normalizar comparaciones
termicas**. Sin saber cuanto transmitio el radio, comparar el pico de dos nets
distintas no dice nada — una net corta y tranquila calienta menos que una larga
y activa, aunque el ducto de aire sea el mismo.

Con el tiempo de TX se puede responder la pregunta que de verdad importa:
cuantos grados por minuto de transmision, que ya es comparable entre dias.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

log = logging.getLogger(__name__)

# "TX time today....................................: 01:01:02:638"  -> HH:MM:SS:mmm
_TX_TIME = re.compile(r"TX time today[.\s]*:\s*(\d+):(\d+):(\d+)(?::(\d+))?")
_KEYUPS = re.compile(r"Keyups today[.\s]*:\s*(\d+)")


def parse_stats(salida: str) -> tuple[float | None, int | None]:
    """Extrae (segundos de TX hoy, keyups hoy) de la salida de `rpt stats`."""
    segundos = None
    m = _TX_TIME.search(salida)
    if m:
        h, mi, s, ms = m.group(1), m.group(2), m.group(3), m.group(4) or "0"
        segundos = int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0

    keyups = None
    k = _KEYUPS.search(salida)
    if k:
        keyups = int(k.group(1))

    return segundos, keyups


class AslStats:
    """Muestrea `rpt stats` cada cierto tiempo y calcula el ciclo de trabajo.

    Se cachea a proposito: lanzar el CLI de Asterisk cuesta bastante mas que
    leer un sysfs, y estos numeros se mueven despacio. El lazo de control sondea
    cada 5 s; esto se refresca cada minuto.
    """

    def __init__(self, node: str | int, asterisk_bin: str = "/usr/sbin/asterisk",
                 refresh_seconds: float = 60.0):
        self.node = str(node)
        self.asterisk_bin = asterisk_bin
        self.refresh_seconds = refresh_seconds

        self.tx_seconds: float | None = None
        self.keyups: int | None = None
        self.duty_pct: float | None = None

        self._next_sample = 0.0
        self._prev_tx: float | None = None
        self._prev_at: float | None = None
        self._fallos = 0

    def sample(self) -> None:
        """Refresca si toca. Silencioso: esto es telemetria, no control."""
        ahora = time.monotonic()
        if ahora < self._next_sample:
            return
        self._next_sample = ahora + self.refresh_seconds

        try:
            salida = subprocess.run(
                [self.asterisk_bin, "-rx", f"rpt stats {self.node}"],
                capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            self._fallos += 1
            if self._fallos in (1, 10):   # una vez, y otra por si se vuelve cronico
                log.warning("no se pudieron leer las estadisticas del nodo %s: %s",
                            self.node, exc)
            return

        segundos, keyups = parse_stats(salida)
        if segundos is None:
            self._fallos += 1
            if self._fallos == 1:
                log.warning("`rpt stats %s` no trajo 'TX time today'; "
                            "revisa que el nodo exista", self.node)
            return

        self._fallos = 0
        self.keyups = keyups

        # Ciclo de trabajo desde la muestra anterior. El contador se reinicia a
        # medianoche, asi que un delta negativo se descarta en vez de publicar
        # un numero absurdo.
        if self._prev_tx is not None and self._prev_at is not None:
            d_tx = segundos - self._prev_tx
            d_t = ahora - self._prev_at
            if d_tx >= 0 and d_t > 0:
                self.duty_pct = max(0.0, min(100.0, d_tx / d_t * 100.0))

        self._prev_tx, self._prev_at = segundos, ahora
        self.tx_seconds = segundos


class AslNodes:
    """Un AslStats por radio, con la misma etiqueta que usa su sensor.

    Compartir etiquetas con los sensores -`vhf`, `tx10m`- permite cruzar
    directamente la temperatura de un radio con su propio ciclo de transmision,
    que es la pareja de datos que de verdad explica el comportamiento termico.
    """

    def __init__(self, cfg: dict):
        binario = cfg.get("asterisk_bin", "/usr/sbin/asterisk")
        refresco = float(cfg.get("refresh_seconds", 60))

        nodos = dict(cfg.get("nodes") or {})
        # Compatibilidad con la forma vieja de un solo nodo.
        if not nodos and cfg.get("node") is not None:
            nodos = {"vhf": cfg["node"]}

        self.por_radio = {
            etiqueta: AslStats(node=numero, asterisk_bin=binario, refresh_seconds=refresco)
            for etiqueta, numero in nodos.items()
        }

    def __bool__(self) -> bool:
        return bool(self.por_radio)

    def sample(self) -> None:
        for st in self.por_radio.values():
            st.sample()

    def payload(self) -> dict:
        datos = {}
        for etiqueta, st in self.por_radio.items():
            datos[f"tx_seconds_{etiqueta}"] = None if st.tx_seconds is None else round(st.tx_seconds)
            datos[f"tx_duty_{etiqueta}"] = None if st.duty_pct is None else round(st.duty_pct)
            datos[f"tx_keyups_{etiqueta}"] = st.keyups
        return datos

    def resumen(self) -> str:
        """Linea compacta para el journal."""
        partes = []
        for etiqueta, st in self.por_radio.items():
            if st.tx_seconds is None:
                continue
            ciclo = "" if st.duty_pct is None else f"@{st.duty_pct:.0f}%"
            partes.append(f"{etiqueta}={st.tx_seconds / 60:.1f}min{ciclo}")
        return ("  tx[" + " ".join(partes) + "]") if partes else ""
