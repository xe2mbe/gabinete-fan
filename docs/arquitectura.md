# Arquitectura

Cómo está armado `gabinete-fan` por dentro: qué mide, qué decide y por dónde
sale la telemetría.

---

## 1. Vista general

Tres sensores entran, un ventilador sale, y todo lo demás es telemetría.

```mermaid
flowchart LR
    subgraph GAB["Gabinete en la torre"]
        VHF["DS18B20<br/>disipador VHF"]
        TX["DS18B20<br/>disipador TX 10 M"]
        FAN["Ventilador PWM<br/>4 hilos, 25 kHz"]
    end

    subgraph PI["Raspberry Pi 3B+ · nodo 299084"]
        SOC["Sensor interno<br/>del SoC"]
        CTL["ThermalController<br/>lógica pura, sin E/S"]
        ASL["Asterisk · app_rpt<br/>tiempo de TX por radio"]
    end

    HA["Home Assistant<br/>MQTT discovery"]

    VHF -->|1-Wire| CTL
    TX -->|1-Wire| CTL
    SOC --> CTL
    CTL -->|"duty 0-100 %"| FAN
    FAN -->|tacómetro| CTL
    ASL -.->|contexto| CTL
    CTL -.->|telemetría| HA
    HA -.->|umbrales| CTL
```

La línea punteada es lo que **no** es crítico: si Home Assistant, el broker o el
túnel se caen, la lógica sigue corriendo en la Pi. MQTT es solo telemetría y
ajuste remoto.

---

## 2. La máquina de estados

Decide el duty a partir de las dos temperaturas de los radios.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> REPOSO

    REPOSO --> ACTIVO: algún radio ≥ t_on
    ACTIVO --> CRITICO: algún radio ≥ t_critical
    CRITICO --> ACTIVO: ambos < t_critical − histéresis
    ACTIVO --> PURGA: ambos < t_off
    PURGA --> REPOSO: vence el temporizador
    PURGA --> ACTIVO: vuelve el calor

    REPOSO --> FALLO: sensor caído
    ACTIVO --> FALLO: sensor caído
    CRITICO --> FALLO: sensor caído
    PURGA --> FALLO: sensor caído
    FALLO --> REPOSO: sensores recuperados

    note right of ACTIVO
        duty = rampa lineal
        min_duty..100 %
        entre t_on y t_critical
    end note

    note right of PURGA
        100 % durante
        purge_minutes
    end note

    note right of FALLO
        100 %: junto a un TX activo
        no se puede suponer que hace frío
    end note
```

**La purga es el punto del diseño.** El controlador original apagaba el
ventilador al bajar del umbral, y el calor que el TX seguía radiando quedaba
atrapado y calentaba la Pi. Antes de detenerse, el ventilador barre ese calor
residual a máximas revoluciones.

---

## 3. El piso por temperatura de CPU

Encima de la máquina de estados hay una segunda razón para soplar, y es
independiente de los radios.

```mermaid
flowchart TD
    A["Lectura de CPU"] --> B{"¿ya estaba<br/>soplando por CPU?"}
    B -->|No| C{"CPU ≥ t_cpu"}
    B -->|Sí| D{"CPU < t_cpu_off"}
    C -->|Sí| E["engancha"]
    C -->|No| F["no interviene"]
    D -->|Sí| G["suelta"]
    D -->|No| E
    E --> H{"¿los radios ya<br/>piden 100 %?"}
    H -->|Sí| F
    H -->|No| I["estado CPU<br/>ventilador al 100 %"]
    G --> F
    F --> J["manda la decisión<br/>de los radios"]
```

Existe porque **en la torre el gabinete se calienta desde afuera**. Medido el 30
de agosto de 2026: con sol y sin un solo keyup, los radios se quedaron en 34 °C
—veinte grados por debajo de `t_on`— mientras la CPU subió a 57.5 °C, más
caliente que su propio pico durante una net de 67 minutos de transmisión.
Mirando solo los radios, el ventilador nunca habría arrancado.

Tres decisiones de diseño:

- **Es un piso, no un control paralelo.** Solo puede pedir *más* aire del que ya
  pidieron los radios, nunca menos. La máquina de estados corre igual por
  debajo, con sus temporizadores intactos; lo único que cambia es la etiqueta.
- **Va al 100 %, no por rampa.** Del umbral a los 60 °C del límite térmico del
  SoC quedan cinco grados, y a esa distancia no hay nada que dosificar. Además
  la curva medida del ventilador tiene zona muerta: a 40 % da 1800 RPM contra un
  piso de 1725, así que los duties intermedios casi no mueven aire.
- **`t_cpu_off` es umbral propio**, no `t_cpu` menos la histéresis de los
  radios, que es otra cosa. Cuidado con pegarlos: la CPU se lee en escalones de
  ~0.5 °C, y medido sobre la traza real, parar en 54 en vez de 53 no ahorra
  tiempo encendido (41 % contra 42 %) y sí mete ciclos de minuto y medio. Para
  que el ventilador corra menos, **sube `t_cpu`**: con arranque en 56 °C baja al
  35 % del tiempo, y en 57 °C al 21 %.

El modo `manual` manda sobre todo esto: es intención explícita del operador.

---

## 4. Una vuelta del lazo

Lo que pasa cada `poll_seconds` (5 s por omisión).

```mermaid
sequenceDiagram
    autonumber
    participant S as Sensores
    participant C as ThermalController
    participant F as Ventilador
    participant W as Watchdog
    participant M as MQTT / HA

    S->>C: dos DS18B20 + CPU
    Note over S: 2 reintentos por lectura,<br/>tras N ciclos fallidos van a FALLO
    C->>C: estado y duty
    C->>F: aplica duty
    F->>C: RPM del tacómetro
    C->>C: ¿ventilador trabado?
    C->>M: publica telemetría
    C->>W: refresca el latido
    Note over W: solo se refresca al terminar<br/>la vuelta completa, para que un lazo<br/>trabado suelte el mando
