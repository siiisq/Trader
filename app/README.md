# Trader Paper: panel de operaciones paper

Una página para ver lo que hacen las skills `trader-*`: el valor de la cartera,
las posiciones frente a su peso objetivo, las propuestas con su veredicto de
`/trader-risk` y cada decisión que registró `/trader-broker`.

Es **solo lectura**: nunca aprueba ni envía órdenes. Eso se sigue haciendo en el
chat, con tu "yes" para cada orden.

## Arrancar

Python 3.8+ y nada más que instalar. Desde la raíz del repo:

```bash
python3 app/server.py --data app/demo/trader   # demo: el ejemplo de Berkshire
python3 app/server.py                          # tus datos reales, en trader/
```

Abre http://127.0.0.1:8765.

## De dónde salen los datos

**Ficheros locales (por defecto).** Lee lo que escriben las skills en `trader/`:
`config.json`, `targets.json`, `portfolio.json`, `prices.json`, `orders.json`,
`state/risk.json` y `state/broker.json`. Como estos ficheros no guardan
histórico, el panel añade un punto a `state/equity.json` cada 15 minutos
mientras está abierto. El botón "Actualizar precios" ejecuta `trader-alerts`
para traer cotizaciones con retraso.

**Cuenta paper de Alpaca.** Si defines las claves, la cuenta, las posiciones,
las órdenes y el histórico vienen directamente de Alpaca (solo peticiones GET):

```bash
export ALPACA_API_KEY=...      # claves de la cuenta PAPER
export ALPACA_SECRET_KEY=...
python3 app/server.py
```

`ALPACA_BASE_URL` es por defecto `https://paper-api.alpaca.markets`. Las
propuestas y los veredictos de riesgo se siguen leyendo de `trader/`.

## Flujo con las skills

```
/trader-copycat  ->  /trader-budget  ->  /trader-risk  ->  /trader-broker (tu "yes")
                                                            |
                         el panel lo muestra  <-------------+
```

No es asesoramiento financiero.
