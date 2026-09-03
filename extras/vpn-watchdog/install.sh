#!/usr/bin/env bash
#
# Instala el vigilante del tunel WireGuard. Idempotente.
#
#   sudo ./install.sh
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 755 "$SRC/vpn-watchdog.sh" /usr/local/sbin/vpn-watchdog.sh
install -m 644 "$SRC/vpn-watchdog.service" /etc/systemd/system/
install -m 644 "$SRC/vpn-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable vpn-watchdog.timer
# --now no basta al ACTUALIZAR: si el servicio ya corria, seguiria
# ejecutando el archivo viejo desde su descriptor abierto.
systemctl restart vpn-watchdog.timer

echo
echo "Instalado. El vigilante revisa el tunel cada minuto."
echo
echo "  Ver cuando corre:   systemctl list-timers vpn-watchdog.timer"
echo "  Probarlo a mano:    sudo /usr/local/sbin/vpn-watchdog.sh; journalctl -t vpn-watchdog -n 20"
echo "  Ver su historial:   journalctl -t vpn-watchdog --since today"
echo
echo "Si tu tunel no es wg0 o el otro extremo no es 10.10.0.1, edita las"
echo "variables al inicio de /usr/local/sbin/vpn-watchdog.sh"
