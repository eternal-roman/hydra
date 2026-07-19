# heartbeat eval — ETH/USD 1h

- events: **77** (reversal 38 / fake 39; 0 tainted excluded)
- sufficient events (>= 60): **True**
- promote criterion: AUC >= 0.7 by bounce+3, walk-forward
- lead-time (earliest checkpoint AUC >= 0.70): **not reached**

| checkpoint | n | AUC | Brier | separation |
|---|---|---|---|---|
| bounce+1 | 77 | 0.5621 | 0.4139 | 0.0089 |
| bounce+2 | 77 | 0.587 | 0.4072 | 0.0076 |
| bounce+3 | 77 | 0.5911 | 0.4075 | 0.0161 |
| progress_2atr | 73 | 0.4805 | 0.4283 | 0.0169 |

## Calibration (bounce+3)

| bin | n | mean pred | obs freq |
|---|---|---|---|
| 0.0-0.1 | 1 | 0.0702 | 0.0 |
| 0.1-0.2 | 0 | None | None |
| 0.2-0.3 | 7 | 0.2549 | 0.5714 |
| 0.3-0.4 | 5 | 0.369 | 0.4 |
| 0.4-0.5 | 2 | 0.4906 | 0.0 |
| 0.5-0.6 | 2 | 0.5567 | 0.5 |
| 0.6-0.7 | 0 | None | None |
| 0.7-0.8 | 2 | 0.7969 | 0.0 |
| 0.8-0.9 | 5 | 0.8713 | 0.6 |
| 0.9-1.0 | 53 | 0.9821 | 0.5283 |

## 5 worst-classified events (bounce+3)

| low ts | label | P(up) | error | tape |
|---|---|---|---|---|
| 2026-06-02 19:00 | fake | 0.9998 | 0.9998 | candles [1052..1055] |
| 2026-05-04 20:00 | fake | 0.9997 | 0.9997 | candles [357..360] |
| 2026-05-05 09:00 | fake | 0.9995 | 0.9995 | candles [370..377] |
| 2026-06-02 23:00 | fake | 0.9994 | 0.9994 | candles [1056..1060] |
| 2026-06-05 19:00 | fake | 0.999 | 0.999 | candles [1124..1133] |
