#!/usr/bin/env bash
# Instala el vigilante de vida del nodo. Idempotente.
#   sudo ./install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 755 "$SRC/nodo-watchdog.sh" /usr/local/sbin/nodo-watchdog.sh
install -m 644 "$SRC/nodo-watchdog.service" /etc/systemd/system/
install -m 644 "$SRC/nodo-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nodo-watchdog.timer

cat <<FIN

Instalado. Revisa cada minuto que Asterisk responda de verdad.

Escalada:
   3 revisiones sin respuesta  ->  reinicia asterisk.service
   8 revisiones sin respuesta  ->  REINICIA LA RASPBERRY

Para desactivar el reinicio de la Pi:
   sudo systemctl edit nodo-watchdog.service
   [Service]
   Environment=REBOOT=0

Ver que ha hecho:   journalctl -t nodo-watchdog --since today
Probarlo a mano:    sudo /usr/local/sbin/nodo-watchdog.sh
FIN
