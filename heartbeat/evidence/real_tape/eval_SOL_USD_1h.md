# heartbeat eval — SOL/USD 1h

- events: **80** (reversal 31 / fake 49; 0 tainted excluded)
- sufficient events (>= 60): **True**
- promote criterion: AUC >= 0.7 by bounce+3, walk-forward
- lead-time (earliest checkpoint AUC >= 0.70): **not reached**

| checkpoint | n | AUC | Brier | separation |
|---|---|---|---|---|
| bounce+1 | 80 | 0.5734 | 0.3617 | 0.1487 |
| bounce+2 | 80 | 0.5695 | 0.3591 | 0.0927 |
| bounce+3 | 80 | 0.5537 | 0.3675 | 0.1349 |
| progress_2atr | 74 | 0.4974 | 0.3858 | 0.039 |

## Calibration (bounce+3)

| bin | n | mean pred | obs freq |
|---|---|---|---|
| 0.0-0.1 | 4 | 0.0448 | 0.0 |
| 0.1-0.2 | 4 | 0.1509 | 0.5 |
| 0.2-0.3 | 6 | 0.2542 | 0.3333 |
| 0.3-0.4 | 4 | 0.3485 | 0.0 |
| 0.4-0.5 | 8 | 0.4673 | 0.625 |
| 0.5-0.6 | 6 | 0.5745 | 0.1667 |
| 0.6-0.7 | 6 | 0.6341 | 0.3333 |
| 0.7-0.8 | 9 | 0.7485 | 0.6667 |
| 0.8-0.9 | 8 | 0.8666 | 0.5 |
| 0.9-1.0 | 25 | 0.9741 | 0.36 |

## 5 worst-classified events (bounce+3)

| low ts | label | P(up) | error | tape |
|---|---|---|---|---|
| 2026-06-02 19:00 | fake | 0.9994 | 0.9994 | candles [1055..1058] |
| 2026-06-02 23:00 | fake | 0.9993 | 0.9993 | candles [1059..1063] |
| 2026-06-03 20:00 | fake | 0.9984 | 0.9984 | candles [1080..1084] |
| 2026-06-03 16:00 | fake | 0.9978 | 0.9978 | candles [1076..1079] |
| 2026-06-05 19:00 | fake | 0.9964 | 0.9964 | candles [1127..1136] |
