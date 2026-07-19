# heartbeat eval — BTC/USD 1h

- events: **69** (reversal 31 / fake 38; 0 tainted excluded)
- sufficient events (>= 60): **True**
- promote criterion: AUC >= 0.7 by bounce+3, walk-forward
- lead-time (earliest checkpoint AUC >= 0.70): **not reached**

| checkpoint | n | AUC | Brier | separation |
|---|---|---|---|---|
| bounce+1 | 69 | 0.5611 | 0.4163 | 0.0037 |
| bounce+2 | 69 | 0.5586 | 0.4177 | -0.0191 |
| bounce+3 | 69 | 0.5518 | 0.4168 | -0.0224 |
| progress_2atr | 65 | 0.527 | 0.3996 | -0.0008 |

## Calibration (bounce+3)

| bin | n | mean pred | obs freq |
|---|---|---|---|
| 0.0-0.1 | 5 | 0.044 | 0.4 |
| 0.1-0.2 | 1 | 0.1087 | 1.0 |
| 0.2-0.3 | 5 | 0.2258 | 0.6 |
| 0.3-0.4 | 2 | 0.3316 | 0.0 |
| 0.4-0.5 | 5 | 0.4324 | 0.6 |
| 0.5-0.6 | 3 | 0.5599 | 0.3333 |
| 0.6-0.7 | 6 | 0.6521 | 0.5 |
| 0.7-0.8 | 3 | 0.7335 | 0.3333 |
| 0.8-0.9 | 11 | 0.8508 | 0.2727 |
| 0.9-1.0 | 28 | 0.9771 | 0.5 |

## 5 worst-classified events (bounce+3)

| low ts | label | P(up) | error | tape |
|---|---|---|---|---|
| 2026-06-04 11:00 | fake | 0.9992 | 0.9992 | candles [1094..1113] |
| 2026-06-02 15:00 | fake | 0.9973 | 0.9973 | candles [1050..1054] |
| 2026-06-02 23:00 | fake | 0.9934 | 0.9934 | candles [1058..1062] |
| 2026-06-03 20:00 | fake | 0.993 | 0.993 | candles [1079..1082] |
| 2026-05-26 18:00 | fake | 0.9837 | 0.9837 | candles [885..889] |
