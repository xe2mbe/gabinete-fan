#!/usr/bin/env bash
#
# Anuncia una falla por radio, en el nodo local.
#
#     radio-alerta.sh sin_internet|tunel_caido|restablecido
#
# Se usa cuando el sitio perdio el enlace: el radio es el unico canal que
# queda. Por eso todo aqui es local -audio pregenerado, sin red, sin nube- y
# por eso NO reutiliza el TTS con IA del proyecto AllVox: ese depende de
# internet, que es justo lo que falta cuando esto hace falta.
#
set -uo pipefail

NODO="${NODO:-1001}"
DESTINO="${DESTINO:-/usr/share/asterisk/sounds/custom}"
ASTERISK="${ASTERISK:-/usr/sbin/asterisk}"

clave="${1:-}"
[[ -n "$clave" ]] || { echo "uso: $(basename "$0") <clave>" >&2; exit 2; }

log() { logger -t radio-alerta -- "$*"; }
audio="$DESTINO/alerta-$clave"

if [[ ! -f "$audio.ulaw" ]]; then
    log "no existe $audio.ulaw; corre radio-alerta-generar.sh"
    exit 1
fi

# localplay y no playback: solo sale por el transmisor de este nodo, no hacia
# los nodos enlazados. Durante un corte los enlaces estan muertos de todos
# modos, y al volver no tiene caso anunciarle la falla a media red.
if "$ASTERISK" -rx "rpt localplay $NODO $audio" >/dev/null 2>&1; then
    log "anunciado por el nodo $NODO: $clave"
else
    log "FALLO al anunciar '$clave' por el nodo $NODO (¿asterisk esta corriendo?)"
    exit 1
fi
