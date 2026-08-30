# Cableado y hardware

Control térmico del gabinete: dos DS18B20 → Raspberry Pi (servidor ASL) →
ventilador PWM de 4 hilos, con telemetría a Home Assistant por WireGuard.

**Instalación: gabinete montado en torre.** Eso define varias decisiones de este
documento — el ruido no importa, el acceso físico es caro, y el ambiente de RF
es mucho más agresivo que un banco de trabajo.

---

## 1. El problema que resuelve

El controlador original apagaba el ventilador **cortando su alimentación** al
bajar de 39 °C. Al cortar el aire, el calor que el TX de 10 M sigue radiando se
queda dentro y sube la temperatura de todo lo demás, incluida la Raspberry Pi.

Medido en esta Pi: `vcgencmd get_throttled` = `0x80000`, o sea que **ya tocó el
límite térmico suave de 60 °C** y el SoC bajó su reloj de 1.4 a 1.2 GHz.

La lógica nueva agrega lo que le faltaba al controlador:

- **Dos puntos de medición**: arranca si *cualquiera* de los dos radios se
  calienta, no sólo donde estaba la sonda única.
- **Purga**: al bajar del umbral de paro, el ventilador sigue a máximas
  revoluciones un tiempo configurable antes de bajar. Barre el calor residual.
- **Telemetría y ajuste remoto** desde Home Assistant.
- **Alarma de ventilador trabado**, que importa mucho estando en una torre.

---

## 2. Diseño tal como quedó armado

Cuatro cables entre la Pi y el ventilador. **Sin relevadores, sin buffer, sin
controlador de respaldo.**

```
   Fuente 12 V ────────────────────────o  pin 2  +12 V   (amarillo)

   GPIO18 (pin 12) ────────────────────o  pin 4  PWM     (azul)

                    3.3 V (pin 1)
                        │
                      [10 kΩ]
                        │
   GPIO23 (pin 16) ─────┴──[1 kΩ]──────o  pin 3  TACH    (verde)

   GND Pi (pin 9) ──┬───────────────────o  pin 1  GND     (negro)
                    │
   GND fuente 12V ──┘        ← EL MISMO PUNTO
```

> **Las masas de la Pi y de la fuente de 12 V tienen que unirse en un punto.**
> Si flotan una respecto de la otra, el PWM y el tacómetro no significan nada, y
> el síntoma es un ventilador que gira errático — fácil de confundir con un
> problema de software.

`GPIO18` va **directo** al pin 4, sin buffer. La Pi mete 3.3 V contra el pull-up
interno del ventilador: medio miliamperio, inofensivo. Por eso
`fan.invert: false` en `config.yaml`. Con un buffer 2N7002 open-drain habría que
ponerlo en `true` (ver apéndice).

### Por qué no hay etapa de apagado

El ventilador **no se detiene con PWM en 0 %**: se queda a ~1725 RPM. Para
pararlo de verdad haría falta cortarle los +12 V con un MOSFET de canal P.

Se decidió **no armarlo**. En una torre el ruido es irrelevante, y un flujo
suave y permanente resuelve mejor el problema original —el calor atrapado— que
apagar y volver a arrancar. Además evita ciclos de arranque, que castigan los
baleros más que el giro continuo, y ayuda contra la condensación.

Consecuencia: el estado `reposo` significa **~1725 RPM**, no cero.

---

## 3. Pines usados

| Función | BCM | Pin | Notas |
|---|---:|---:|---|
| Bus 1-Wire, los dos DS18B20 | GPIO4 | 7 | Un solo pull-up de 4.7 kΩ a 3.3 V |
| PWM del ventilador, 25 kHz | GPIO18 | 12 | Directo al pin 4 |
| Tacómetro | GPIO23 | 16 | 10 kΩ a 3.3 V + 1 kΩ en serie |
| 3.3 V | — | 1 | Sensores y pull-ups |
| GND | — | 9 | Masa común |

`GPIO24` y `GPIO25` quedan libres: eran para el corte de +12 V y el watchdog por
hardware del apéndice.

### Conflicto que hubo que resolver

Esta Pi 3 B+ tenía `dtparam=audio=on`, y en esa placa el audio analógico interno
**ocupa PWM0 y PWM1** — el mismo hardware que necesita GPIO18. Los nodos usan
`rxchannel = SimpleUSB/…`, o sea fobs USB, así que el instalador lo apaga y
libera el PWM sin afectar la operación de ASL.

---

## 4. Los dos sensores DS18B20

Los dos van en el **mismo bus**, en paralelo. El kernel los distingue por su
número de serie.

