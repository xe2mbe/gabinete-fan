# radio-alerta

Anuncia por radio, en el nodo local, que el sitio perdió el enlace.

Cuando la Raspberry se queda sin internet no hay Home Assistant, no hay MQTT y
no hay SSH. **El radio es el único canal que sigue vivo**, y es el que se usa
para avisar qué está pasando.

## La restricción que define todo el diseño

El anuncio tiene que sonar **exactamente cuando no hay internet**. Eso descarta
cualquier síntesis de voz en la nube — incluido el pipeline con ElevenLabs de
[AllVox](https://github.com/xe2mbe/AllVox), que es la opción obvia y que fallaría
justo en el momento en que hace falta.

De ahí las dos decisiones centrales:

- **Voz local**, con `espeak-ng`. Menos bonita, pero no depende de nadie.
- **Audio pregenerado al instalar**, no en el momento de la falla. Cuando el
  enlace se cae, lo único que ocurre es reproducir un archivo que ya existe: sin
  síntesis, sin carga de CPU y sin depender de que `espeak-ng` funcione con el
  sistema en problemas.

```mermaid
flowchart LR
    A["frases.conf"] -->|"al INSTALAR"| B["espeak-ng"]
    B --> C["sox<br/>8 kHz mono u-law"]
    C --> D["alerta-*.ulaw"]
    D -->|"en la FALLA"| E["rpt localplay"]
    E --> F["Transmisor<br/>del nodo"]
```

## Qué anuncia

Tres frases, en `/etc/radio-alerta/frases.conf`:

| Clave | Cuándo |
|---|---|
| `sin_internet` | El enlace 4G está caído. No hay nada que reparar desde la Pi |
| `tunel_caido` | Hay internet pero el túnel no responde. Se está reintentando |
| `restablecido` | El enlace volvió |

El número de nodo se deletrea dígito por dígito a propósito: sobre RF, con ruido
y desvanecimiento, «dos nueve nueve cero ocho cuatro» se entiende mucho mejor
que «doscientos noventa y nueve mil ochenta y cuatro».

## Cómo evita acaparar el repetidor

Esto sale al aire por un repetidor que otras personas están usando. La política
va en el vigilante del túnel y está medida:

- **Espera a confirmar la falla.** Tres revisiones seguidas, o sea unos tres
  minutos, antes del primer aviso. Un parpadeo de treinta segundos no merece
  ocupar el repetidor.
- **Repite cada 15 minutos**, no cada minuto.
- **Se calla tras 12 avisos** —unas tres horas— aunque la falla siga. Si en tres
  horas nadie fue, no va a servir seguir gritando.
- **Solo anuncia la recuperación si llegó a anunciar la falla.** Si nadie la
  oyó, avisar que ya se arregló es ruido.
- Un cambio en la clase de falla reinicia el conteo: es otra falla y merece su
  propio aviso.

Todo eso está probado con casos simulados, incluido el tope y el parpadeo corto.

## `localplay`, no `playback`

El anuncio sale **solo por el transmisor de este nodo**, no hacia los nodos
enlazados. Durante un corte los enlaces están muertos de todos modos, y al
volver no tiene caso anunciarle la falla a media red.

## Instalación

```bash
cd extras/radio-alerta
sudo ./install.sh
```

Instala `espeak-ng` y `sox` si faltan, copia los scripts, escribe las frases en
`/etc/radio-alerta/frases.conf` y **genera los audios**.

Requiere el [vigilante del túnel](../vpn-watchdog/) para dispararse solo; sin él
los scripts funcionan igual pero hay que llamarlos a mano.

## Probar

```bash
# escuchar en la Pi, SIN transmitir
play /usr/share/asterisk/sounds/custom/alerta-sin_internet.ulaw

# sacarlo AL AIRE por el nodo — esto SÍ transmite
sudo /usr/local/sbin/radio-alerta.sh sin_internet

# ver qué ha anunciado
journalctl -t radio-alerta --since today
```

## Cambiar los textos

```bash
sudo nano /etc/radio-alerta/frases.conf
sudo /usr/local/sbin/radio-alerta-generar.sh
```

Manten las frases **por debajo de ocho segundos**. El generador imprime la
duración de cada una para que sea fácil ajustarlas.

Si tu nodo no es el 1001, edita `NODO` en `/usr/local/sbin/radio-alerta.sh`.

## Ajustar la política

```bash
sudo systemctl edit vpn-watchdog.service
```

```ini
[Service]
Environment=ANUNCIAR_TRAS=3      # revisiones antes del primer aviso
Environment=REPETIR_CADA=15      # revisiones entre repeticiones
Environment=MAX_ANUNCIOS=12      # tope de avisos por episodio
Environment=ANUNCIAR=0           # apagar los anuncios sin desinstalar nada
```
