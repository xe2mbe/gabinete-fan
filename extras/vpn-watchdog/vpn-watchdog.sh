#!/usr/bin/env bash
#
# Vigila el enlace del sitio y, si se cae, lo repara y lo anuncia por radio.
#
# Existe por como falla un sitio con enlace 4G. La Pi se conecta por Ethernet a
# un router 4G: cuando el operador pierde el servicio, el CABLE SIGUE ARRIBA. La
# Pi conserva su IP, su ruta por omision y su interfaz, y desde adentro todo se
# ve sano mientras esta completamente aislada. Nada en el sistema se entera.
#
# Y cuando el servicio vuelve, nadie levanta el tunel: wg-quick@wg0 es un
# servicio `oneshot`, systemd lo da por terminado al arrancar y no lo vuelve a
# tocar nunca. Ni siquiera Restart=on-failure ayuda, porque no hubo falla.
# PersistentKeepalive tampoco basta: tras un corte largo WireGuard deja de
# reintentar el handshake y se queda esperando trafico nuevo.
#
# Cuando no hay internet no hay nada que reparar desde aqui, pero SI hay algo
# que hacer: avisar. El radio es el unico canal que sigue vivo, y por eso el
# anuncio es 100% local (audio pregenerado, sin red y sin nube).
#
set -uo pipefail

IFACE="${IFACE:-wg0}"
PEER_IP="${PEER_IP:-10.10.0.1}"            # el otro extremo DENTRO del tunel
# Varios destinos: si el operador bloquea ICMP a uno, el otro responde.
PRUEBA_INTERNET="${PRUEBA_INTERNET:-1.1.1.1 8.8.8.8}"
ESTADO="${ESTADO:-/var/lib/vpn-watchdog.estado}"

# -- politica de anuncios por radio ----------------------------------------
# Esto sale al aire por un repetidor que otros estan usando, asi que se cuida.
ANUNCIAR="${ANUNCIAR:-1}"
ANUNCIADOR="${ANUNCIADOR:-/usr/local/sbin/radio-alerta.sh}"
ANUNCIAR_TRAS="${ANUNCIAR_TRAS:-3}"    # revisiones seguidas antes del 1er aviso
REPETIR_CADA="${REPETIR_CADA:-15}"     # revisiones entre repeticiones
MAX_ANUNCIOS="${MAX_ANUNCIOS:-12}"     # tope: no acaparar el repetidor sin fin

# Reinicio de la Pi como ultimo recurso. APAGADO: en un nodo de repetidor un
# reinicio lo saca del aire, y esa decision es del operador, no de un script.
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

anunciar() {
    [ "$ANUNCIAR" = "1" ] || return 0
    [ -x "$ANUNCIADOR" ] || { log "no encuentro $ANUNCIADOR; no se anuncia por radio"; return 0; }
    "$ANUNCIADOR" "$1" || true
}

# -- estado previo: clase fallos ultimo_anuncio anuncios --------------------
clase_ant=""; fallos=0; ultimo_anuncio=0; anuncios=0
if [ -r "$ESTADO" ]; then
    read -r clase_ant fallos ultimo_anuncio anuncios < "$ESTADO" 2>/dev/null || true
fi
[[ "$fallos" =~ ^[0-9]+$ ]] || fallos=0
[[ "$ultimo_anuncio" =~ ^[0-9]+$ ]] || ultimo_anuncio=0
[[ "$anuncios" =~ ^[0-9]+$ ]] || anuncios=0

guardar() { mkdir -p "$(dirname "$ESTADO")" && echo "$1 $2 $3 $4" > "$ESTADO"; }

# -- diagnostico ------------------------------------------------------------
if ! hay_internet; then
    clase="sin_internet"
elif ! tunel_vivo; then
    clase="tunel_caido"
else
    clase="ok"
fi

# -- todo bien --------------------------------------------------------------
if [ "$clase" = "ok" ]; then
    if [ "$fallos" -gt 0 ]; then
        log "enlace de vuelta tras $fallos revision(es) en falla ($clase_ant)"
        # Solo se anuncia la recuperacion si la falla llego a anunciarse; si
        # nadie la oyo, avisar que ya se arreglo es ruido.
        [ "$anuncios" -gt 0 ] && anunciar restablecido
        rm -f "$ESTADO"
    fi
    exit 0
fi

# -- hay falla: se cuenta ---------------------------------------------------
# Si cambio la clase de falla (p.ej. volvio el internet pero no el tunel) se
# reinicia el conteo: es otra falla distinta y merece su propio aviso.
if [ "$clase" != "$clase_ant" ]; then
    fallos=1; ultimo_anuncio=0; anuncios=0
else
    fallos=$(( fallos + 1 ))
fi

# -- reparar: solo tiene sentido si hay internet -----------------------------
if [ "$clase" = "tunel_caido" ]; then
    ahora=$(date +%s)
    ultimo=$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)
    if [ "${ultimo:-0}" -gt 0 ] 2>/dev/null; then edad="hace $(( ahora - ultimo )) s"; else edad="nunca"; fi

    log "hay internet pero $PEER_IP no responde por $IFACE (ultimo handshake: $edad); reiniciando wg-quick@$IFACE [revision $fallos]"
    systemctl restart "wg-quick@$IFACE"
    sleep 10
    if tunel_vivo; then
        log "tunel restablecido"
        [ "$anuncios" -gt 0 ] && anunciar restablecido
        rm -f "$ESTADO"
        exit 0
    fi
    log "el tunel SIGUE sin responder tras reiniciar [revision $fallos]"
else
    log "sin enlace a internet [revision $fallos]; no hay nada que reparar desde aqui"
fi

# -- anunciar por radio ------------------------------------------------------
# Se espera a confirmar la falla antes de salir al aire: un parpadeo de treinta
# segundos no merece ocupar el repetidor.
if [ "$fallos" -ge "$ANUNCIAR_TRAS" ] && [ "$anuncios" -lt "$MAX_ANUNCIOS" ] &&
   { [ "$ultimo_anuncio" -eq 0 ] || [ $(( fallos - ultimo_anuncio )) -ge "$REPETIR_CADA" ]; }; then
    anuncios=$(( anuncios + 1 ))
    ultimo_anuncio="$fallos"
    log "anunciando '$clase' por radio [aviso $anuncios de $MAX_ANUNCIOS]"
    anunciar "$clase"
fi

guardar "$clase" "$fallos" "$ultimo_anuncio" "$anuncios"

# -- ultimo recurso ----------------------------------------------------------
if [ "$REINICIAR_SI_FALLA" = "1" ] && [ "$clase" = "tunel_caido" ] && [ "$fallos" -ge "$MAX_INTENTOS" ]; then
    log "ultimo recurso: reiniciando la Raspberry tras $fallos revisiones en falla"
    rm -f "$ESTADO"
    systemctl reboot
fi
