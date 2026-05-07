"""Test Kraken CLI authentication and order pipeline for APEX."""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hydra_meme_agent import _kraken_cli

has_key = bool(os.environ.get("KRAKEN_API_KEY"))
has_secret = bool(os.environ.get("KRAKEN_API_SECRET"))
print(f"KRAKEN_API_KEY present: {has_key}")
print(f"KRAKEN_API_SECRET present: {has_secret}")

# Test 1: public endpoint
print("\n=== Test 1: orderbook (public) ===")
r = _kraken_cli(["orderbook", "PLAYUSD", "--count", "3"])
if "error" in r:
    print(f"ERROR: {r}")
else:
    key = next(iter(r.keys()), None)
    book = r.get(key, {})
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    print(f"OK: {len(bids)} bids, {len(asks)} asks")
    if asks:
        print(f"Best ask: {asks[0][0]}")

# Test 2: authenticated balance check
print("\n=== Test 2: balance (auth required) ===")
r2 = _kraken_cli(["balance"])
print(f"Result: {json.dumps(r2, indent=2)[:500]}")

# Test 3: validate-only order (no execution)
print("\n=== Test 3: validate order ===")
r3 = _kraken_cli(["order", "buy", "PLAY/USD", "100", "--type", "limit",
                   "--price", "0.001", "--validate"])
print(f"Result: {json.dumps(r3, indent=2)[:500]}")

# Test 4: actual order attempt (same as test-fire would do)
# GUARDED: set APEX_TEST_REAL_ORDER=1 to enable real money execution
if asks and os.environ.get("APEX_TEST_REAL_ORDER") == "1":
    ask = float(asks[0][0])
    qty = 5.0 / ask
    limit_price = ask * 1.0005
    print(f"\n=== Test 4: real order attempt (${5} worth) ===")
    print(f"  ask={ask:.8f}  qty={qty:.8f}  limit={limit_price:.8f}")
    r4 = _kraken_cli(["order", "buy", "PLAY/USD", f"{qty:.8f}",
                       "--type", "limit", "--price", f"{limit_price:.8f}", "--yes"])
    print(f"Result: {json.dumps(r4, indent=2)[:500]}")
elif asks:
    print("\n=== Test 4: SKIPPED (set APEX_TEST_REAL_ORDER=1 to enable) ===")
