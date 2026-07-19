# heartbeat eval — ZEC/USD 1h

- events: **75** (reversal 33 / fake 42; 0 tainted excluded)
- sufficient events (>= 60): **True**
- promote criterion: AUC >= 0.7 by bounce+3, walk-forward
- lead-time (earliest checkpoint AUC >= 0.70): **not reached**

| checkpoint | n | AUC | Brier | separation |
|---|---|---|---|---|
| bounce+1 | 75 | 0.5418 | 0.4648 | 0.0184 |
| bounce+2 | 75 | 0.5382 | 0.4552 | 0.017 |
| bounce+3 | 75 | 0.5505 | 0.46 | 0.0397 |
| progress_2atr | 74 | 0.4316 | 0.5122 | -0.0048 |

## Calibration (bounce+3)

| bin | n | mean pred | obs freq |
|---|---|---|---|
| 0.0-0.1 | 0 | None | None |
| 0.1-0.2 | 5 | 0.1763 | 0.2 |
| 0.2-0.3 | 1 | 0.2115 | 1.0 |
| 0.3-0.4 | 1 | 0.3463 | 1.0 |
| 0.4-0.5 | 6 | 0.4549 | 0.5 |
| 0.5-0.6 | 4 | 0.5442 | 0.5 |
| 0.6-0.7 | 5 | 0.66 | 1.0 |
| 0.7-0.8 | 4 | 0.7482 | 0.5 |
| 0.8-0.9 | 6 | 0.8621 | 0.0 |
| 0.9-1.0 | 43 | 0.9838 | 0.4186 |

## 5 worst-classified events (bounce+3)

| low ts | label | P(up) | error | tape |
|---|---|---|---|---|
| 2026-06-30 22:00 | fake | 0.9999 | 0.9999 | candles [1725..1728] |
| 2026-06-17 22:00 | fake | 0.9989 | 0.9989 | candles [1413..1419] |
| 2026-07-08 10:00 | fake | 0.9978 | 0.9978 | candles [1905..1908] |
| 2026-05-27 18:00 | fake | 0.9977 | 0.9977 | candles [905..908] |
| 2026-06-05 02:00 | fake | 0.9959 | 0.9959 | candles [1105..1108] |