```
   3.3 V (pin 1) o──────┬──────────────────o VDD ──┐
                        │                          │
                      [4.7 kΩ]           [100 nF]  │
                        │                     │    │
   GPIO4 (pin 7) o──────┴──────────────────o DQ ───┘
                                                   │
   GND   (pin 9) o──────────────────────────o GND ─┘
```

- **Una sola resistencia de 4.7 kΩ para todo el bus**, del lado de la Pi.
- **Alimentación de 3 hilos, nunca parásita.** Es la causa número uno de
  lecturas de 85.0 °C; el código trata ese valor como fallo por eso mismo.

### Sensores identificados

| ID | Radio |
|---|---|
| `28-3c01d607e65a` | Radio VHF |
| `28-000000c91978` | TX 10 M |

El segundo es un **clon**: su ROM con tres bytes en cero es el patrón típico. Lee
bien, pero su tolerancia es de ±2 °C en vez de ±0.5. Si algún día marca corrido
un par de grados, ya sabes por qué — se compensa moviendo el umbral.

Medido en el bus: **~2.5 % de lecturas truncadas transitorias en ambos
sensores**. `DS18B20.RETRIES = 2` las absorbe por completo (0 fallos en 120
lecturas).

### RF: esto importa mucho más en una torre

Un bus 1-Wire junto a la antena es una antena. En el banco medimos cero errores,
pero **eso fue sin transmitir y lejos del feedline**. Arriba va a ser distinto:

- **Cable blindado de 3 conductores.** Malla aterrizada **sólo del lado de la
  Pi**; en los dos extremos creas un lazo de masa.
- **100 nF entre VDD y GND en cada sensor**, soldado en las patas del chip.
- **Ferrita de clip** en cada cable, cerca de la Pi.
- Ruta los cables **pegados al chasis y lejos del coaxial**. Nunca en paralelo
  al feedline.
- Si aparecen errores al transmitir: **100 Ω en serie** en DQ del lado de la Pi,
  y baja el pull-up a 2.2 kΩ.

El síntoma a vigilar en Home Assistant es la entidad **«Falla de sensor»**. Si
se enciende sólo durante las transmisiones, es RF y no un sensor muerto.

### Montaje mecánico

Fija cada sensor **al disipador** con pasta térmica y brida o tornillo, y aísla
el encapsulado con kapton — el TO-92 no está aislado. No los cuelgues al aire:
medirías la temperatura del aire y reaccionarías tarde.

---

## 5. Comportamiento ante fallas

Un ventilador de 4 hilos sin nadie manejando su pin de PWM se va al **100 %** por
su propio pull-up interno. Todo el diseño se apoya en eso: **el estado seguro no
es apagado, es soplando.** En una torre eso es exactamente lo que quieres.

| Falla | Qué hace el ventilador |
|---|---|
| Un DS18B20 deja de responder | 100 %, estado `fallo`, alerta en HA |
| Se cae el túnel, HA o el broker | Nada: la lógica es local, MQTT es sólo telemetría |
| Excepción en el servicio | 100 %, y systemd reinicia en 5 s |
| Paro limpio, reinicio, actualización | 100 % mientras dura el hueco |
| La Pi pierde corriente o no arranca | GPIO en alta impedancia → pull-up → 100 % |
| Kernel panic o cuelgue | El watchdog del SoC resetea en ≤60 s → 100 % |
| Se desconecta el cable de PWM | Pull-up → 100 % |
| SIGKILL | Último duty por ~5 s, hasta que systemd reinicia |
| **El ventilador se traba** | **Alarma «Ventilador trabado» en HA** |

Por eso `Fan.close()` deja el PWM **habilitado al 100 %** en vez de apagarlo:
medido aquí, `enable=0` deja la línea en bajo, o sea el ventilador al mínimo —
justo lo que no se quiere sin nadie vigilando.

El watchdog del SoC (`/dev/watchdog0`, BCM2835, 60 s) ya venía activo y systemd
lo alimenta. No hubo que configurar nada.

---

## 6. Curva medida del ventilador

Barrido con `--fan-test`, **con el gabinete cerrado y armado como queda**:

| Duty | RPM | | Duty | RPM |
|---:|---:|---|---:|---:|
| 0 % | 1720 | | 50 % | 2170 |
| 15 % | 1730 | | 60 % | 2750 |
| 25 % | 1730 | | 70 % | 3380 |
| 30 % | 1730 | | 85 % | 4440 |
| 40 % | 1800 | | 100 % | 5000 |

Subida y bajada coinciden dentro de ±10 RPM: no hay histéresis.

### Tiene una zona muerta

**De 0 a ~35 % el ventilador ignora el PWM** y se queda clavado en su piso de
~1725 RPM. El control útil va de **40 % a 100 %**, o sea de 1800 a 5000 RPM.

