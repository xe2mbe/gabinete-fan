"""Salida PWM por hardware a 25 kHz, corte duro de +12V y lectura del tacometro."""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

PWM_SYSFS = "/sys/class/pwm"


def _detect_pwmchip() -> int:
    """Pi 5 expone el PWM del header en pwmchip2; Pi 4 y anteriores en pwmchip0."""
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as fh:
            model = fh.read()
    except OSError:
        model = ""
    return 2 if "Raspberry Pi 5" in model else 0


class HardwarePWM:
    """Canal PWM del SoC via sysfs.

    Se usa hardware y no PWM por software porque a 25 kHz el jitter de un lazo en
    Python haria audible el ventilador y desestabilizaria el duty.
    Requiere `dtoverlay=pwm,pin=18,func=2` en /boot/firmware/config.txt.
    """

    def __init__(self, chip: int | str, channel: int, frequency: int, invert: bool):
        self.chip = _detect_pwmchip() if chip in (None, "auto") else int(chip)
        self.channel = channel
        self.period_ns = int(1_000_000_000 / frequency)
        self.invert = invert
        self.base = f"{PWM_SYSFS}/pwmchip{self.chip}/pwm{self.channel}"
        self._duty = 0.0
        self._opened = False

    def open(self) -> None:
        if not os.path.isdir(f"{PWM_SYSFS}/pwmchip{self.chip}"):
            raise RuntimeError(
                f"no existe pwmchip{self.chip}. Agrega 'dtoverlay=pwm,pin=18,func=2' "
                f"a /boot/firmware/config.txt y reinicia."
            )
        if not os.path.isdir(self.base):
            self._write(f"{PWM_SYSFS}/pwmchip{self.chip}/export", str(self.channel))
            # El kernel crea el directorio de forma asincrona tras el export.
            for _ in range(50):
                if os.path.isdir(self.base):
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError(f"el kernel no creo {self.base} tras exportar")

        # El periodo no puede fijarse mientras el canal esta habilitado.
        self._write(f"{self.base}/enable", "0")
        self._write(f"{self.base}/period", str(self.period_ns))
        self._write(f"{self.base}/duty_cycle", "0")
        self._write(f"{self.base}/polarity", "inversed" if self.invert else "normal")
        self._write(f"{self.base}/enable", "1")
        self._opened = True
        log.info("PWM listo: pwmchip%d canal %d, %d ns de periodo, polaridad %s",
                 self.chip, self.channel, self.period_ns,
                 "inversed" if self.invert else "normal")

    def set_duty(self, percent: float) -> None:
        percent = max(0.0, min(100.0, float(percent)))
        self._duty = percent
        if self._opened:
            self._write(f"{self.base}/duty_cycle", str(int(self.period_ns * percent / 100.0)))

    @property
    def duty(self) -> float:
        return self._duty

    def close(self, safe_duty: float = 100.0) -> None:
        """Deja el ventilador en el estado seguro y suelta el canal.

        NO se deshabilita el canal: medido en la Pi 3B+, `enable=0` deja la
        linea en BAJO, o sea el ventilador al minimo, que es exactamente lo que
        no se quiere con el control muerto. Se deja habilitado al 100%: un
        ventilador sin quien lo gobierne debe soplar, no ronronear.
        """
        if not self._opened:
            return
        try:
            self.set_duty(safe_duty)
        except OSError as exc:
            log.warning("no se pudo dejar el PWM en estado seguro: %s", exc)
        self._opened = False

    @staticmethod
    def _write(path: str, value: str) -> None:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(value)


class Tach:
    """Cuenta los pulsos del tacometro del ventilador para estimar RPM."""

    def __init__(self, gpio: int, pulses_per_rev: int):
        from gpiozero import DigitalInputDevice

        self.pulses_per_rev = pulses_per_rev
        self._count = 0
        self._lock = threading.Lock()
        self._last_sample = time.monotonic()
        self._device = DigitalInputDevice(gpio, pull_up=True)
        self._device.when_activated = self._on_pulse

    def _on_pulse(self) -> None:
        # gpiozero llama esto desde su propio hilo. Una excepcion que se escape
        # aqui mata ese hilo y perderiamos el tacometro en silencio, asi que se
        # traga: un pulso perdido no importa, quedarse sin RPM si.
        try:
            with self._lock:
                self._count += 1
        except Exception:
            log.exception("fallo contando un pulso del tacometro")

    def rpm(self) -> float:
        """RPM desde la llamada anterior. Consume el contador."""
        now = time.monotonic()
        with self._lock:
            pulses, self._count = self._count, 0
        elapsed = now - self._last_sample
        self._last_sample = now
        if elapsed <= 0 or self.pulses_per_rev <= 0:
            return 0.0
        return pulses / elapsed * 60.0 / self.pulses_per_rev

    def close(self) -> None:
        self._device.close()


