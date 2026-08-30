"""Carga de configuracion y parametros ajustables en caliente desde Home Assistant."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/gabinete-fan/config.yaml"


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("GABINETE_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except PermissionError:
        # El archivo es 640 porque guarda la contrasena del broker MQTT.
        raise SystemExit(
            f"No se puede leer {path}: hace falta sudo.\n"
            f"  sudo PYTHONPATH=/opt/gabinete-fan/src python3 -m gabinete_fan ..."
        ) from None
    except FileNotFoundError:
        raise SystemExit(
            f"No existe {path}. Corre ./install.sh, o indica otra ruta con -c."
        ) from None
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: se esperaba un mapeo YAML en la raiz")
    return cfg


@dataclass
class Params:
    """Parametros que Home Assistant puede modificar en caliente.

    Las restricciones t_off < t_on < t_critical se imponen en validate(); un valor
    invalido llegado por MQTT se rechaza y se conserva el anterior.
    """

    t_on: float = 42.0
    t_critical: float = 50.0
    t_off: float = 38.0
    hysteresis: float = 2.0
    purge_minutes: float = 3.0
    mode: str = "auto"          # auto | manual | respaldo
    manual_duty: float = 60.0
    # Duty donde arranca la rampa al cruzar t_on. Es ajustable en caliente
    # porque su valor correcto depende del ventilador: uno con zona muerta
    # necesita empezar arriba de ella o los primeros grados no mueven aire.
    min_duty: float = 40.0
    # Temperatura de CPU a la que el ventilador se va al 100% aunque los radios
    # esten frios. Con el gabinete en la torre el sol lo calienta desde afuera y
    # la Pi se cuece sin que nadie transmita; mirando solo los radios nadie
    # soplaria. Al maximo y no por rampa: del umbral al limite del SoC quedan
    # cinco grados, y no hay nada que dosificar tan cerca del borde.
    t_cpu: float = 55.0
    # Temperatura a la que el ventilador deja de soplar por la Pi. Es umbral
    # propio y no t_cpu menos la histeresis: esa histeresis es la de los radios,
    # y el ancho util aqui depende de que tan abajo puede llevar la CPU el
    # ventilador, que es otra pregunta. Muy pegado a t_cpu -la CPU se lee en
    # escalones de ~0.5 C- salen ciclos cortos de encendido y apagado.
    t_cpu_off: float = 53.0

    MODES = ("auto", "manual", "respaldo")

    # Rangos aceptados por cada parametro (los mismos que se publican a HA).
    LIMITS = {
        "t_on": (25.0, 80.0),
        "t_critical": (30.0, 90.0),
        "t_off": (20.0, 75.0),
        "hysteresis": (0.5, 10.0),
        "purge_minutes": (0.0, 30.0),
        "manual_duty": (0.0, 100.0),
        "min_duty": (0.0, 100.0),
        # El tope queda por debajo de los 60 C del SoC: un umbral puesto en el
        # limite mismo haria arrancar el ventilador cuando ya se esta frenando.
        "t_cpu": (40.0, 58.0),
        "t_cpu_off": (35.0, 57.0),
    }

    def validate(self) -> None:
        for name, (lo, hi) in self.LIMITS.items():
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not lo <= float(value) <= hi:
                raise ValueError(f"{name}={value!r} fuera de rango [{lo}, {hi}]")
        if self.mode not in self.MODES:
            raise ValueError(f"mode={self.mode!r} no es uno de {self.MODES}")
        if not (self.t_off < self.t_on < self.t_critical):
            raise ValueError(
                f"se requiere t_off < t_on < t_critical, "
                f"se recibio {self.t_off} / {self.t_on} / {self.t_critical}"
            )
        if not self.t_cpu_off < self.t_cpu:
            raise ValueError(
                f"se requiere t_cpu_off < t_cpu, "
                f"se recibio {self.t_cpu_off} / {self.t_cpu}"
            )

    def copy_with(self, key: str, value) -> "Params":
        data = asdict(self)
        data[key] = value
        return Params(**data)


class ParamStore:
    """Params + persistencia atomica en disco. Seguro para varios hilos."""

    def __init__(self, defaults: dict, state_file: str):
        self._state_file = state_file
        self._lock = threading.Lock()

        known = {f.name for f in fields(Params)}
        base = Params(**{k: v for k, v in (defaults or {}).items() if k in known})
        try:
            base.validate()
        except ValueError as exc:
            raise ValueError(f"config.yaml seccion 'control' invalida: {exc}") from exc

        self._params = base
        self._load_overrides()

    @property
    def params(self) -> Params:
        with self._lock:
            return self._params

    def set(self, key: str, value) -> tuple[bool, str]:
        """Aplica un cambio venido de HA. Devuelve (aplicado, mensaje)."""
        if key not in {f.name for f in fields(Params)}:
            return False, f"parametro desconocido: {key}"
        with self._lock:
            candidate = self._params.copy_with(key, value)
            try:
                candidate.validate()
            except ValueError as exc:
                return False, str(exc)
            self._params = candidate
            self._save()
        log.info("parametro %s = %s (desde Home Assistant)", key, value)
        return True, "ok"

    # -- persistencia ------------------------------------------------------

    def _load_overrides(self) -> None:
        if not self._state_file:
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("no se pudo leer %s (%s); se usan los valores de config.yaml",
                        self._state_file, exc)
            return

        known = {f.name for f in fields(Params)}
        merged = asdict(self._params) | {k: v for k, v in saved.items() if k in known}
        candidate = Params(**merged)
        try:
            candidate.validate()
        except ValueError as exc:
            log.warning("state.json invalido (%s); se usan los valores de config.yaml", exc)
            return
        self._params = candidate
        log.info("parametros restaurados desde %s", self._state_file)

    def _save(self) -> None:
        if not self._state_file:
            return
        directory = os.path.dirname(self._state_file) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(asdict(self._params), fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as exc:
            log.error("no se pudo guardar %s: %s", self._state_file, exc)
