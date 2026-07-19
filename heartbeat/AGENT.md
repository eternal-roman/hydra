# AGENT.md — heartbeat agent / MCP contract

Binding contract for LLM agents and MCP hosts that consume the
**heartbeat** indicator package (`heartbeat-flow` on install; import
`heartbeat`).

## Thesis

- Pure indicator: **P(up)** posterior from trade tape (aggressor side +
  price/qty).
- Asset-agnostic: crypto **or** equity when trades carry `side`.
- **No order path.** Tools never place, cancel, or size orders.
- Missing/invalid data → structured error envelope, never silent
  coin-flip without warning.

## Integration (recommended)

```python
from heartbeat import TOOL_SCHEMAS, call_tool

# 1) Advertise schemas to the host (MCP tools/list, function calling, …)
tools = TOOL_SCHEMAS

# 2) Dispatch every tool call through the envelope API
envelope = call_tool("heartbeat_requirements", {})
if envelope["ok"]:
    reqs = envelope["result"]
else:
    code = envelope["error"]["code"]  # e.g. missing_dataset
    # do not parse traceback text — branch on code
```

Optional public re-exports:

```python
from heartbeat import call_tool, TOOL_SCHEMAS
```

There is **no** built-in MCP stdio server in this package (avoids an MCP
SDK dependency). Wire `TOOL_SCHEMAS` + `call_tool` into any host, or
wrap them in a thin stdio JSON-RPC adapter in your agent runtime.

## Tools

| name | purpose |
|------|---------|
| `heartbeat_requirements` | Dataset formats + required columns (`dataset_requirements()`) |
| `heartbeat_run_dataset` | Batch run: path **or** rows → `IndicatorResult` dict |
| `heartbeat_explain` | Short interpretation text (p_up, tainted, uncalibrated) |

### Arguments — `heartbeat_run_dataset`

| arg | required | notes |
|-----|----------|--------|
| `path` | path **or** `rows` | Local `.csv` / `.jsonl` / `.json` (list of objects) |
| `rows` | path **or** `rows` | List of `{ts, price, qty, side}` objects |
| `symbol` | no | Free-form label (default `UNKNOWN`) |
| `tf` | no | Timeframe string (default `1h`) |
| `weights_path` | no | Calibrated weights JSON; omit → auto-find or degraded |

Provide **exactly one** of `path` / `rows`. Both or neither →
`invalid_arguments`.

### Envelope

Success:

```json
{"ok": true, "result": { ... }}
```

Failure (never a raised exception from `call_tool`):

```json
{
  "ok": false,
  "error": {
    "code": "missing_dataset",
    "message": "dataset not found: ...",
    "hint": "required columns: ts|timestamp|time, price, ..."
  }
}
```

| code | meaning |
|------|---------|
| `missing_dataset` | Empty path, missing file, unreadable path |
| `invalid_dataset` | Schema/parse/side/empty-after-parse |
| `invalid_arguments` | Bad/missing tool args (e.g. no path or rows) |
| `unknown_tool` | Name not in `TOOL_SCHEMAS` |
| `tool_error` | Unexpected internal failure (rare) |

`call_tool` **never raises**. Branch on `ok` and `error.code`.

## Interpreting results

`heartbeat_run_dataset` success `result` matches `IndicatorResult.to_dict()`:

| field | agent rule |
|-------|------------|
| `p_up` | ∈ [0, 1] posterior P(up) at last closed candle; null if no series |
| `L` | log-odds companion |
| `ts` | exchange timestamp of last series point |
| `tainted` | if `true` → **no opinion** (never treat as 0.5) |
| `status` | `ok` \| `degraded` \| `error` |
| `warnings` | includes uncalibrated disclosure when weights missing |
| `series` | closed-candle history (each row has `p_up`, etc.) |

### Uncalibrated weights

When no weights file is found for `symbol`/`tf` and `weights_path` is
omitted:

- `status` = `"degraded"` (if any candles closed)
- `warnings` contains  
  `uncalibrated_weights: p_up uses default_weight (near coin-flip)`

Agents must surface this; do not present degraded p_up as calibrated
edge.

### Data requirements (summary)

Required columns (aliases allowed):

- **ts** — `ts` \| `timestamp` \| `time`
- **price** — `price`
- **qty** — `qty` \| `quantity` \| `size` \| `volume`
- **side** — `side` \| `aggressor` (`buy`/`sell`/`b`/`s`/`1`/`-1`)

Optional: `trade_id`, `ord_type`.

**OHLCV-only without aggressor side is not supported.** Call
`heartbeat_requirements` for the full machine-readable description.

## Python API (non-tool)

For in-process use without the envelope:

```python
from heartbeat import run_dataset, MissingDatasetError, InvalidDatasetError

try:
    result = run_dataset("tape.csv", symbol="AAPL", tf="1h")
    print(result.p_up, result.status, result.warnings)
except MissingDatasetError as e:
    print(e.code, e.hint)
```

CLI: `heartbeat run-dataset PATH --symbol AAPL --tf 1h [--json]`  
(exit 2 = missing, 3 = invalid).

## Hard rules for agents

1. **No orders** from heartbeat tools or status JSON.
2. **`tainted: true` or stale `ts` ⇒ no opinion** — never invent 0.5.
3. **Surface `degraded` / uncalibrated warnings** to the user.
4. **Prefer error codes over message string matching.**
5. **Do not invent OHLCV→side heuristics** in the agent; request a proper
   tape or fail closed with `invalid_dataset`.
