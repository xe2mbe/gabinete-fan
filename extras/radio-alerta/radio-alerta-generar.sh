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
# Tono de atencion al principio y al final. TONO=0 lo desactiva.
TONO="${TONO:-1}"
TONO_ALTO="${TONO_ALTO:-1200}"
TONO_BAJO="${TONO_BAJO:-900}"
TONO_VOL="${TONO_VOL:-0.6}"

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

# ---------------------------------------------------------------------------
# Tonos de atencion. El `fade` de los bordes no es adorno: un seno que arranca
# y corta en seco produce un chasquido que en RF se oye peor que el tono.
if [[ $TONO == 1 ]]; then
    sox -n -r 8000 -c 1 -b 16 "$tmp/alto.wav"         synth 0.20 sine "$TONO_ALTO" vol "$TONO_VOL" fade q 0.008 0.20 0.02
    sox -n -r 8000 -c 1 -b 16 "$tmp/bajo.wav"         synth 0.20 sine "$TONO_BAJO" vol "$TONO_VOL" fade q 0.008 0.20 0.02
    sox -n -r 8000 -c 1 -b 16 "$tmp/hueco.wav" trim 0.0 0.05
    sox -n -r 8000 -c 1 -b 16 "$tmp/pausa.wav" trim 0.0 0.25
    # Entrada: dos pares alternados, que es lo que hace voltear a ver el radio.
    sox "$tmp/alto.wav" "$tmp/hueco.wav" "$tmp/bajo.wav" "$tmp/hueco.wav"         "$tmp/alto.wav" "$tmp/hueco.wav" "$tmp/bajo.wav" "$tmp/ini.wav"
    # Salida: un par descendente, mas corto, que dice "ya termine".
    sox "$tmp/bajo.wav" "$tmp/hueco.wav" "$tmp/alto.wav" "$tmp/fin.wav"
    echo "Tono: $TONO_ALTO/$TONO_BAJO Hz al inicio y al final"
    echo
fi

n=0
while IFS='|' read -r clave texto; do
    [[ -z "${clave// }" || "${clave:0:1}" == "#" ]] && continue
    clave="${clave// }"

    if [[ $MOTOR == piper ]]; then
        # piper anuncia en stdout el archivo que escribio; estorba en el reporte
        printf '%s\n' "$texto" | piper -m "$VOZ_PIPER" -f "$tmp/x.wav" >/dev/null 2>&1
    else
        espeak-ng -v "$VOZ_ESPEAK" -s "$VELOCIDAD" -w "$tmp/x.wav" "$texto"
    fi

    # La voz se lleva a 8 kHz ANTES de pegarle los tonos: sox solo concatena
    # archivos que coinciden en frecuencia, canales y profundidad.
    sox -v "$GANANCIA" "$tmp/x.wav" -r 8000 -c 1 -b 16 "$tmp/voz.wav"
    if [[ $TONO == 1 ]]; then
        sox "$tmp/ini.wav" "$tmp/pausa.wav" "$tmp/voz.wav"             "$tmp/pausa.wav" "$tmp/fin.wav" "$tmp/final.wav"
    else
        cp "$tmp/voz.wav" "$tmp/final.wav"
    fi
    # 8 kHz mono u-law es lo que reproduce Asterisk sin reconvertir nada.
    sox "$tmp/final.wav" -t ul "$DESTINO/alerta-$clave.ulaw"
    chmod 644 "$DESTINO/alerta-$clave.ulaw"
    dur=$(soxi -D "$tmp/final.wav" 2>/dev/null || echo 0)
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
