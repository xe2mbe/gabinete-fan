"""Maquina de estados termica del gabinete.

    REPOSO   --(algun radio >= t_on)-------------------> ACTIVO
    ACTIVO   duty = rampa lineal de min_duty..100 entre t_on y t_critical
    ACTIVO   --(algun radio >= t_critical)-------------> CRITICO   (100%)
    CRITICO  --(todos < t_critical - hysteresis)------->  ACTIVO
    ACTIVO   --(todos < t_off)------------------------->  PURGA    (100%, temporizada)
    PURGA    --(vence el temporizador)----------------->  REPOSO
    PURGA    --(algun radio >= t_on)------------------->  ACTIVO   (cancela la purga)
    *        --(sensor caido)------------------------->  FALLO    (100%)
    FALLO    --(sensores recuperados)------------------>  reevalua

La PURGA existe porque al cortar el aire el calor radiado por el TX sigue
subiendo la temperatura del gabinete y de la Pi: se barre ese calor residual
a maximas revoluciones antes de detener el ventilador.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Params
from .sensors import Reading

log = logging.getLogger(__name__)

REPOSO = "reposo"
ACTIVO = "activo"
CRITICO = "critico"
PURGA = "purga"
FALLO = "fallo"
MANUAL = "manual"
CPU = "cpu"

ESTADOS = (REPOSO, ACTIVO, CRITICO, PURGA, FALLO, MANUAL, CPU)

# Limite termico suave del SoC: a partir de aqui la Pi baja su reloj. Es el
# ancla de la rampa de CPU, no un numero arbitrario: cuando la CPU llega aqui
# el ventilador ya tiene que estar al 100%.
CPU_LIMITE = 60.0


@dataclass
class Decision:
    state: str
    duty: float
    reason: str
    purge_remaining: float = 0.0


class ThermalController:
    """Logica pura y sin E/S: entra temperatura, sale estado + duty.

    No toca GPIO ni MQTT a proposito, para poder ejercitar cada transicion en
    los tests sin hardware.
    """

    def __init__(self):
        self.state = REPOSO
        self.purge_remaining = 0.0
        self.cpu_active = False

    def reset(self, state: str = REPOSO) -> None:
        self.state = state
        self.purge_remaining = 0.0
        self.cpu_active = False

    def update(self, readings: dict[str, Reading], params: Params, dt: float,
               cpu: float | None = None) -> Decision:
        if params.mode == "manual":
            self.state = MANUAL
            self.purge_remaining = 0.0
            self.cpu_active = False
            return Decision(MANUAL, params.manual_duty, "modo manual")

        # Los radios deciden primero; la CPU solo puede pedir MAS aire, nunca menos.
        return self._con_cpu(self._por_radios(readings, params, dt), cpu, params)

    def _por_radios(self, readings: dict[str, Reading], params: Params, dt: float) -> Decision:
        temps = [r.celsius for r in readings.values() if r.ok and r.celsius is not None]
        failed = [r.label for r in readings.values() if not r.ok]

        # Sin lectura confiable se sopla al maximo. Un sensor caido junto a un TX
        # activo es exactamente el caso en el que no se puede asumir "hace frio".
        if failed or not temps:
            if self.state != FALLO:
                log.error("sensores sin lectura confiable (%s): ventilador al 100%%",
                          ", ".join(failed) or "ninguna lectura valida")
            self.state = FALLO
            self.purge_remaining = 0.0
            return Decision(FALLO, 100.0,
                            f"sensor caido: {', '.join(failed) or 'sin lecturas'}")

        hottest = max(temps)

        if self.state == FALLO:
            log.info("sensores recuperados (max %.1f C); se reevalua el estado", hottest)
            self.state = REPOSO if hottest < params.t_on else ACTIVO

        if self.state == PURGA:
            return self._in_purge(hottest, params, dt)

        if hottest >= params.t_critical:
            self._enter(CRITICO, hottest)
            return Decision(CRITICO, 100.0, f"critico: {hottest:.1f} >= {params.t_critical:.1f}")

        if self.state == CRITICO:
            # Solo se sale de CRITICO con histeresis, para no oscilar en el umbral.
            if hottest >= params.t_critical - params.hysteresis:
                return Decision(CRITICO, 100.0,
                                f"critico con histeresis: {hottest:.1f} C")
            self._enter(ACTIVO, hottest)

        if self.state in (ACTIVO, CRITICO):
            if hottest < params.t_off:
                self._enter(PURGA, hottest)
                self.purge_remaining = params.purge_minutes * 60.0
                if self.purge_remaining <= 0:
                    self._enter(REPOSO, hottest)
                    return Decision(REPOSO, 0.0, "por debajo del umbral, purga deshabilitada")
                return Decision(PURGA, 100.0,
                                f"purga {self.purge_remaining / 60:.1f} min tras caer a "
                                f"{hottest:.1f} C", self.purge_remaining)
            duty = self._ramp(hottest, params)
            return Decision(ACTIVO, duty, f"{hottest:.1f} C -> {duty:.0f}%")

        # REPOSO
        if hottest >= params.t_on:
            self._enter(ACTIVO, hottest)
            duty = self._ramp(hottest, params)
            return Decision(ACTIVO, duty,
                            f"arranque: {hottest:.1f} >= {params.t_on:.1f}")
        return Decision(REPOSO, 0.0, f"reposo: {hottest:.1f} C")

    # -- la CPU como segundo motivo para soplar -----------------------------

    def _con_cpu(self, d: Decision, cpu: float | None, params: Params) -> Decision:
        """Manda el ventilador al 100% si la Pi se calienta, aunque los radios no.

        En la torre el gabinete se calienta desde afuera: con sol y sin trafico,
        los radios se quedan en 34 C -lejos de t_on- mientras la CPU pasa de
        55 C. Mirando solo los radios el ventilador nunca arrancaria.

        Va al maximo y no por rampa a proposito. La rampa de los radios existe
        para dosificar ruido y desgaste en operacion normal; esto es otra cosa:
        entre t_cpu y los 60 C del SoC quedan cinco grados, y a esa distancia
        del limite no hay nada que dosificar. O sopla todo o llega tarde.

        Es un piso, no un estado propio de la maquina: la logica de los radios
        corre igual por debajo (sus temporizadores de purga siguen su curso) y
        esto solo puede pedir mas aire del que ya se pidio.
        """
        if cpu is None:
            self.cpu_active = False
            return d

        # Arranca en t_cpu y para en t_cpu_off, que es umbral propio y no una
        # histeresis derivada: la lectura del SoC va en escalones de ~0.5 C, y
        # con los dos umbrales pegados el ventilador entra y sale a cada rato.
        if self.cpu_active:
            if cpu < params.t_cpu_off:
                log.info("CPU de vuelta a %.1f C (paro %.1f): deja de pedir aire",
                         cpu, params.t_cpu_off)
                self.cpu_active = False
        elif cpu >= params.t_cpu:
            log.info("CPU en %.1f C (arranque %.1f, paro %.1f, limite del SoC %.1f): "
                     "ventilador al 100%% por la Pi, no por los radios",
                     cpu, params.t_cpu, params.t_cpu_off, CPU_LIMITE)
            self.cpu_active = True

        if not self.cpu_active or d.duty >= 100.0:
            return d
        return Decision(CPU, 100.0,
                        f"CPU {cpu:.1f} C >= {params.t_cpu:.1f}: 100% (radios en {d.state})",
                        d.purge_remaining)

    # -- auxiliares --------------------------------------------------------

    def _in_purge(self, hottest: float, params: Params, dt: float) -> Decision:
        # Si el calor vuelve durante la purga, se cancela y se reanuda la regulacion.
        if hottest >= params.t_on:
            self._enter(ACTIVO, hottest)
            self.purge_remaining = 0.0
            duty = self._ramp(hottest, params)
            return Decision(ACTIVO, duty, f"purga cancelada: {hottest:.1f} C")

        self.purge_remaining = max(0.0, self.purge_remaining - dt)
        if self.purge_remaining > 0:
            return Decision(PURGA, 100.0,
                            f"purga: quedan {self.purge_remaining:.0f} s",
                            self.purge_remaining)

        self._enter(REPOSO, hottest)
        return Decision(REPOSO, 0.0, "purga completa, ventilador detenido")

    def _ramp(self, hottest: float, params: Params) -> float:
        """Rampa lineal de min_duty (en t_on) a 100% (en t_critical)."""
        span = params.t_critical - params.t_on
        if span <= 0:
            return 100.0
        fraction = (hottest - params.t_on) / span
        fraction = max(0.0, min(1.0, fraction))
        return params.min_duty + fraction * (100.0 - params.min_duty)

    def _enter(self, state: str, hottest: float) -> None:
        if state != self.state:
            log.info("estado %s -> %s (max %.1f C)", self.state, state, hottest)
            self.state = state
