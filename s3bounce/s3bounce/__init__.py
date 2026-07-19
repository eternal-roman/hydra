"""s3bounce — daily bounce-leg continuation classifier + gate-adopted exits.

Standalone, stdlib-only packaging of the S3 algorithm promoted by the
HYDRA heartbeat research program (pre-registered bakeoffs; evidence
pointers in README.md). Public API re-exported here.
"""

from .candles import DailyBar, DailyBarSeries  # noqa: F401

__version__ = "0.1.0"
