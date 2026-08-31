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

Listo. El vigilante del tunel los usara solo, pero pruebalo antes:

  sudo /usr/local/sbin/radio-alerta.sh sin_internet

Eso transmite: el anuncio sale por app_rpt (rpt localplay) hacia el nodo, es
decir por el FOB USB del radio. No intentes oirlo con `play` en la Pi: eso
usaria la tarjeta de sonido, que ademas esta apagada porque el instalador del
ventilador libero PWM0/PWM1 desactivando el audio analogico.

Si cambias el numero de nodo, edita NODO en /usr/local/sbin/radio-alerta.sh
Si cambias los textos, edita /etc/radio-alerta/frases.conf y vuelve a correr
/usr/local/sbin/radio-alerta-generar.sh
FIN
