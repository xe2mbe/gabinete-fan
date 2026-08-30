"""Latido de hardware que decide quien manda sobre el ventilador.

El GPIO de latido alimenta una bomba de carga (charge pump) que sostiene las
bobinas de RLY1 y RLY2. Mientras hay onda cuadrada, los relevadores estan
energizados y el ventilador cuelga de la Raspberry Pi. Si el latido se detiene
-por cuelgue, panic, apagado, o cierre limpio del servicio- el condensador se
descarga en ~2 s, los relevadores caen a su posicion de reposo (NC) y el
controlador de temperatura original recupera el ventilador.

Es deliberadamente software y no un canal PWM del SoC: un PWM por hardware
seguiria oscilando con el userspace colgado, que es justo el fallo del que hay
que protegerse. Y el hilo solo late mientras el lazo de control refresca su
marca de tiempo, de modo que un lazo trabado tambien suelta el mando.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class Heartbeat:
    # El lazo de control refresca el latido una vez por iteracion, asi que la
    # ventana de expiracion tiene que ser mayor que el periodo de sondeo o el
    # watchdog se declara muerto entre dos kicks normales y el ventilador brinca
    # entre la Pi y el respaldo. Se exige margen para dos sondeos completos.
    MARGEN_SOBRE_SONDEO = 2.0

    def __init__(self, cfg: dict, poll_seconds: float = 5.0):
        from gpiozero import DigitalOutputDevice

        self.gpio = int(cfg["heartbeat_gpio"])
        self.half_period = 1.0 / (2.0 * float(cfg.get("heartbeat_hz", 10)))

        self.stale_after = float(cfg.get("stale_after_seconds", 15))
        minimo = poll_seconds * self.MARGEN_SOBRE_SONDEO + 1.0
        if self.stale_after < minimo:
            log.warning(
                "watchdog.stale_after_seconds=%.1f es menor que el minimo seguro "
                "para poll_seconds=%.1f; se eleva a %.1f s para que el mando no "
                "oscile entre la Pi y el respaldo",
                self.stale_after, poll_seconds, minimo)
            self.stale_after = minimo

        self._device = DigitalOutputDevice(self.gpio, initial_value=False)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_kick = 0.0
        self._lock = threading.Lock()
        self._alive = False
        self._released = False

    def kick(self) -> None:
        """Llamado por el lazo de control en cada iteracion completada."""
        with self._lock:
            self._last_kick = time.monotonic()

    def release(self, released: bool) -> None:
        """Entrega deliberada del mando al controlador de respaldo (modo 'respaldo').

        Se distingue de un cuelgue para no registrarlo como error.
        """
        with self._lock:
            if released != self._released:
                log.info("mando %s por peticion del operador",
                         "entregado al respaldo" if released else "recuperado por la Pi")
            self._released = released

    @property
    def holding_control(self) -> bool:
        """True si la Pi tiene el mando (relevadores energizados)."""
        return self._alive

    def start(self) -> None:
        self.kick()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()
        log.info("watchdog iniciado en GPIO%d (%.0f Hz, expira a los %.1f s)",
                 self.gpio, 1.0 / (2 * self.half_period), self.stale_after)

    def _run(self) -> None:
        level = False
        while not self._stop.is_set():
            with self._lock:
                released = self._released
                fresh = (not released) and (time.monotonic() - self._last_kick) < self.stale_after

            if fresh:
                level = not level
                self._device.value = level
                if not self._alive:
                    log.info("watchdog activo: la Raspberry Pi toma el control del ventilador")
                    self._alive = True
            else:
                if self._alive:
                    if released:
                        log.info("modo respaldo: el ventilador pasa al controlador original")
                    else:
                        log.error("lazo de control detenido >%.1f s: se entrega el ventilador "
                                  "al controlador de respaldo", self.stale_after)
                    self._alive = False
                self._device.off()

            self._stop.wait(self.half_period)

    def stop(self) -> None:
        """Detiene el latido y deja el GPIO en bajo: el respaldo toma el ventilador."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._alive = False
        try:
            self._device.off()
            self._device.close()
        except Exception as exc:  # el cierre nunca debe impedir la entrega del mando
            log.warning("no se pudo cerrar el GPIO del watchdog: %s", exc)
        log.info("watchdog detenido: el controlador de respaldo tiene el ventilador")
