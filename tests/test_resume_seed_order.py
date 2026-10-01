"""Resume must reconcile stale working orders BEFORE seeding balances.

Regression: shutdown cancel-all (or the dead man's switch) releases a resting
BUY's hold into the free pool. Seeding first kept that engine's post-buy cash
as "locked" and split the rest across flat siblings; the reconcile then
restored the pre-trade cash, so the engines held the BUY's cost twice — more
quote than the exchange — and the inflated sibling peaks armed their 15%
breakers on the next restart with no loss at all.
"""
import inspect
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hydra_agent import HydraAgent
from hydra_engine import HydraEngine, CrossPairCoordinator, Strategy
from hydra_kraken_cli import KrakenCLI
from hydra_streams import ExecutionStream

PAIRS = ["BTC/USD", "ETH/USD", "ZEC/USD"]
PRICES = {"BTC/USD": 60000.0, "ETH/USD": 3000.0, "ZEC/USD": 40.0}


def _engine(pair, balance):
    eng = HydraEngine(initial_balance=balance, asset=pair, candle_interval=60)
    for i in range(60):
        px = PRICES[pair]
        eng.ingest_candle({"open": px, "high": px * 1.001, "low": px * 0.999,
                           "close": px, "volume": 1.0,
                           "timestamp": 1_700_000_000.0 + 3600 * i})
    return eng


def _agent_with_resting_buy():
    agent = object.__new__(HydraAgent)
    agent.pairs = list(PAIRS)
    agent.paper = False
    agent.demo = False
    agent.triangle = None
    agent.order_journal = []
    agent._books_dirty = False
    agent._cancel_sent_ids = set()
    agent.engines = {p: _engine(p, 300.0) for p in PAIRS}
    agent.coordinator = CrossPairCoordinator(list(PAIRS))
    agent.execution_stream = ExecutionStream(paper=False)
    agent.balance_stream = None
    agent._cached_balance = {"USD": 900.0}
    agent._cached_free_balance = {"USD": 900.0}
    agent.initial_balance = 900.0
    agent._constructor_balance_split = 300.0
    btc = agent.engines["BTC/USD"]
    pre = btc.snapshot_position()
    btc._apply_buy_fill(0.0045, 60000.0, "optimistic", Strategy.MOMENTUM, 0.8)
    agent.order_journal.append({
        "placed_at": "2026-09-30T23:59:00+00:00", "pair": "BTC/USD", "side": "BUY",
        "intent": {"amount": 0.0045, "limit_price": 60000.0, "paper": False},
        "order_ref": {"order_userref": 3, "order_id": "OBUY"},
        "pre_trade_snapshot": pre,
        "lifecycle": {"state": "PLACED", "vol_exec": 0.0, "fee_quote": 0.0,
                      "exec_ids": []},
    })
    return agent


def test_reconcile_then_seed_matches_the_exchange_pool():
    agent = _agent_with_resting_buy()
    cancelled = {"OBUY": {"status": "canceled", "vol_exec": "0", "price": "0", "fee": "0"}}
    with mock.patch("hydra_agent.time.sleep"), \
            mock.patch.object(KrakenCLI, "query_orders", return_value=cancelled), \
            mock.patch.object(HydraAgent, "_get_asset_prices", return_value={}):
        agent._reconcile_stale_placed()
        agent._set_engine_balances(per_pair_usd=300.0)
    cash = {p: e.balance for p, e in agent.engines.items()}
    assert abs(sum(cash.values()) - 900.0) < 1e-6, cash
    assert all(e.position.size == 0.0 for e in agent.engines.values())


def test_run_reconciles_before_seeding_balances():
    src = inspect.getsource(HydraAgent.run)
    reconcile_at = src.index("self._reconcile_stale_placed()")
    seed_at = src.index("self._set_engine_balances(")
    assert reconcile_at < seed_at