class StallDetector:
    """Detecta que el ventilador no gira aunque se le este pidiendo aire.

    Es el unico modo de falla que nada mas cubre: el ventilador es la sola pieza
    mecanica del sistema y algun dia se va a trabar. Los demas fallos terminan
    con el ventilador al 100%; este termina con la temperatura subiendo y nadie
    enterado.

    Solo se vigila cuando el duty es mayor que cero, o sea cuando de verdad se
    esta pidiendo aire. Con duty en cero las RPM legitimas dependen del montaje
    -este ventilador ronronea a 1010 RPM, otro con corte de +12 V daria 0- y
    confundir eso con una falla seria peor que no avisar.

    Logica pura y sin E/S, para poder ejercitarla en los tests.
    """

    def __init__(self, min_rpm: float = 300.0, grace_seconds: float = 20.0):
        self.min_rpm = min_rpm
        self.grace_seconds = grace_seconds
        self.quieto_desde = 0.0
        self.stalled = False

    def update(self, duty: float, rpm: float | None, dt: float) -> bool:
        # Sin tacometro no hay nada que juzgar, y sin duty no se pide aire.
        if rpm is None or duty <= 0.0:
            self._reset()
            return False

        if rpm >= self.min_rpm:
            self._reset()
            return False

        # Margen para que el ventilador tome vuelo antes de acusarlo.
        self.quieto_desde += dt
        if self.quieto_desde >= self.grace_seconds and not self.stalled:
            log.error("VENTILADOR TRABADO: %.0f%% de duty pedido y solo %.0f RPM "
                      "durante %.0f s", duty, rpm, self.quieto_desde)
            self.stalled = True
        return self.stalled

    def _reset(self) -> None:
        if self.stalled:
            log.info("el ventilador volvio a girar")
        self.quieto_desde = 0.0
        self.stalled = False


class Fan:
    """Fachada del ventilador: PWM + alimentacion + tacometro."""

    def __init__(self, cfg: dict):
        self.min_duty = float(cfg.get("min_duty", 40))
        self.max_duty = float(cfg.get("max_duty", 100))
        self.spinup_duty = float(cfg.get("spinup_duty", 100))
        self.spinup_seconds = float(cfg.get("spinup_seconds", 2))

        self.pwm = HardwarePWM(
            chip=cfg.get("pwm_chip", "auto"),
            channel=int(cfg.get("pwm_channel", 0)),
            frequency=int(cfg.get("pwm_hz", 25000)),
            invert=bool(cfg.get("invert", True)),
        )

        self._power = None
        power_gpio = cfg.get("power_gpio")
        if power_gpio is not None:
            from gpiozero import DigitalOutputDevice
            self._power = DigitalOutputDevice(
                int(power_gpio),
                active_high=bool(cfg.get("power_active_high", True)),
                initial_value=False,
            )

        self._tach = None
        tach_gpio = cfg.get("tach_gpio")
        if tach_gpio is not None:
            self._tach = Tach(int(tach_gpio), int(cfg.get("tach_pulses_per_rev", 2)))

        self._powered = False
        self._spinup_until = 0.0

    def open(self) -> None:
        self.pwm.open()

    def apply(self, duty: float) -> float:
        """Fija el duty solicitado (0 = apagar). Devuelve el duty realmente aplicado."""
        duty = max(0.0, min(self.max_duty, float(duty)))

        if duty <= 0.0:
            self._set_power(False)
            self.pwm.set_duty(0.0)
            self._spinup_until = 0.0
            return 0.0

        if not self._powered:
            self._set_power(True)
            # Patada de arranque: a duty bajo un ventilador frio puede no romper inercia.
            self._spinup_until = time.monotonic() + self.spinup_seconds

        if time.monotonic() < self._spinup_until:
            duty = max(duty, self.spinup_duty)

        self.pwm.set_duty(duty)
        return duty

    def set_raw(self, duty: float) -> None:
        """Aplica el duty exacto, sin min_duty ni patada de arranque.

        Solo para --fan-test: ahi el objetivo es justamente encontrar donde se
        atasca el ventilador, y las protecciones de apply() lo esconderian.
        """
        duty = max(0.0, min(100.0, float(duty)))
        self._set_power(duty > 0.0)
        self.pwm.set_duty(duty)
        self._spinup_until = 0.0

    def rpm(self) -> float | None:
        return self._tach.rpm() if self._tach else None

    @property
    def has_tach(self) -> bool:
        return self._tach is not None

    @property
    def has_power_switch(self) -> bool:
        return self._power is not None

    def _set_power(self, on: bool) -> None:
        self._powered = on
        if self._power is None:
            return
        self._power.on() if on else self._power.off()

    def close(self) -> None:
        """Deja el ventilador soplando al maximo y suelta los GPIO.

        Es deliberado que quede al 100% y no detenido: al salir ya no hay nadie
        vigilando la temperatura, y con los radios encendidos la suposicion
        segura es que hace calor. El respaldo por hardware, si esta armado,
        toma el mando en cuanto cesa el latido del watchdog.
        """
        self.pwm.close(safe_duty=100.0)
        if self._power is not None:
            self._power.on()      # alimentacion PUESTA: sin ella no sopla nada
            self._power.close()
        if self._tach is not None:
            self._tach.close()
