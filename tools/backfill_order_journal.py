"""Reconcile hydra_order_journal.json against hydra_kraken_trades.sqlite,
and (with --apply) backfill missing trades into the journal.

Scope: the 5 pairs Hydra has ever traded — SOL/USD, SOL/USDC, SOL/BTC,
BTC/USD, BTC/USDC. Trades on other pairs (ADA, ETH, ZEC, DOGE, NIGHT, …)
stay in the Kraken trades store as the user's personal record but do
NOT pollute the Hydra order journal — that journal is Hydra-context.

Two-pass safety:

    python -m tools.backfill_order_journal              # DRY RUN — shows diff
    python -m tools.backfill_order_journal --apply      # writes journal + backup

CLAUDE.md Rule 2: STOP THE AGENT before --apply. Hydra appends to the
order journal on every fill; an in-flight write will overwrite this
backfill.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Set, Tuple


HYDRA_RELEVANT_PAIRS = (
    "BTC/USD", "BTC/USDC", "BTC/USDT",
    "ETH/USD", "ETH/USDC", "ETH/USDT",
    "ZEC/USD", "ZEC/USDC",
    "SOL/USD", "SOL/USDC", "SOL/USDT", "SOL/BTC",
)


def _load_journal(journal_path: str) -> List[dict]:
    with open(journal_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_journal_atomic(journal_path: str, journal: List[dict]) -> None:
    """Atomic write: temp file + os.replace. Matches the convention used
    by the live agent's _save_snapshot path."""
    tmp = journal_path + ".backfill.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2)
    os.replace(tmp, journal_path)


def _journal_known_txids(journal: List[dict]) -> Set[str]:
    """Set of all Kraken txids the journal already has via lifecycle.exec_ids."""
    seen: Set[str] = set()
    for e in journal:
        lc = e.get("lifecycle") or {}
        for tx in (lc.get("exec_ids") or []):
            if tx:
                seen.add(tx)
    return seen


def _kraken_relevant_trades(db_path: str) -> List[dict]:
    """All rows in hydra_kraken_trades.sqlite for the 5 Hydra-relevant pairs."""
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" * len(HYDRA_RELEVANT_PAIRS))
        rows = conn.execute(
            f"""SELECT txid, ordertxid, pair_canonical, side, ordertype,
                       price, vol, cost, fee, time_unix, raw_json
                FROM trades
                WHERE pair_canonical IN ({placeholders})
                ORDER BY time_unix ASC""",
            HYDRA_RELEVANT_PAIRS,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "txid": r[0], "ordertxid": r[1], "pair": r[2], "side": r[3],
            "ordertype": r[4], "price": r[5], "vol": r[6], "cost": r[7],
            "fee": r[8], "time_unix": r[9], "raw_json": r[10],
        })
    return out


def _build_backfill_entry(t: dict, today_iso: str) -> dict:
    """Construct a journal-shaped entry from a Kraken trade row.

    Matches the existing kraken_backfill convention exactly (see e.g.
    the 2026-04-11 backfill entries already present in the journal).
    """
    iso_ts = (
        dt.datetime.fromtimestamp(t["time_unix"], tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "+00:00")  # stay explicit
    )
    return {
        "placed_at": iso_ts,
        "pair": t["pair"],
        "side": t["side"].upper(),  # journal convention is uppercase
        "intent": {
            "amount": float(t["vol"]),
            "limit_price": float(t["price"]),
            "post_only": True,
            "order_type": t["ordertype"] or "limit",
            "paper": False,
        },
        "decision": {
            "strategy": None,
            "regime": None,
            "reason": (
                f"[BACKFILL] Reconstructed from Kraken trades-history on "
                f"{today_iso}; user-action trade or pre-Hydra entry not "
                f"originally journaled. Source: hydra_kraken_trades.sqlite."
            ),
            "confidence": None,
            "params_at_entry": None,
            "cross_pair_override": None,
            "book_confidence_modifier": None,
            "brain_verdict": None,
            "swap_id": None,
        },
        "order_ref": {
            "order_userref": None,
            "order_id": t["ordertxid"] or None,
        },
        "lifecycle": {
            "state": "FILLED",
            "vol_exec": float(t["vol"]),
            "avg_fill_price": float(t["price"]),
            "fee_quote": float(t["fee"]) if t["fee"] is not None else None,
            "final_at": iso_ts,
            "terminal_reason": None,
            "exec_ids": [t["txid"]],
        },
        "source": "kraken_backfill",
    }


