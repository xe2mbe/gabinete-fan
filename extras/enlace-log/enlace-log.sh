#!/usr/bin/env bash
#
# Registra la calidad del enlace, un renglon por minuto.
#
# Existe porque la falla es intermitente y no se deja atrapar a mano: el enlace
# alterna entre impecable y 46% de perdida, y cuando esta malo tampoco se puede
# entrar por SSH a medirlo. Medido el 2 de septiembre de 2026: cinco minutos
# seguidos con 0% de perdida, y minutos despues 46%.
#
# Corriendo aqui, el registro sobrevive a los cortes y permite responder dos
# preguntas que a mano no se pueden:
#
#   1. La perdida sigue al TX del nodo?  -> seria RF del transmisor
#   2. La perdida sigue a la hora del dia? -> seria saturacion de la celda
#
# Se cruza con el CSV termico, que ya guarda los segundos de TX por radio.
#
set -uo pipefail

DESTINO="${DESTINO:-1.1.1.1}"
INTERVALO="${INTERVALO:-0.5}"          # segundos entre pings
DIR="${DIR:-/var/lib/gabinete-fan/telemetria}"
PREFIJO="${PREFIJO:-enlace-}"

mkdir -p "$DIR"

# ping -D pone una marca de tiempo epoch al inicio de cada linea. Se agrega por
# minuto con awk y se escribe un renglon al cerrar cada minuto, de modo que el
# archivo esta siempre al dia aunque el proceso se muera de golpe.
exec ping -D -i "$INTERVALO" -W 2 "$DESTINO" 2>&1 | awk -v dir="$DIR" -v pre="$PREFIJO" '
function cierra(m,   f, perd, hora) {
    if (tot[m] == 0) return
    perd = (tot[m] - ok[m]) * 100.0 / tot[m]
    f = dir "/" pre strftime("%Y%m%d", m*60) ".csv"
    if (!(f in vistos)) {
        # se agrega la cabecera solo si el archivo esta vacio o no existe
        if ((getline linea < f) <= 0)
            print "hora,enviados,recibidos,perdida_pct,rtt_min,rtt_avg,rtt_max" > f
        close(f); vistos[f] = 1
    }
    hora = strftime("%H:%M:00", m*60)
    printf "%s,%d,%d,%.1f,%.1f,%.1f,%.1f\n", hora, tot[m], ok[m], perd,
           (ok[m] ? mn[m] : 0), (ok[m] ? sum[m]/ok[m] : 0), (ok[m] ? mx[m] : 0) >> f
    fflush(f)
    delete tot[m]; delete ok[m]; delete sum[m]; delete mn[m]; delete mx[m]
}
/^\[/ {
    ts = $1; gsub(/[\[\]]/, "", ts); m = int(ts / 60)
    if (minuto_actual != "" && m > minuto_actual) cierra(minuto_actual)
    minuto_actual = m
    tot[m]++
    # OJO: se busca "time=", no "icmp_seq". Las lineas de paquete perdido dicen
    # "no answer yet for icmp_seq=123" y CONTIENEN icmp_seq, asi que buscar eso
    # contaba las perdidas como recibidas y reportaba 0% de perdida siempre.
    if ($0 ~ /time=/) {
        ok[m]++
        if (match($0, /time=[0-9.]+/)) {
            t = substr($0, RSTART+5, RLENGTH-5) + 0
            sum[m] += t
            if (mn[m] == 0 || t < mn[m]) mn[m] = t
            if (t > mx[m]) mx[m] = t
        }
    }
    next
}
# Las lineas sin respuesta ("no answer yet", "Destination Host Unreachable")
# tambien cuentan como enviados: si no, un corte total se veria como 0% de
# perdida sobre cero muestras, que es justo lo contrario de la verdad.
/no answer yet|Unreachable|100% packet loss/ {
    if (minuto_actual != "") tot[minuto_actual]++
}
'
