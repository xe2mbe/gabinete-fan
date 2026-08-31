#!/usr/bin/env bash
#
# Convierte frases.conf en archivos de audio listos para app_rpt.
#
# Se corre AL INSTALAR, no en el momento de la falla. Esa es la idea central:
# cuando el enlace se cae, lo unico que hay que hacer es reproducir un archivo
# que ya existe. Nada de sintetizar voz con el sistema en problemas.
#
set -euo pipefail

FRASES="${FRASES:-/etc/radio-alerta/frases.conf}"
DESTINO="${DESTINO:-/usr/share/asterisk/sounds/custom}"
VOZ="${VOZ:-es-419}"          # espanol latinoamericano
VELOCIDAD="${VELOCIDAD:-140}" # palabras por minuto; despacio se entiende mejor en RF

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
command -v espeak-ng >/dev/null || { echo "Falta espeak-ng: sudo apt install espeak-ng" >&2; exit 1; }
command -v sox >/dev/null || { echo "Falta sox: sudo apt install sox" >&2; exit 1; }
[[ -f $FRASES ]] || { echo "No existe $FRASES" >&2; exit 1; }

mkdir -p "$DESTINO"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

n=0
while IFS='|' read -r clave texto; do
    [[ -z "${clave// }" || "${clave:0:1}" == "#" ]] && continue
    clave="${clave// }"
    espeak-ng -v "$VOZ" -s "$VELOCIDAD" -w "$tmp/x.wav" "$texto"
    # -3 dB de margen: sin esto sox recorta picos y la voz sale con chasquidos.
    # 8 kHz mono u-law es lo que reproduce Asterisk sin reconvertir nada.
    sox -v 0.7 "$tmp/x.wav" -r 8000 -c 1 -t ul "$DESTINO/alerta-$clave.ulaw"
    chmod 644 "$DESTINO/alerta-$clave.ulaw"
    dur=$(soxi -D "$tmp/x.wav" 2>/dev/null || echo "?")
    printf '  %-14s %5.1f s   %s\n' "$clave" "$dur" "$DESTINO/alerta-$clave.ulaw"
    n=$((n+1))
done < "$FRASES"

echo
echo "$n audios generados."
echo "Probar uno SIN transmitir:  play $DESTINO/alerta-sin_internet.ulaw"
echo "Probar AL AIRE:             sudo /usr/local/sbin/radio-alerta.sh sin_internet"
