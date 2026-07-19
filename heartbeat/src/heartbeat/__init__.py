"""heartbeat — recursive Bayesian order-flow posterior (P(up) indicator).

Public surface (publishable package):
    run_dataset, load_trades, dataset_requirements,
    IndicatorResult, Trade, Side,
    MissingDatasetError, InvalidDatasetError,
    call_tool, TOOL_SCHEMAS

No order path. Agents consume indicator JSON only.
"""

from .agent_tools import TOOL_SCHEMAS, call_tool
from .dataset import dataset_requirements, load_trades
from .errors import InvalidDatasetError, MissingDatasetError
from .feed.tape import Side, Trade
from .indicator import HeartbeatSession, IndicatorResult, run_dataset

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "run_dataset",
    "load_trades",
    "dataset_requirements",
    "MissingDatasetError",
    "InvalidDatasetError",
    "IndicatorResult",
    "HeartbeatSession",
    "Trade",
    "Side",
    "call_tool",
    "TOOL_SCHEMAS",
]
