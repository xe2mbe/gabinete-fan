# enlace-log

Registra la calidad del enlace, un renglón por minuto, en el mismo directorio
que la telemetría térmica — para poder cruzar ambos.

## Por qué hace falta

La falla es **intermitente y no se deja atrapar a mano**. Medido el 2 de
septiembre de 2026, en menos de media hora:

```
46 % de pérdida  ·  luego cinco minutos seguidos con 0 %  ·  luego 23 %  ·  luego 3 %
```

Y cuando el enlace está malo tampoco se puede entrar por SSH a medirlo: tres
intentos seguidos fallaron justo cuando había algo que ver. Cualquier medición
hecha a mano cae en una ventana buena o mala por pura suerte.

## Qué preguntas contesta

Con unos días de registro, cruzado contra el CSV térmico —que ya guarda los
segundos de transmisión por radio— se puede distinguir:

| Si la pérdida sigue a... | Entonces es |
|---|---|
| **el TX del nodo** | RF del transmisor entrando al CPE 4G |
| **la hora del día** | saturación de la celda |
| **nada en particular** | cobertura pobre o problema del operador |

Esa distinción no se puede hacer con muestras sueltas, y sin acceso a la
interfaz del CPE no hay otra forma de obtener el dato.

## Formato

```
hora,enviados,recibidos,perdida_pct,rtt_min,rtt_avg,rtt_max
20:43:00,120,120,0.0,124.0,165.3,255.0
20:44:00,120,65,45.8,131.0,178.2,301.0
```

Un archivo por día: `enlace-AAAAMMDD.csv`, junto a `tx-AAAAMMDD.csv`.

## Instalación

```bash
cd extras/enlace-log
sudo ./install.sh
```

## Un detalle que costó un error

Las líneas de paquete perdido de `ping -D` dicen `no answer yet for
icmp_seq=123`, y **contienen `icmp_seq`**. Contar los recibidos buscando esa
cadena reportaba 0 % de pérdida siempre — exactamente lo contrario de la
verdad, y de forma silenciosa. Se cuentan buscando `time=`, que solo aparece en
las respuestas reales.
