"""Registro local de telemetria en CSV, un archivo por dia.

Existe para que el historial NO dependa de la red. La captura anterior se
suscribia al broker MQTT para escribir este mismo archivo, y el broker vive del
otro lado del tunel: el dato nacia en la Pi, viajaba a la casa y volvia para
guardarse en la misma Pi. Cuando el enlace se caia, el registro se cortaba
aunque el gabinete y sus sensores estuvieran perfectos.

Medido el 31 de agosto de 2026: 786 minutos -trece horas- sin una sola muestra,
exactamente la ventana del corte de 4G.

Escribiendolo aqui, el registro sobrevive a cualquier problema de red.
"""

from __future__ import annotations

import datetime
import logging
import os

log = logging.getLogger(__name__)


class CsvLog:
    """Un CSV por dia, con las mismas columnas que la captura anterior.

    El formato se conserva a proposito para que los scripts de analisis ya
    escritos sigan funcionando sin cambios.
    """

    COLUMNAS = ("hora", "vhf", "tx10m", "cpu", "estado", "duty", "rpm",
                "tx_vhf", "tx_tx10m")
    CAMPOS = ("temp_vhf", "temp_tx10m", "temp_cpu", "state", "fan_duty",
              "fan_rpm", "tx_seconds_vhf", "tx_seconds_tx10m")

    def __init__(self, directorio: str, prefijo: str = "tx-"):
        self.directorio = directorio
        self.prefijo = prefijo
        self._aviso_dado = False

    def ruta(self, ahora: datetime.datetime | None = None) -> str:
        """Ruta del archivo de HOY.

        Se recalcula en cada muestra a proposito: calcularla una sola vez al
        arrancar hace que el servicio siga escribiendo en el archivo del dia en
        que arranco, y los datos de hoy acaben dentro del CSV de ayer.
        """
        ahora = ahora or datetime.datetime.now()
        return os.path.join(self.directorio, f"{self.prefijo}{ahora:%Y%m%d}.csv")

    def escribe(self, payload: dict) -> None:
        """Agrega una fila. Nunca levanta: esto es telemetria, no control."""
        try:
            ahora = datetime.datetime.now()
            ruta = self.ruta(ahora)
            nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
            os.makedirs(self.directorio, exist_ok=True)
            with open(ruta, "a", encoding="utf-8") as fh:
                if nuevo:
                    fh.write(",".join(self.COLUMNAS) + "\n")
                valores = [ahora.strftime("%H:%M:%S")]
                valores += [str(payload.get(c)) for c in self.CAMPOS]
                fh.write(",".join(valores) + "\n")
            self._aviso_dado = False
        except OSError as exc:
            # Se avisa una sola vez por racha: un disco lleno no debe llenar
            # tambien el journal, y menos aun tumbar el control del ventilador.
            if not self._aviso_dado:
                log.warning("no se pudo escribir el CSV de telemetria (%s); "
                            "el ventilador sigue funcionando igual", exc)
                self._aviso_dado = True
