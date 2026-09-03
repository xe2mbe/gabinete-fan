#!/usr/bin/env python3
"""Registra la calidad del enlace, un renglon por minuto.

Existe porque la falla es intermitente y no se deja atrapar a mano: el enlace
alterna entre impecable y 46% de perdida, y cuando esta malo tampoco se puede
entrar por SSH a medirlo. Medido el 2 de septiembre de 2026, en media hora:
46%, luego cinco minutos seguidos con 0%, luego 23%, luego 3%.

Corriendo aqui el registro sobrevive a los cortes, y cruzado con el CSV termico
-que ya guarda los segundos de TX por radio- permite distinguir tres causas que
a mano no se pueden separar:

    la perdida sigue al TX del nodo   -> RF del transmisor entrando al CPE
    la perdida sigue a la hora        -> saturacion de la celda
    la perdida no sigue a nada        -> cobertura o problema del operador

Va en Python y no en awk a proposito. La primera version usaba un pipeline de
`ping | awk` y fallo dos veces por portabilidad: el agregador ni siquiera
quedaba corriendo. Esto es mas largo pero se puede probar.
"""

from __future__ import annotations

import datetime
import os
import re
import signal
import subprocess
import sys

DESTINO = os.environ.get("DESTINO", "1.1.1.1")
INTERVALO = os.environ.get("INTERVALO", "0.5")
DIR = os.environ.get("DIR", "/var/lib/gabinete-fan/telemetria")
PREFIJO = os.environ.get("PREFIJO", "enlace-")

COLUMNAS = "hora,enviados,recibidos,perdida_pct,rtt_min,rtt_avg,rtt_max"

# Una respuesta real trae "time=". Las lineas de paquete perdido dicen
# "no answer yet for icmp_seq=123" y CONTIENEN icmp_seq, asi que contar por esa
# cadena reportaria 0% de perdida siempre, y en silencio.
_TIEMPO = re.compile(r"time=([0-9.]+)")
_MARCA = re.compile(r"^\[(\d+)\.")


class Minuto:
    """Acumulador de un minuto de pings."""

    def __init__(self, epoch_min: int):
        self.epoch_min = epoch_min
        self.enviados = 0
        self.rtts: list[float] = []

    def agrega(self, rtt: float | None) -> None:
        self.enviados += 1
        if rtt is not None:
            self.rtts.append(rtt)

    def fila(self) -> str:
        rec = len(self.rtts)
        perd = (self.enviados - rec) * 100.0 / self.enviados if self.enviados else 0.0
        hora = datetime.datetime.fromtimestamp(self.epoch_min * 60).strftime("%H:%M:00")
        if rec:
            mn, av, mx = min(self.rtts), sum(self.rtts) / rec, max(self.rtts)
        else:
            mn = av = mx = 0.0
        return f"{hora},{self.enviados},{rec},{perd:.1f},{mn:.1f},{av:.1f},{mx:.1f}"

    def ruta(self) -> str:
        dia = datetime.datetime.fromtimestamp(self.epoch_min * 60).strftime("%Y%m%d")
        return os.path.join(DIR, f"{PREFIJO}{dia}.csv")


def escribe(m: Minuto) -> None:
    """Agrega la fila del minuto. Nunca levanta: esto es telemetria."""
    if not m.enviados:
        return
    try:
        ruta = m.ruta()
        os.makedirs(DIR, exist_ok=True)
        nuevo = not os.path.exists(ruta) or os.path.getsize(ruta) == 0
        with open(ruta, "a", encoding="utf-8") as fh:
            if nuevo:
                fh.write(COLUMNAS + "\n")
            fh.write(m.fila() + "\n")
    except OSError as exc:
        print(f"no se pudo escribir el registro: {exc}", file=sys.stderr, flush=True)


def procesa(lineas, al_cerrar=escribe) -> None:
    """Lee lineas de `ping -D` y cierra cada minuto al cruzar su frontera."""
    actual: Minuto | None = None
    for linea in lineas:
        m = _MARCA.match(linea)
        if not m:
            continue
        emin = int(m.group(1)) // 60
        if actual is not None and emin > actual.epoch_min:
            al_cerrar(actual)
            actual = None
        if actual is None:
            actual = Minuto(emin)
        t = _TIEMPO.search(linea)
        actual.agrega(float(t.group(1)) if t else None)
    if actual is not None:
        al_cerrar(actual)


def main() -> int:
    # -D pone la marca de tiempo epoch al inicio de cada linea; sin ella no se
    # puede saber a que minuto pertenece un paquete perdido.
    proc = subprocess.Popen(
        ["ping", "-D", "-i", INTERVALO, "-W", "2", DESTINO],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    def adios(*_):
        proc.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, adios)
    signal.signal(signal.SIGINT, adios)

    try:
        procesa(proc.stdout)
    finally:
        proc.terminate()
    return 1  # si ping muere, systemd reinicia el servicio


if __name__ == "__main__":
    sys.exit(main())
