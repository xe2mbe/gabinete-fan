#!/usr/bin/env bash
#
# Vigila el tunel WireGuard y lo levanta si se quedo mudo.
#
# Existe por como falla un sitio con enlace 4G. La Pi se conecta por Ethernet a
# un router 4G: cuando el operador pierde el servicio, el CABLE SIGUE ARRIBA. La
# Pi conserva su IP, su ruta por omision y su interfaz, y desde adentro todo se
# ve sano mientras esta completamente aislada. Nada en el sistema se entera.
#
# Y cuando el servicio vuelve, nadie levanta el tunel: wg-quick@wg0 es un
# servicio `oneshot`, systemd lo da por terminado al arrancar y no lo vuelve a
# tocar nunca. Ni siquiera Restart=on-failure ayuda, porque no hubo falla.
#
# PersistentKeepalive no basta: tras un corte largo WireGuard deja de reintentar
# el handshake y se queda esperando trafico nuevo.
#
# Se instala con el install.sh de esta misma carpeta.
#
set -uo pipefail

IFACE="${IFACE:-wg0}"
PEER_IP="${PEER_IP:-10.10.0.1}"           # el otro extremo DENTRO del tunel
# Varios destinos: si el operador bloquea ICMP a uno, el otro responde. Basta
# con que cualquiera conteste para dar el internet por bueno.
PRUEBA_INTERNET="${PRUEBA_INTERNET:-1.1.1.1 8.8.8.8}"
ESTADO="${ESTADO:-/var/lib/vpn-watchdog.fallos}"

# Reinicio de la Pi como ultimo recurso, tras este numero de reparaciones
# fallidas seguidas. Viene APAGADO: en un nodo de repetidor un reinicio saca
# el nodo del aire, y esa decision es del operador, no de un script.
MAX_INTENTOS="${MAX_INTENTOS:-10}"
REINICIAR_SI_FALLA="${REINICIAR_SI_FALLA:-0}"

log() { logger -t vpn-watchdog -- "$*"; }

hay_internet() {
    local h
    for h in $PRUEBA_INTERNET; do
        ping -c1 -W5 "$h" >/dev/null 2>&1 && return 0
    done
    return 1
}

tunel_vivo() { ping -c2 -W5 -I "$IFACE" "$PEER_IP" >/dev/null 2>&1; }

fallos=$(cat "$ESTADO" 2>/dev/null || echo 0)
[[ "$fallos" =~ ^[0-9]+$ ]] || fallos=0

# 1. Sin internet no hay nada que arreglar aqui. Reiniciar el tunel sin enlace
#    solo genera ruido; se espera al siguiente disparo del temporizador.
if ! hay_internet; then
    exit 0
fi

# 2. Si el otro extremo contesta por el tunel, esta sano. Se comprueba con
#    trafico real y no con la edad del handshake: WireGuard no lo renueva si no
#    hay nada que mandar, asi que un handshake viejo en un tunel ocioso es
#    normal y reiniciar por eso seria un error.
if tunel_vivo; then
    if [ "$fallos" -gt 0 ]; then
        log "tunel de vuelta tras $fallos intento(s)"
        rm -f "$ESTADO"
    fi
    exit 0
fi

# 3. Hay internet pero el tunel esta mudo: se repara.
ahora=$(date +%s)
ultimo=$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)
if [ "${ultimo:-0}" -gt 0 ] 2>/dev/null; then
    edad="hace $(( ahora - ultimo )) s"
else
    edad="nunca"
fi

fallos=$(( fallos + 1 ))
mkdir -p "$(dirname "$ESTADO")" && echo "$fallos" > "$ESTADO"

log "hay internet pero $PEER_IP no responde por $IFACE (ultimo handshake: $edad); reiniciando wg-quick@$IFACE [intento $fallos]"
systemctl restart "wg-quick@$IFACE"

sleep 10
if tunel_vivo; then
    log "tunel restablecido"
    rm -f "$ESTADO"
    exit 0
fi

log "el tunel SIGUE sin responder tras reiniciar [intento $fallos de $MAX_INTENTOS]"

if [ "$REINICIAR_SI_FALLA" = "1" ] && [ "$fallos" -ge "$MAX_INTENTOS" ]; then
    log "ultimo recurso: reiniciando la Raspberry tras $fallos intentos fallidos"
    rm -f "$ESTADO"
    systemctl reboot
fi