```

El orden importa. El latido del watchdog se refresca **al final**, de modo que
un lazo que se cuelgue a medio camino deja de latir y entrega el ventilador.

---

## 5. Por dónde llega la telemetría

El nodo está detrás de un enlace sin port forwarding y Home Assistant vive en
otra LAN, sin cliente WireGuard. Un tercer nodo hace de puente.

```mermaid
flowchart LR
    PI["Raspberry Pi<br/>nodo 299084<br/>wg0 10.0.0.2"]
    VPS["Servidor WireGuard<br/>:51820"]
    BR["Nodo puente<br/>wg0 10.0.0.5<br/>eth0 en la LAN de casa"]
    MQ["Mosquitto<br/>add-on de Home Assistant<br/>:1883"]

    PI -->|wg0| VPS
    VPS -->|wg0| BR
    BR -->|"DNAT solo del 1883"| MQ
```

El nodo puente reenvía **únicamente el puerto 1883** desde el túnel hacia el
broker, con reglas `PostUp` en su `wg0.conf`, así que sobreviven a los
reinicios. Por eso `mqtt.host` apunta al nodo puente y no a la IP real de Home
Assistant.

Ese puente se reinicia por cron una vez por semana: la telemetría se corta un
minuto y Home Assistant marca el dispositivo como no disponible. **El ventilador
no se entera.**

---

## 6. Los módulos

```mermaid
flowchart TD
    MAIN["__main__.py<br/>lazo de control y CLI"]
    CFG["config.py<br/>Params + persistencia atómica"]
    CTL["controller.py<br/>máquina de estados"]
    SEN["sensors.py<br/>DS18B20, CPU, throttling"]
    FAN["fan.py<br/>PWM, tacómetro, trabado"]
    WD["watchdog.py<br/>latido de hardware"]
    MQ["mqtt_ha.py<br/>discovery y comandos"]
    ASL["asl.py<br/>estadísticas de TX"]

    MAIN --> CFG
    MAIN --> CTL
    MAIN --> SEN
    MAIN --> FAN
    MAIN --> WD
    MAIN --> MQ
    MAIN --> ASL
    CTL --> CFG
    MQ -->|cambios de umbral| CFG
```

`controller.py` y el detector de ventilador trabado de `fan.py` **no tocan
hardware a propósito**: son lógica pura, y por eso las 44 pruebas corren en
cualquier máquina sin Raspberry Pi.

---

## 7. Comportamiento ante fallas

Un ventilador de 4 hilos sin nadie manejando su pin de PWM se va al **100 %** por
su propio pull-up interno. Todo el diseño se apoya en eso: **el estado seguro no
es apagado, es soplando.** En una torre eso es exactamente lo que se quiere.

| Falla | Qué hace el ventilador |
|---|---|
| Un DS18B20 deja de responder | 100 %, estado `fallo`, alerta en HA |
| La CPU se calienta sin tráfico | 100 % por el piso de CPU |
| Se cae el túnel, HA o el broker | Nada: la lógica es local |
| Excepción en el servicio | 100 %, y systemd reinicia en 5 s |
| Paro limpio, reinicio o actualización | 100 % mientras dura el hueco |
| La Pi pierde corriente o no arranca | GPIO en alta impedancia → pull-up → 100 % |
| Kernel panic o cuelgue | El watchdog del SoC resetea en ≤60 s → 100 % |
| Se desconecta el cable de PWM | Pull-up → 100 % |
| SIGKILL | Último duty ~5 s, hasta que systemd reinicia |
| El ventilador se traba | Alarma «Ventilador trabado» en HA |

Por eso `Fan.close()` deja el PWM **habilitado al 100 %** en vez de apagarlo:
medido en la Pi 3B+, `enable=0` deja la línea en bajo, o sea el ventilador al
mínimo — justo lo que no se quiere sin nadie vigilando.

---

El cableado, la lista de materiales y la curva medida del ventilador están en
**[hardware.md](hardware.md)**. La puesta en marcha, en
**[instalacion.md](instalacion.md)**.
