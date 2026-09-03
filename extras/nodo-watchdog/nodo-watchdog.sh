#!/usr/bin/env bash
#
# Vigila que el nodo este VIVO, no solo que sus procesos existan.
#
# Existe por un atasco real: el 2 de septiembre de 2026 a las 18:26:57 la Pi
# dejo de responder y estuvo 65 minutos muerta hasta que alguien subio a la
# torre a reiniciarla. El LED de actividad del fob USB del nodo 1001 no
# parpadeaba, y ni AllScan, ni Allmon3, ni SSH respondian.
#
# Lo que hace este caso dificil es que NADA de lo que ya estaba puesto lo
# detecta:
#
#   - El watchdog por hardware (BCM2835, 60 s) estaba activo y systemd lo
#     alimentaba cada minuto... y siguio alimentandolo los 65 minutos. systemd
#     seguia sano; solo estaban atascados Asterisk, PHP y sshd. Ese watchdog
#     solo salva de un cuelgue total del kernel.
#   - systemctl decia que asterisk.service estaba "active". Y lo estaba: el
#     proceso existia. Simplemente no respondia.
#   - El kernel contestaba pings y Apache alcanzaba a devolver un 301, asi que
#     desde afuera la maquina parecia viva.
#
# Por eso aqui no se pregunta "esta corriendo el proceso" sino "me contesta".
#
set -uo pipefail

ASTERISK="${ASTERISK:-/usr/sbin/asterisk}"
ESPERA="${ESPERA:-10}"          # segundos que se le dan a Asterisk para contestar
ESTADO="${ESTADO:-/var/lib/nodo-watchdog.fallos}"
ULTIMO_REBOOT="${ULTIMO_REBOOT:-/var/lib/nodo-watchdog.reboot}"

# Escalada. Con el temporizador cada minuto, esto son ~3 min hasta el reinicio
# del servicio y ~8 min hasta el reinicio de la Pi.
UMBRAL_ASTERISK="${UMBRAL_ASTERISK:-3}"
UMBRAL_REBOOT="${UMBRAL_REBOOT:-8}"
# 0 desactiva el reinicio de la Pi. Viene ACTIVO porque la evidencia dice que
# fue lo unico que recupero el nodo: si los hilos de Asterisk quedan en espera
# ininterrumpible por un USB colgado, ni SIGKILL los mata.
REBOOT="${REBOOT:-1}"
# Guarda de bucle: si ya se reinicio por esta causa hace poco, reiniciar otra
# vez no va a arreglarlo y deja al repetidor entrando y saliendo del aire cada
# ocho minutos. Se avisa y se deja quieto para que un humano lo mire.
ESPERA_ENTRE_REBOOTS="${ESPERA_ENTRE_REBOOTS:-3600}"

log() { logger -t nodo-watchdog -- "$*"; }

# La prueba de vida. `asterisk -rx` se cuelga si el CLI esta atascado, por eso
# va envuelto en timeout: sin eso, el vigilante se atascaria junto con el nodo.
nodo_responde() {
    timeout "$ESPERA" "$ASTERISK" -rx "core show uptime" 2>/dev/null | grep -qi "uptime\|reload"
}

fallos=$(cat "$ESTADO" 2>/dev/null || echo 0)
[[ "$fallos" =~ ^[0-9]+$ ]] || fallos=0

if nodo_responde; then
    if [ "$fallos" -gt 0 ]; then
        log "el nodo volvio a responder tras $fallos revision(es)"
        rm -f "$ESTADO"
    fi
    exit 0
fi

fallos=$(( fallos + 1 ))
mkdir -p "$(dirname "$ESTADO")" && echo "$fallos" > "$ESTADO"

# Contexto util para despues. Se registra ANTES de intentar nada, porque el
# reinicio se lleva la evidencia por delante.
usb=$(lsusb 2>/dev/null | grep -ci "c-media")
carga=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)
bloqueados=$(ps -eo stat,comm 2>/dev/null | awk '$1 ~ /^D/ {print $2}' | sort -u | tr '\n' ' ')
log "Asterisk NO responde [revision $fallos] · fobs USB visibles: $usb · carga: $carga · procesos en espera de E/S: ${bloqueados:-ninguno}"

if [ "$fallos" -eq "$UMBRAL_ASTERISK" ]; then
    log "reiniciando asterisk.service"
    # Con timeout: si los hilos estan en espera ininterrumpible, systemd se
    # queda esperando el TimeoutStopSec y el vigilante no debe quedarse con el.
    timeout 60 systemctl restart asterisk
    sleep 8
    if nodo_responde; then
        log "el nodo respondio tras reiniciar asterisk"
        rm -f "$ESTADO"
        exit 0
    fi
    log "sigue sin responder tras reiniciar asterisk"
fi

if [ "$REBOOT" = "1" ] && [ "$fallos" -ge "$UMBRAL_REBOOT" ]; then
    ahora=$(date +%s)
    previo=$(cat "$ULTIMO_REBOOT" 2>/dev/null || echo 0)
    [[ "$previo" =~ ^[0-9]+$ ]] || previo=0
    transcurrido=$(( ahora - previo ))

    if [ "$previo" -gt 0 ] && [ "$transcurrido" -lt "$ESPERA_ENTRE_REBOOTS" ]; then
        log "NO se reinicia: ya se habia reiniciado hace $(( transcurrido / 60 )) min y no sirvio. "             "El nodo necesita intervencion humana; revisa el fob USB y el cableado."
        exit 1
    fi

    log "ULTIMO RECURSO: reiniciando la Raspberry tras $fallos revisiones sin respuesta"
    echo "$ahora" > "$ULTIMO_REBOOT"
    rm -f "$ESTADO"
    sync
    systemctl reboot
fi
