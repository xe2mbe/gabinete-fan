"""Punto de entrada del servicio gabinete-fan."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import ParamStore, load_config
from .controller import ThermalController
from .sensors import Reading, SensorSet

log = logging.getLogger("gabinete")


class SimFan:
    """Ventilador simulado para --dry-run y --selftest: no toca GPIO ni PWM."""

    def __init__(self, min_duty: float = 40.0):
        self.min_duty = min_duty
        self.applied = 0.0

    def open(self) -> None:
        log.info("ventilador SIMULADO (no se escribe ningun GPIO)")

    def apply(self, duty: float) -> float:
        self.applied = 0.0 if duty <= 0 else max(duty, self.min_duty)
        return self.applied

    def rpm(self) -> float | None:
        # Aproximacion lineal solo para ver algo coherente en la telemetria.
        return round(self.applied * 45.0, 0) if self.applied else 0.0

    def close(self) -> None:
        self.applied = 0.0


class SimStall:
    """Nunca reporta trabado: en simulacion no hay ventilador que se trabe."""

    stalled = False

    def update(self, duty, rpm, dt) -> bool:
        return False


class SimHeartbeat:
    # False a proposito: en --dry-run la Pi calcula lo que HARIA, pero no mueve
    # ningun relevador, asi que el ventilador sigue en manos del controlador
    # viejo. Publicar "la Pi manda" seria mentira en el modo de solo medicion.
    holding_control = False

    def kick(self) -> None: ...
    def release(self, released: bool) -> None: ...
    def start(self) -> None:
        log.info("watchdog SIMULADO")

    def stop(self) -> None: ...


class SimSensors:
    """Temperaturas de mentiras para verificar Home Assistant sin sensores puestos.

    Recorre en ciclo la lista de pares que se le pase, un par por cada lectura,
    de modo que en HA se ven las transiciones de estado y no un valor plano.
    """

    def __init__(self, pairs: list[tuple[float, float]], real: SensorSet):
        self.pairs = pairs
        self.real = real
        self.index = 0

    def read_all(self) -> dict[str, Reading]:
        vhf, tx = self.pairs[self.index % len(self.pairs)]
        self.index += 1
        return {"vhf": Reading("Radio VHF", vhf, ok=True),
                "tx10m": Reading("TX 10M", tx, ok=True)}

    def cpu_temp(self) -> float | None:
        return self.real.cpu_temp()   # esta si es real

    def throttled(self) -> int | None:
        return self.real.throttled()  # esta tambien


def parse_sim_temps(spec: str) -> list[tuple[float, float]]:
    """Convierte '30:31,44:47,52:48' en pares (vhf, tx10m)."""
    pairs = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            vhf, tx = chunk.split(":")
            pairs.append((float(vhf), float(tx)))
        except ValueError:
            raise SystemExit(
                f"--simulate-temps: '{chunk}' no tiene el formato VHF:TX10M, "
                f"por ejemplo 44:47 o 30:31,44:47,52:48"
            )
    if not pairs:
        raise SystemExit("--simulate-temps: no se recibio ningun par de temperaturas")
    return pairs


class Service:
    def __init__(self, cfg: dict, dry_run: bool = False, use_mqtt: bool = True,
                 sim_temps: str | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.poll = float(cfg["sensors"].get("poll_seconds", 5))

        # min_duty vivia bajo `fan:` antes de volverse ajustable desde HA.
        # Se acepta la ubicacion vieja para no romper configuraciones existentes.
        control = dict(cfg.get("control", {}))
        control.setdefault("min_duty", cfg.get("fan", {}).get("min_duty", 40))
        self.params = ParamStore(control, cfg["state_file"])
        self.sensors = SensorSet(cfg["sensors"])
        if sim_temps:
            pairs = parse_sim_temps(sim_temps)
            self.sensors = SimSensors(pairs, self.sensors)
            log.warning("TEMPERATURAS SIMULADAS: %s  (la CPU si es real)",
                        " -> ".join(f"{v:.0f}/{t:.0f}" for v, t in pairs))
        min_duty = float(self.params.params.min_duty)
        self.control = ThermalController()

        if dry_run:
            self.fan = SimFan(min_duty)
            self.watchdog = SimHeartbeat()
            self.stall = SimStall()
        else:
            from .fan import Fan, StallDetector
            from .watchdog import Heartbeat
            self.fan = Fan(cfg["fan"])
            self.watchdog = Heartbeat(cfg["watchdog"], poll_seconds=self.poll)
            self.stall = StallDetector(
                min_rpm=float(cfg["fan"].get("stall_rpm", 300)),
                grace_seconds=float(cfg["fan"].get("stall_grace_seconds", 20)))

        from .asl import AslNodes
        self.asl = AslNodes(cfg.get("asl") or {})

        self.bridge = None
        if use_mqtt:
            from .mqtt_ha import HomeAssistantBridge
            self.bridge = HomeAssistantBridge(cfg["mqtt"], self.params.set)

        self._running = True
        self._last_log = 0.0

    def stop(self, *_args) -> None:
        self._running = False

    def run(self) -> int:
        self.fan.open()
        self.watchdog.start()
        if self.bridge:
            self.bridge.start()

        last = time.monotonic()
        try:
            while self._running:
                now = time.monotonic()
                dt, last = now - last, now

                readings = self.sensors.read_all()
                params = self.params.params
                # Una sola lectura por vuelta: la CPU ahora entra al lazo de
                # control, y `throttled` cuesta un fork de vcgencmd.
                cpu = self.sensors.cpu_temp()
                thr = self.sensors.throttled()

                # En modo respaldo la Pi suelta el mando a proposito: se detiene el
                # latido, los relevadores caen a NC y el controlador original manda.
                handing_over = params.mode == "respaldo"
                self.watchdog.release(handing_over)

                if handing_over:
                    decision_state, duty, reason = "respaldo", 0.0, "control manual del respaldo"
                    self.fan.apply(0.0)
                else:
                    decision = self.control.update(readings, params, dt, cpu=cpu)
                    decision_state, reason = decision.state, decision.reason
                    duty = self.fan.apply(decision.duty)

                # Una sola lectura de RPM por vuelta: rpm() consume el contador
                # de pulsos, llamarlo dos veces partiria la medicion a la mitad.
                rpm = self.fan.rpm()
                trabado = self.stall.update(duty, rpm, dt)
                if self.asl:
                    self.asl.sample()

                self._publish(readings, params, decision_state, duty, reason,
                              rpm, trabado, cpu, thr)

                # Solo se refresca el watchdog tras completar la iteracion: si el
                # lazo se traba a medio camino, el respaldo entra por si solo.
                if not handing_over:
                    self.watchdog.kick()

                time.sleep(self.poll)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    # Resumen periodico al journal aunque MQTT este activo: si se cae el tunel,
    # el historial local sigue ahi para reconstruir lo que paso.
    LOG_EVERY_SECONDS = 300.0

    def _publish(self, readings: dict[str, Reading], params, state: str,
                 duty: float, reason: str, rpm: float | None = None,
                 trabado: bool = False, cpu: float | None = None,
                 thr: int | None = None) -> None:
        now = time.monotonic()
        if not self.bridge or (now - self._last_log) >= self.LOG_EVERY_SECONDS:
            self._last_log = now
            tx = self.asl.resumen() if self.asl else ""
            log.info("%-9s duty=%5.1f%%  rpm=%s  vhf=%s  tx10m=%s  cpu=%s  thr=%s%s | %s",
                     state, duty, "  n/d" if rpm is None else f"{rpm:5.0f}",
                     _fmt(readings["vhf"].celsius), _fmt(readings["tx10m"].celsius),
                     _fmt(cpu), "n/d" if thr is None else f"0x{thr:x}", tx, reason)
        if not self.bridge:
            return

        from dataclasses import asdict
        # Palabra de throttling del SoC. Los bits bajos son el estado AHORA; los
        # altos son "ocurrio desde el arranque" y no se apagan hasta reiniciar.
        #   bit 0 / 16 : bajo voltaje          bit 2 / 18 : frenado (cualquier causa)
        #   bit 3 / 19 : limite termico suave
        # Publicar solo el 3 y el 19 dejaba invisible el bajo voltaje: el 30 de
        # agosto la Pi estuvo en 0x50000 -bajo voltaje y frenado- con las dos
        # entidades de HA en verde.
        w = thr or 0
        payload = {
            "state": state,
            "reason": reason,
            "temp_vhf": _round(readings["vhf"].celsius),
            "temp_tx10m": _round(readings["tx10m"].celsius),
            "temp_cpu": _round(cpu),
            "cpu_throttling": "true" if w & 0x8 else "false",
            "cpu_throttled_ever": "true" if w & 0x80000 else "false",
            "undervoltage": "true" if w & 0x1 else "false",
            "undervoltage_ever": "true" if w & 0x10000 else "false",
            "throttled_now": "true" if w & 0x4 else "false",
            "throttled_ever": "true" if w & 0x40000 else "false",
            "throttled_word": "n/d" if thr is None else f"0x{thr:x}",
            "fan_duty": round(duty, 1),
            "fan_rpm": _round(rpm, 0),
            "fan_stalled": "true" if trabado else "false",
            "purge_remaining": round(self.control.purge_remaining, 0),
            "pi_in_control": "true" if self.watchdog.holding_control else "false",
            "sensor_fault": "true" if any(not r.ok for r in readings.values()) else "false",
            **(self.asl.payload() if self.asl else {}),
            "params": asdict(params),
        }
        self.bridge.publish_state(payload)

    def _shutdown(self) -> None:
        log.info("deteniendo el servicio")
        # El orden importa: primero se avisa a HA, luego se apaga el ventilador y
        # al final se suelta el watchdog, que es lo que entrega el mando al respaldo.
        if self.bridge:
            try:
                self.bridge.stop()
            except Exception as exc:
                log.warning("cierre de MQTT con errores: %s", exc)
        try:
            self.fan.close()
        except Exception as exc:
            log.warning("cierre del ventilador con errores: %s", exc)
        self.watchdog.stop()


def _fmt(value: float | None) -> str:
    return "  n/d" if value is None else f"{value:5.1f}"


def _round(value: float | None, digits: int = 1):
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Autoprueba: recorre un perfil de temperatura y muestra la linea de tiempo.
# Sirve para validar umbrales y purga sin sensores ni ventilador conectados.
# ---------------------------------------------------------------------------

PERFIL = [
    # (minutos, temp VHF, temp TX 10M, comentario)
    (0.0, 30.0, 31.0, "arranque en frio"),
    (2.0, 38.0, 40.0, "trafico ligero"),
    (4.0, 41.0, 43.5, "el TX cruza el umbral de arranque"),
    (6.0, 44.0, 47.0, "rampa"),
    (8.0, 46.0, 50.5, "TX en critico"),
    (10.0, 45.0, 49.0, "baja, pero sigue en critico por histeresis"),
    (12.0, 42.0, 44.0, "regulando"),
    (14.0, 36.0, 37.5, "cae bajo el umbral de paro -> PURGA"),
    (17.5, 34.0, 35.0, "purga cumplida a los 17.0, ventilador detenido"),
    (18.5, 33.0, 34.0, "reposo"),
    (20.0, 33.0, 43.0, "el TX vuelve a calentar"),
    (22.0, 34.0, 36.0, "enfria de nuevo -> segunda purga"),
    (23.0, None, 35.0, "el sensor VHF se cae"),
    (25.0, 34.0, 35.0, "el sensor VHF se recupera"),
]


def selftest(cfg: dict) -> int:
    # La autoprueba no debe leer ni escribir el estado persistido.
    control_cfg = dict(cfg.get("control", {}))
    control_cfg.setdefault("min_duty", cfg.get("fan", {}).get("min_duty", 40))
    store = ParamStore(control_cfg, "")
    params = store.params
    min_duty = float(params.min_duty)
    control = ThermalController()
    max_err = int(cfg["sensors"].get("max_consecutive_errors", 3))

    print(f"\nUmbrales: arranque {params.t_on} C | critico {params.t_critical} C | "
          f"paro {params.t_off} C | histeresis {params.hysteresis} C | "
          f"purga {params.purge_minutes} min | duty minimo {min_duty}%\n")
    print("Cada fila muestra el estado AL FINAL del intervalo indicado.\n")
    print(f"{'min':>6}  {'VHF':>6} {'TX10M':>6}  {'estado':<9} {'duty':>5}  motivo")
    print("-" * 92)

    step = 10.0  # segundos simulados por iteracion
    fails = 0
    for index, (minute, vhf, tx, note) in enumerate(PERFIL):
        # Avanza el tiempo simulado hasta este punto del perfil.
        span = 0.0 if index == 0 else (minute - PERFIL[index - 1][0]) * 60.0
        elapsed = 0.0
        decision = None
        while True:
            fails = fails + 1 if vhf is None else 0
            readings = {
                "vhf": Reading("Radio VHF", vhf, ok=(vhf is not None) or fails <= max_err),
                "tx10m": Reading("TX 10M", tx, ok=True),
            }
            decision = control.update(readings, params, step)
            elapsed += step
            if elapsed >= span:
                break

        print(f"{minute:>6.1f}  {_fmt(vhf)} {_fmt(tx)}  {decision.state:<9} "
              f"{decision.duty:>4.0f}%  {decision.reason}   <- {note}")

    print("-" * 92)
    print("Autoprueba completa: la maquina de estados recorrio arranque, rampa, "
          "critico con histeresis,\npurga temporizada, cancelacion de purga y "
          "fallo/recuperacion de sensor.\n")
    return 0


# ---------------------------------------------------------------------------
# Puesta en marcha de la etapa de potencia: barre el duty y mide RPM en cada
# paso. Responde tres cosas que no se pueden saber de la hoja de datos:
# a que duty arranca el ventilador, a cual se atasca al bajar, y si se detiene
# de verdad con PWM en 0 o hace falta el corte duro de +12 V.
# ---------------------------------------------------------------------------

PASOS_SUBIDA = [0, 15, 20, 25, 30, 40, 50, 60, 70, 85, 100]
PASOS_BAJADA = [85, 70, 60, 50, 40, 30, 25, 20, 15, 10, 0]


def _medir_rpm(fan, asentar: float, ventana: float) -> float | None:
    """Deja asentar la velocidad y luego cuenta pulsos durante 'ventana'."""
    time.sleep(asentar)
    if not fan.has_tach:
        return None
    fan.rpm()              # descarta lo acumulado durante el asentamiento
    time.sleep(ventana)
    return fan.rpm()


def fan_test(cfg: dict, asentar: float = 5.0, ventana: float = 3.0) -> int:
    from .fan import Fan

    fan = Fan(cfg["fan"])
    fan.open()

    print("\n=== PUESTA EN MARCHA DE LA ETAPA DE POTENCIA ===")
    print(f"  PWM      pwmchip{fan.pwm.chip} canal {fan.pwm.channel}, "
          f"{1e9 / fan.pwm.period_ns:.0f} Hz, "
          f"{'invertido (buffer open-drain)' if fan.pwm.invert else 'directo'}")
    print(f"  Tacometro {'GPIO conectado' if fan.has_tach else 'NO configurado'}"
          f"   Corte de +12 V {'presente' if fan.has_power_switch else 'ausente'}")
    print(f"  Cada paso: {asentar:.0f} s de asentamiento + {ventana:.0f} s de conteo\n")

    resultados: list[tuple[str, int, float | None]] = []
    try:
        print(f"  {'fase':<7} {'duty':>5}  {'RPM':>7}")
        print("  " + "-" * 24)
        for fase, pasos in (("subida", PASOS_SUBIDA), ("bajada", PASOS_BAJADA)):
            for duty in pasos:
                fan.set_raw(duty)
                rpm = _medir_rpm(fan, asentar, ventana)
                resultados.append((fase, duty, rpm))
                texto = "sin tach" if rpm is None else f"{rpm:7.0f}"
                print(f"  {fase:<7} {duty:>4}%  {texto}")
    except KeyboardInterrupt:
        print("\n  interrumpido")
    finally:
        fan.set_raw(0.0)
        fan.close()

    _resumen_fan_test(resultados)
    return 0


def _resumen_fan_test(resultados: list[tuple[str, int, float | None]]) -> None:
    girando = [(f, d, r) for f, d, r in resultados if r is not None and r > 60]
    if not girando:
        print("\n  Sin lecturas de tacometro. Revisa el cableado del pin 3 del "
              "ventilador,\n  el pull-up de 10 k a 3.3 V, y que tach_gpio este "
              "puesto en config.yaml.")
        return

    cero = [r for f, d, r in resultados if d == 0 and r is not None]
    piso = max(cero) if cero else 0.0
    rpm_max = max(r for _, _, r in girando)
    detenido = bool(cero) and all(r <= 60 for r in cero)

    # Muchos ventiladores ignoran el PWM por debajo de cierto duty y se quedan
    # en su piso. Ahi la rampa no hace nada, asi que min_duty tiene que empezar
    # donde el ventilador de verdad empieza a responder, no donde empieza a girar.
    umbral_util = max(piso * 1.10, piso + 100.0)
    responde = sorted(d for f, d, r in resultados
                      if f == "subida" and r is not None and r > umbral_util)
    primer_util = responde[0] if responde else None

    print("\n  --- RESUMEN ---")
    print(f"  RPM maximas medidas              : {rpm_max:.0f}")
    if cero:
        print(f"  Con PWM en 0% el ventilador      : "
              f"{'SE DETIENE' if detenido else f'SIGUE GIRANDO (~{piso:.0f} RPM)'}")
    if primer_util is not None and primer_util > 0:
        print(f"  Zona muerta                      : 0% a {primer_util}% "
              f"(sin respuesta, se queda en ~{piso:.0f} RPM)")
        print(f"  Rango util de control            : {primer_util}% a 100% "
              f"({piso:.0f} a {rpm_max:.0f} RPM)")
    else:
        print("  Zona muerta                      : ninguna, responde desde 0%")

    print("\n  --- QUE PONER EN config.yaml ---")
    if primer_util:
        print(f"  fan.min_duty: {primer_util}     donde el ventilador empieza a responder;")
        print(f"                       por debajo la rampa no movería ni un RPM")
    else:
        print("  fan.min_duty: 25     responde en todo el rango, elige por ruido")
    print(f"  fan.stall_rpm: {max(100, int(piso * 0.3))}    "
          f"muy por debajo del piso de {piso:.0f} RPM")
    if cero and not detenido:
        print("  fan.power_gpio: 24   si quieres que se DETENGA: no lo hace con PWM en 0.")
        print("                       null si aceptas el piso como reposo")
    elif cero:
        print("  fan.power_gpio: null se detiene solo con PWM en 0")


def fan_duty(cfg: dict, duty: float) -> int:
    """Fija un duty y lo sostiene, reportando RPM. Para medir con multimetro."""
    from .fan import Fan

    fan = Fan(cfg["fan"])
    fan.open()
    fan.set_raw(duty)
    print(f"\n  Duty fijo en {duty:.0f}%. Ctrl-C para salir y apagar.\n")
    try:
        while True:
            time.sleep(3)
            rpm = fan.rpm()
            print(f"    {duty:5.0f}%   {'sin tach' if rpm is None else f'{rpm:6.0f} RPM'}")
    except KeyboardInterrupt:
        print("\n  apagando")
    finally:
        fan.set_raw(0.0)
        fan.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gabinete-fan",
                                     description="Control termico del gabinete ASL")
    parser.add_argument("-c", "--config", help="ruta de config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="no toca GPIO ni PWM (ventilador simulado)")
    parser.add_argument("--no-mqtt", action="store_true",
                        help="no conectarse a Home Assistant; registra en consola")
    parser.add_argument("--selftest", action="store_true",
                        help="recorre un perfil de temperatura y muestra la linea de tiempo")
    parser.add_argument("--discover", action="store_true",
                        help="lista los DS18B20 detectados en el bus 1-Wire")
    parser.add_argument("--fan-test", action="store_true",
                        help="barre el duty y mide RPM en cada paso; encuentra el "
                             "duty minimo y si el ventilador se detiene con PWM en 0")
    parser.add_argument("--fan-duty", type=float, metavar="PCT",
                        help="fija un duty y lo sostiene reportando RPM (Ctrl-C para salir)")
    parser.add_argument("--simulate-temps", metavar="VHF:TX10M",
                        help="temperaturas falsas para verificar Home Assistant sin "
                             "sensores puestos; acepta una lista que se recorre en "
                             "ciclo, p.ej. 30:31,44:47,52:48,36:35")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.discover:
        bus = cfg["sensors"].get("bus_path", "/sys/bus/w1/devices")
        found = SensorSet.discover(bus)
        if not found:
            print(f"No se detecto ningun DS18B20 en {bus}.")
            print("Revisa: dtoverlay=w1-gpio,gpiopin=4 en config.txt, la resistencia "
                  "de 4.7k a 3.3V y el cableado.")
            return 1
        print(f"DS18B20 detectados en {bus}:")
        for sensor_id in found:
            print(f"  {sensor_id}")
        return 0

    if args.selftest:
        return selftest(cfg)

    if args.fan_test:
        return fan_test(cfg)

    if args.fan_duty is not None:
        return fan_duty(cfg, args.fan_duty)

    service = Service(cfg, dry_run=args.dry_run, use_mqtt=not args.no_mqtt,
                      sim_temps=args.simulate_temps)
    signal.signal(signal.SIGTERM, service.stop)
    signal.signal(signal.SIGINT, service.stop)
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
