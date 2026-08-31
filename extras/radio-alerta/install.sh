#!/usr/bin/env bash
#
# Instala los anuncios de falla por radio. Idempotente.
#
#   sudo ./install.sh
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Dependencias"
if ! command -v espeak-ng >/dev/null || ! command -v sox >/dev/null; then
    apt-get update -qq && apt-get install -y -qq espeak-ng sox
fi
echo "    espeak-ng y sox listos"

echo "==> Scripts"
install -m 755 "$SRC/radio-alerta.sh" /usr/local/sbin/radio-alerta.sh
install -m 755 "$SRC/radio-alerta-generar.sh" /usr/local/sbin/radio-alerta-generar.sh

echo "==> Frases"
install -d /etc/radio-alerta
if [[ -f /etc/radio-alerta/frases.conf ]]; then
    cp "$SRC/frases.conf" /etc/radio-alerta/frases.conf.nuevo
    echo "    ya existe frases.conf; la version nueva quedo como frases.conf.nuevo"
else
    install -m 644 "$SRC/frases.conf" /etc/radio-alerta/frases.conf
    echo "    escrito /etc/radio-alerta/frases.conf"
fi

echo "==> Generando audios"
/usr/local/sbin/radio-alerta-generar.sh

cat <<FIN

Listo. El vigilante del tunel los usara solo, pero pruebalos antes:

  1. Escuchar en la Pi, SIN transmitir:
       play /usr/share/asterisk/sounds/custom/alerta-sin_internet.ulaw

  2. Sacarlo AL AIRE por el nodo (esto SI transmite):
       sudo /usr/local/sbin/radio-alerta.sh sin_internet

Si cambias el numero de nodo, edita NODO en /usr/local/sbin/radio-alerta.sh
Si cambias los textos, edita /etc/radio-alerta/frases.conf y vuelve a correr
/usr/local/sbin/radio-alerta-generar.sh
FIN
