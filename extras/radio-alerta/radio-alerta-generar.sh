#!/usr/bin/env bash
#
# Convierte frases.conf en archivos de audio listos para app_rpt.
#
# Se corre AL INSTALAR, no en el momento de la falla. Esa es la idea central, y
# tiene una consecuencia que vale la pena aprovechar: como la generacion ocurre
# con internet y sin prisa, puede permitirse ser lenta y pesada. Por eso el
# motor preferido es Piper -red neuronal, 61 MB de modelo, ~11 s por frase en
# una Pi 3B+- y no espeak, que es instantaneo pero suena a robot.
#
# En la falla nada de eso corre: solo se abre un archivo que ya existe.
#
set -euo pipefail

FRASES="${FRASES:-/etc/radio-alerta/frases.conf}"
DESTINO="${DESTINO:-/usr/share/asterisk/sounds/custom}"
VOZ_PIPER="${VOZ_PIPER:-/usr/share/radio-alerta/voces/es_MX-claude-high.onnx}"
# Respaldo, por si no hay modelo de Piper: peor voz, pero el sistema funciona.
VOZ_ESPEAK="${VOZ_ESPEAK:-es-419}"
VELOCIDAD="${VELOCIDAD:-140}"   # solo aplica a espeak
# -3 dB de margen: sin esto sox recorta picos y la voz sale con chasquidos.
GANANCIA="${GANANCIA:-0.7}"

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
command -v sox >/dev/null || { echo "Falta sox: sudo apt install sox" >&2; exit 1; }
[[ -f $FRASES ]] || { echo "No existe $FRASES" >&2; exit 1; }

if command -v piper >/dev/null && [[ -f $VOZ_PIPER ]]; then
    MOTOR=piper
    echo "Motor: piper  ($(basename "$VOZ_PIPER"))"
elif command -v espeak-ng >/dev/null; then
    MOTOR=espeak
    echo "Motor: espeak-ng  (voz $VOZ_ESPEAK)"
    echo "  Aviso: no encontre el modelo de Piper en $VOZ_PIPER."
    echo "  La voz va a sonar robotica. Para la buena, corre el install.sh."
else
    echo "No hay ni piper con modelo ni espeak-ng. Instala uno de los dos." >&2
    exit 1
fi
echo

mkdir -p "$DESTINO"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

n=0
while IFS='|' read -r clave texto; do
    [[ -z "${clave// }" || "${clave:0:1}" == "#" ]] && continue
    clave="${clave// }"

    if [[ $MOTOR == piper ]]; then
        printf '%s\n' "$texto" | piper -m "$VOZ_PIPER" -f "$tmp/x.wav" 2>/dev/null
    else
        espeak-ng -v "$VOZ_ESPEAK" -s "$VELOCIDAD" -w "$tmp/x.wav" "$texto"
    fi

    # 8 kHz mono u-law es lo que reproduce Asterisk sin reconvertir nada.
    sox -v "$GANANCIA" "$tmp/x.wav" -r 8000 -c 1 -t ul "$DESTINO/alerta-$clave.ulaw"
    chmod 644 "$DESTINO/alerta-$clave.ulaw"
    dur=$(soxi -D "$tmp/x.wav" 2>/dev/null || echo 0)
    printf '  %-14s %5.1f s   %s\n' "$clave" "$dur" "$DESTINO/alerta-$clave.ulaw"
    n=$((n+1))
done < "$FRASES"

echo
echo "$n audios generados con $MOTOR."
echo
echo "Probar al aire:  sudo /usr/local/sbin/radio-alerta.sh sin_internet"
echo
echo "El anuncio sale por app_rpt (rpt localplay) hacia el nodo, o sea por el"
echo "FOB USB del radio. NO por la tarjeta de sonido de la Pi, que ademas esta"
echo "apagada: el instalador del ventilador libero PWM0/PWM1 desactivando el"
echo "audio analogico. Para oirlo sin transmitir, copia el .ulaw a otra maquina."
