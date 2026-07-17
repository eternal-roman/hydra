"""Parquet append store for tape, candles, posterior series, and scalers.

Layout (under `store.root`, default `data/`):

    <root>/<PAIR>/<tf>/tape/part-<first_trade_ns>-<seq>.parquet
    <root>/<PAIR>/<tf>/posterior/part-<first_ts_ns>-<seq>.parquet
    <root>/<PAIR>/<tf>/scalers.json

Parquet files are immutable once written; "append" means writing a new
part file. Readers glob all parts, concatenate, sort by (ts, trade_id)
and de-duplicate — so overlapping backfills are safe and replays are
deterministic regardless of how the tape was captured.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from .feed.tape import Side, Trade

TAPE_SCHEMA = pa.schema([
    ("ts", pa.float64()),
    ("price", pa.float64()),
    ("qty", pa.float64()),
    ("side", pa.string()),
    ("ord_type", pa.string()),
    ("trade_id", pa.int64()),
])

POSTERIOR_SCHEMA = pa.schema([
    ("ts", pa.float64()),          # candle close time (exchange)
    ("candle_open_ts", pa.float64()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("buy_vol", pa.float64()),
    ("sell_vol", pa.float64()),
    ("trade_count", pa.int64()),
    ("vwap", pa.float64()),
    ("L", pa.float64()),
    ("p_up", pa.float64()),
    ("tainted", pa.bool_()),
    ("features_json", pa.string()),  # {name: {raw, z, S}} at candle close
])


def _safe_pair(pair: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", pair)


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- paths ---------------------------------------------------------------

    def dir_for(self, pair: str, tf: str, kind: str) -> Path:
        d = self.root / _safe_pair(pair) / tf / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _part_path(d: Path, first_ts: float) -> Path:
        """Collision-safe immutable part name. The fixed-width numeric
        sequence suffix keeps lexicographic order == write order, which
        read_posterior's last-write-wins dedup relies on."""
        first_ns = int(first_ts * 1e9)
        for i in range(1000):
            path = d / f"part-{first_ns:020d}-{i:03d}.parquet"
            if not path.exists():
                return path
        raise RuntimeError(f"1000 part collisions at {d} ts={first_ts}")

    # -- tape ------------------------------------------------------------------

    def append_tape(self, pair: str, tf: str, trades: list[Trade]) -> Optional[Path]:
        if not trades:
            return None
        d = self.dir_for(pair, tf, "tape")
        path = self._part_path(d, trades[0].ts)
        table = pa.Table.from_pydict({
            "ts": [t.ts for t in trades],
            "price": [t.price for t in trades],
            "qty": [t.qty for t in trades],
            "side": [t.side.value for t in trades],
            "ord_type": [t.ord_type for t in trades],
            "trade_id": [t.trade_id for t in trades],
        }, schema=TAPE_SCHEMA)
        pq.write_table(table, path)
        return path

    def read_tape(self, pair: str, tf: str,
                  ts_start: float = float("-inf"),
                  ts_end: float = float("inf")) -> list[Trade]:
        d = self.dir_for(pair, tf, "tape")
        rows: list[Trade] = []
        for part in sorted(d.glob("part-*.parquet")):
            t = pq.read_table(part)
            cols = {name: t.column(name).to_pylist() for name in t.schema.names}
            for j in range(t.num_rows):
                ts = cols["ts"][j]
                if ts_start <= ts <= ts_end:
                    rows.append(Trade(
                        ts=ts, price=cols["price"][j], qty=cols["qty"][j],
                        side=Side(cols["side"][j]), ord_type=cols["ord_type"][j],
                        trade_id=cols["trade_id"][j]))
        rows.sort(key=Trade.sort_key)
        # de-dup exact records (overlapping backfill parts)
        out: list[Trade] = []
        prev = None
        for r in rows:
            key = (r.ts, r.trade_id, r.price, r.qty, r.side)
            if key != prev:
                out.append(r)
            prev = key
        return out

    def read_tape_file(self, path: str | Path) -> list[Trade]:
        t = pq.read_table(path)
        cols = {name: t.column(name).to_pylist() for name in t.schema.names}
        rows = [Trade(ts=cols["ts"][j], price=cols["price"][j], qty=cols["qty"][j],
                      side=Side(cols["side"][j]), ord_type=cols["ord_type"][j],
                      trade_id=cols["trade_id"][j])
                for j in range(t.num_rows)]
        rows.sort(key=Trade.sort_key)
        return rows

    # -- posterior series ------------------------------------------------------

    def append_posterior(self, pair: str, tf: str, rows: list[dict]) -> Optional[Path]:
        if not rows:
            return None
        d = self.dir_for(pair, tf, "posterior")
        path = self._part_path(d, rows[0]["ts"])
        table = pa.Table.from_pydict(
            {f.name: [r[f.name] for r in rows] for f in POSTERIOR_SCHEMA},
            schema=POSTERIOR_SCHEMA)
        pq.write_table(table, path)
        return path

    def read_posterior(self, pair: str, tf: str) -> list[dict]:
        d = self.dir_for(pair, tf, "posterior")
        rows: list[dict] = []
        for part in sorted(d.glob("part-*.parquet")):
            t = pq.read_table(part)
            names = t.schema.names
            cols = {n: t.column(n).to_pylist() for n in names}
            rows.extend({n: cols[n][j] for n in names} for j in range(t.num_rows))
        rows.sort(key=lambda r: r["ts"])
        dedup: dict[float, dict] = {r["ts"]: r for r in rows}  # last write wins
        return [dedup[k] for k in sorted(dedup)]

    # -- scalers ---------------------------------------------------------------

    def scalers_path(self, pair: str, tf: str) -> Path:
        return self.root / _safe_pair(pair) / tf / "scalers.json"

    def save_scalers(self, pair: str, tf: str, state: dict) -> None:
        path = self.scalers_path(pair, tf)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp, path)

    def load_scalers(self, pair: str, tf: str) -> Optional[dict]:
        path = self.scalers_path(pair, tf)
        if not path.exists():
            return None
        return json.loads(path.read_text())
