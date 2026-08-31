#!/usr/bin/env bash
#
# Instala los anuncios de falla por radio. Idempotente.
#
#   sudo ./install.sh              con voz neuronal (descarga 61 MB)
#   sudo SIN_PIPER=1 ./install.sh  solo espeak, sin descargar nada
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Corre este script con sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VOCES=/usr/share/radio-alerta/voces
MODELO=es_MX-claude-high
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high/$MODELO"
SIN_PIPER="${SIN_PIPER:-0}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    aviso: %s\033[0m\n' "$*"; }

say "Dependencias"
faltan=()
command -v sox >/dev/null || faltan+=(sox)
command -v espeak-ng >/dev/null || faltan+=(espeak-ng)
if [[ ${#faltan[@]} -gt 0 ]]; then
    apt-get update -qq && apt-get install -y -qq "${faltan[@]}"
fi
echo "    sox y espeak-ng listos"

# ---------------------------------------------------------------------------
# La voz. Se descarga AHORA, que es cuando hay internet: en la falla ya no lo
# habria, y por eso el audio se pregenera en vez de sintetizarse al vuelo.
say "Voz neuronal (Piper)"
if [[ $SIN_PIPER == 1 ]]; then
    warn "SIN_PIPER=1: se usara espeak-ng, que suena bastante peor"
elif ! command -v piper >/dev/null; then
    warn "no esta instalado piper; se usara espeak-ng"
    warn "en ASL3 suele venir con:  sudo apt install piper-tts"
elif [[ -f "$VOCES/$MODELO.onnx" && -f "$VOCES/$MODELO.onnx.json" ]]; then
    echo "    ya esta: $VOCES/$MODELO.onnx ($(du -h "$VOCES/$MODELO.onnx" | cut -f1))"
else
    install -d "$VOCES"
    echo "    descargando $MODELO (~61 MB)..."
    if curl -fSL --progress-bar -o "$VOCES/$MODELO.onnx.tmp" "$BASE.onnx" &&
       curl -fsSL -o "$VOCES/$MODELO.onnx.json.tmp" "$BASE.onnx.json"; then
        mv "$VOCES/$MODELO.onnx.tmp" "$VOCES/$MODELO.onnx"
        mv "$VOCES/$MODELO.onnx.json.tmp" "$VOCES/$MODELO.onnx.json"
        chmod 644 "$VOCES/$MODELO.onnx" "$VOCES/$MODELO.onnx.json"
        echo "    listo: $(du -h "$VOCES/$MODELO.onnx" | cut -f1)"
    else
        rm -f "$VOCES/$MODELO".onnx.tmp "$VOCES/$MODELO".onnx.json.tmp
        warn "fallo la descarga; se usara espeak-ng. Reintenta luego con:"
        warn "  sudo $0"
    fi
fi

# ---------------------------------------------------------------------------
say "Scripts"
install -m 755 "$SRC/radio-alerta.sh" /usr/local/sbin/radio-alerta.sh
install -m 755 "$SRC/radio-alerta-generar.sh" /usr/local/sbin/radio-alerta-generar.sh

say "Frases"
install -d /etc/radio-alerta
if [[ -f /etc/radio-alerta/frases.conf ]]; then
    cp "$SRC/frases.conf" /etc/radio-alerta/frases.conf.nuevo
    warn "ya existe frases.conf; la version nueva quedo como frases.conf.nuevo"
else
    install -m 644 "$SRC/frases.conf" /etc/radio-alerta/frases.conf
    echo "    escrito /etc/radio-alerta/frases.conf"
fi

say "Generando audios"
echo "    con Piper esto tarda ~10 s por frase en una Pi 3B+. Es normal:"
echo "    corre una sola vez, no en la falla."
echo
/usr/local/sbin/radio-alerta-generar.sh

cat <<FIN

Pruebalo al aire, por el FOB del nodo:

  sudo /usr/local/sbin/radio-alerta.sh sin_internet

Si cambias el numero de nodo, edita NODO en /usr/local/sbin/radio-alerta.sh
Si cambias los textos, edita /etc/radio-alerta/frases.conf y vuelve a correr
/usr/local/sbin/radio-alerta-generar.sh
FIN