Esto no es un defecto, es cómo se comporta este modelo. Pero tiene una
consecuencia directa: `min_duty` **no puede quedar por debajo de 40**, o la
rampa arrancaría en `t_on` sin mover ni un RPM hasta cruzar ese punto.

> Ojo si algún día cambias de ventilador: uno de bancada muy parecido a éste
> resultó tener una curva completamente distinta — lineal desde 1010 RPM a 0 %,
> sin zona muerta. **Vuelve a correr `--fan-test`**, no supongas.

De aquí salen tres valores de `config.yaml`:

- `min_duty: 40` — donde el ventilador empieza a responder de verdad.
- `spinup_seconds: 0` — la patada de arranque es innecesaria, nunca se atasca.
- `stall_rpm: 500` — muy por debajo del piso de 1725 RPM, así que sólo dispara
  con el ventilador realmente detenido.

## 7. Puesta en marcha

```bash
sudo ./install.sh          # dependencias, overlays, servicio
sudo reboot                # los overlays sólo cargan al arranque

sudo PYTHONPATH=/opt/gabinete-fan/src python3 -m gabinete_fan --discover
#   pega los IDs 28-… en /etc/gabinete-fan/config.yaml

sudo PYTHONPATH=/opt/gabinete-fan/src python3 -m gabinete_fan --fan-test
#   curva del ventilador; de aquí salen min_duty y si hace falta cortar +12 V

sudo systemctl start gabinete-fan
journalctl -u gabinete-fan -f
```

Para identificar cuál sensor es cuál, caliéntalos **uno a la vez** y mira cuál
sube. Si los cables corren en el mismo mazo es fácil agarrar el mismo dos veces:
haz la prueba cruzada y confirma que sube el otro.

---

## 8. Ajuste de umbrales

Los valores actuales son un punto de partida tomado **en interiores**:

| Parámetro | Valor | |
|---|---:|---|
| `t_on` | 42 °C | Arranca la rampa |
| `t_critical` | 50 °C | 100 % de revoluciones |
| `t_off` | 38 °C | Debajo de esto empieza la purga |
| `hysteresis` | 2 °C | Evita traqueteo al salir de crítico |
| `purge_minutes` | 3 | Purga a máximas revoluciones |

Los seis se cambian desde Home Assistant sin reiniciar nada, quedan guardados en
`/var/lib/gabinete-fan/state.json`, y el servicio rechaza combinaciones
inconsistentes: siempre `t_off < t_on < t_critical`.

### Línea base medida en interiores

19 horas en recepción, sin ventilación: **VHF 39 °C, TX 10 M 35 °C, CPU 52 °C**,
con una deriva diurna de 4 a 8 °C. El ventilador del controlador viejo nunca
llegó a arrancar.

> **Estos números no van a valer en la torre.** Un gabinete metálico al sol
> puede rebasar por mucho la temperatura ambiente, y el ventilador sólo puede
> acercar el interior al aire de afuera, nunca enfriarlo por debajo. Si el
> ambiente supera `t_off`, el ventilador **no podrá detenerse nunca** — es
> correcto y seguro, pero conviene saberlo antes de creer que algo falla.

Planea recalibrar con la gráfica de Home Assistant después de instalar. La
medida de si funcionó no es la temperatura de los radios: es **la de la CPU
después de que el ventilador baja de revoluciones**, y que `get_throttled` deje
de reportar el límite térmico.

---

## Apéndice — respaldo por hardware, si algún día lo quieres

No está armado y no hace falta: systemd y el watchdog del SoC ya cubren los
mismos casos, dejando sólo ventanas de 5 a 60 segundos que son irrelevantes
frente a constantes térmicas de minutos.

Si aun así lo quieres, la versión mínima es **un relevador en serie con la línea
de PWM**, con su bobina sostenida por un detector de pulso faltante con 555
alimentado por un latido en GPIO25:

```
   GPIO18 o────o NO \
                      \___o COM o────o pin 4 del ventilador
       (sin conectar) /
```

Con latido, el contacto cierra y la Pi manda. Sin latido —cuelgue, panic,
SIGKILL, apagón— el contacto abre, la línea queda al aire y el pull-up interno
manda el ventilador al 100 %.

El software ya trae el lado que le toca: `watchdog.heartbeat_gpio` genera el
tren de pulsos a 10 Hz, y sólo late si el lazo de control terminó su última
iteración completa, de modo que un lazo trabado también suelta el mando.

**No combines eso con un corte de +12 V en GPIO24 sin más.** En un cuelgue,
GPIO24 se quedaría en su último estado; si estaba en reposo, el ventilador
quedaría sin alimentación — cero RPM con los radios calientes, peor que ahora.
Si armas las dos cosas, el watchdog tiene que forzar también la alimentación.
