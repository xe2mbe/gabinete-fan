#!/usr/bin/env bash
# Instala el registro de calidad del enlace. Idempotente.
#   sudo ./install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f /usr/local/sbin/enlace-log.sh   # version vieja en awk
install -m 755 "$SRC/enlace-log.py" /usr/local/sbin/enlace-log.py
install -m 644 "$SRC/enlace-log.service" /etc/systemd/system/
install -d -o root -g asl -m 775 /var/lib/gabinete-fan/telemetria
systemctl daemon-reload
systemctl enable --now enlace-log.service
sleep 65
cat <<FIN

Instalado. Un renglon por minuto en:
  /var/lib/gabinete-fan/telemetria/enlace-AAAAMMDD.csv

Primeras filas:
FIN
tail -3 /var/lib/gabinete-fan/telemetria/enlace-$(date +%Y%m%d).csv 2>/dev/null || echo "  (aun sin datos, espera un minuto)"