def _diff(journal: List[dict], kraken_trades: List[dict]) -> Tuple[List[dict], Dict[str, int], List[dict]]:
    """Return (missing_trades, by_pair_count, journal_only_entries).

    journal_only_entries: entries with FILLED state in the journal whose
    sole exec_id is NOT in Kraken's records — likely a paper run, a
    journal-side bug, or a pair we don't sync. Reported for visibility,
    not auto-pruned."""
    known_txids = _journal_known_txids(journal)
    missing: List[dict] = []
    by_pair: Dict[str, int] = {}
    for t in kraken_trades:
        if t["txid"] in known_txids:
            continue
        missing.append(t)
        by_pair[t["pair"]] = by_pair.get(t["pair"], 0) + 1

    kraken_txids = {t["txid"] for t in kraken_trades}
    journal_only: List[dict] = []
    for e in journal:
        lc = e.get("lifecycle") or {}
        if lc.get("state") not in ("FILLED", "PARTIALLY_FILLED"):
            continue
        ids = lc.get("exec_ids") or []
        if not ids:
            continue
        if not any(tx in kraken_txids for tx in ids):
            journal_only.append(e)
    return missing, by_pair, journal_only


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal",
                    default="C:/Users/elamj/Dev/Hydra/hydra_order_journal.json",
                    help="Path to hydra_order_journal.json (live repo)")
    ap.add_argument("--db", default="hydra_kraken_trades.sqlite",
                    help="Path to hydra_kraken_trades.sqlite (this branch)")
    ap.add_argument("--apply", action="store_true",
                    help="Write the backfilled journal. Without this, dry-run only.")
    args = ap.parse_args()

    if not os.path.exists(args.journal):
        print(f"ERROR: journal not found: {args.journal}", file=sys.stderr)
        return 1
    if not os.path.exists(args.db):
        print(f"ERROR: kraken trades store not found: {args.db}", file=sys.stderr)
        return 1

    journal = _load_journal(args.journal)
    kraken_trades = _kraken_relevant_trades(args.db)

    print(f"Journal entries:                {len(journal)}")
    print(f"Kraken trades (5 pairs):        {len(kraken_trades)}")

    missing, by_pair, journal_only = _diff(journal, kraken_trades)
    print(f"\nMissing-from-journal:           {len(missing)}")
    for p in sorted(by_pair):
        print(f"  {p:12s}  {by_pair[p]}")

    print(f"\nJournal-only (no Kraken match): {len(journal_only)}")
    if journal_only:
        for e in journal_only[:5]:
            ids = (e.get("lifecycle") or {}).get("exec_ids") or []
            print(f"  {e.get('placed_at','?')[:19]} {e.get('pair'):10s} "
                  f"{e.get('side'):4s} ids={ids}")
        if len(journal_only) > 5:
            print(f"  ... and {len(journal_only) - 5} more")

    if not missing:
        print("\nNothing to backfill.")
        return 0

    today_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    backfill_entries = [_build_backfill_entry(t, today_iso) for t in missing]

    print(f"\nWill build {len(backfill_entries)} backfill entries with reason "
          f"prefix '[BACKFILL] Reconstructed from Kraken trades-history on {today_iso}'.")
    print("Sample (first 3):")
    for be in backfill_entries[:3]:
        print(f"  {be['placed_at'][:19]} {be['pair']:10s} {be['side']:4s} "
              f"vol={be['intent']['amount']:.6f}@${be['intent']['limit_price']:.6f} "
              f"txid={be['lifecycle']['exec_ids'][0]}")

    if not args.apply:
        print("\nDRY RUN — no journal changes written. Re-run with --apply "
              "after stopping the live agent (CLAUDE.md Rule 2).")
        return 0

    # Backup before write.
    backup = args.journal + f".backup-{int(time.time())}"
    shutil.copy2(args.journal, backup)
    print(f"\nBackup: {backup}")

    # Append in-place; sort by placed_at to preserve chronological reading.
    new_journal = list(journal) + backfill_entries
    new_journal.sort(key=lambda e: e.get("placed_at") or "")
    _save_journal_atomic(args.journal, new_journal)
    print(f"Wrote {len(new_journal)} entries (was {len(journal)}, +{len(backfill_entries)}) "
          f"to {args.journal}")
    print("\nIMPORTANT: restart the agent to pick up the backfilled journal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
