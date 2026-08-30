#!/usr/bin/env bash
#
# Instalador de gabinete-fan para la Raspberry Pi del servidor ASL.
# Es idempotente: se puede volver a correr para actualizar el codigo.
#
#   sudo ./install.sh
#
set -euo pipefail

PREFIX=/opt/gabinete-fan
CONFDIR=/etc/gabinete-fan
STATEDIR=/var/lib/gabinete-fan
BOOTCFG=/boot/firmware/config.txt
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
[[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
[[ -f $BOOTCFG ]] || { echo "No encuentro config.txt." >&2; exit 1; }

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    aviso: %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
say "Dependencias"
apt-get update -qq
apt-get install -y -qq python3-yaml python3-paho-mqtt python3-gpiozero python3-lgpio

# ---------------------------------------------------------------------------
say "Codigo y configuracion"
install -d "$PREFIX" "$CONFDIR" "$STATEDIR"
rm -rf "$PREFIX/src"
cp -r "$SRC/src" "$PREFIX/src"
cp -r "$SRC/docs" "$PREFIX/docs" 2>/dev/null || true

if [[ -f "$CONFDIR/config.yaml" ]]; then
    cp "$SRC/config/config.yaml" "$CONFDIR/config.yaml.nuevo"
    warn "ya existe $CONFDIR/config.yaml; la version nueva quedo como config.yaml.nuevo"
else
    cp "$SRC/config/config.yaml" "$CONFDIR/config.yaml"
    chmod 640 "$CONFDIR/config.yaml"   # contiene la contrasena del broker MQTT
    echo "    escrito $CONFDIR/config.yaml  <-- edita broker, credenciales e IDs de sensores"
fi

# ---------------------------------------------------------------------------
say "Overlays de arranque"
REBOOT=0

add_overlay() {
    local line="$1" why="$2"
    if grep -qxF "$line" "$BOOTCFG"; then
        echo "    ya presente: $line"
    else
        [[ -f "$BOOTCFG.gabinete-bak" ]] || cp "$BOOTCFG" "$BOOTCFG.gabinete-bak"
        printf '\n# gabinete-fan: %s\n%s\n' "$why" "$line" >> "$BOOTCFG"
        echo "    agregado: $line"
        REBOOT=1
    fi
}

add_overlay "dtoverlay=w1-gpio,gpiopin=4" "bus 1-Wire para los DS18B20"
add_overlay "dtoverlay=pwm,pin=18,func=2" "PWM0 por hardware a 25 kHz para el ventilador"

# En la Pi 3B+ el audio analogico interno ocupa PWM0 y PWM1: mientras
# dtparam=audio=on siga activo, GPIO18 no puede dar PWM por hardware.
# ASL usa fobs USB (SimpleUSB), asi que apagarlo no afecta la operacion.
if grep -qE '^\s*dtparam=audio=on' "$BOOTCFG"; then
    [[ -f "$BOOTCFG.gabinete-bak" ]] || cp "$BOOTCFG" "$BOOTCFG.gabinete-bak"
    sed -i 's/^\(\s*\)dtparam=audio=on/\1dtparam=audio=off   # gabinete-fan: libera PWM0\/PWM1 para el ventilador/' "$BOOTCFG"
    echo "    dtparam=audio=on -> off (libera PWM0/PWM1)"
    warn "se desactivo el audio analogico interno (jack de 3.5 mm)."
    warn "ASL usa SimpleUSB por USB, asi que no afecta a los nodos."
    REBOOT=1
fi

# ---------------------------------------------------------------------------
say "Servicio systemd"
cp "$SRC/systemd/gabinete-fan.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gabinete-fan.service >/dev/null
echo "    habilitado (aun no iniciado)"

# ---------------------------------------------------------------------------
say "Listo"
cat <<FIN

Siguientes pasos:

  1. Edita $CONFDIR/config.yaml
       - mqtt.host / username / password  del broker de Home Assistant
       - sensors.vhf.id y sensors.tx10m.id
       - control.t_on / t_critical / t_off / purge_minutes

FIN

if [[ $REBOOT -eq 1 ]]; then
cat <<FIN
  2. REINICIA la Raspberry Pi para cargar los overlays (1-Wire y PWM).
     Es un servidor de repetidor: hazlo en una ventana de baja actividad.

         sudo reboot

  3. Tras el reinicio, identifica los sensores y pon los IDs en config.yaml:

         sudo python3 -m gabinete_fan --discover

FIN
else
cat <<FIN
  2. Identifica los sensores y pon los IDs en config.yaml:

         sudo PYTHONPATH=$PREFIX/src python3 -m gabinete_fan --discover

FIN
fi

cat <<FIN
  4. Prueba en seco (no toca ningun GPIO):

         sudo PYTHONPATH=$PREFIX/src python3 -m gabinete_fan --dry-run --no-mqtt

  5. Arranca el servicio y observa:

         sudo systemctl start gabinete-fan
         journalctl -u gabinete-fan -f

FIN
