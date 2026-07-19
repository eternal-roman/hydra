"""s3bounce — daily bounce-leg continuation classifier + gate-adopted exits.

Standalone, stdlib-only packaging of the S3 algorithm promoted by the
HYDRA heartbeat research program (pre-registered bakeoffs; evidence
pointers in README.md). Public API re-exported here.
"""

from .candles import DailyBar, DailyBarSeries                       # noqa: F401
from .setups import Setup, causal_setups, entry_index               # noqa: F401
from .features import FEATURES, compute_features, fresh_low_days    # noqa: F401
from .model import (Artifact, ArtifactError, AssetModel,            # noqa: F401
                    gate, load_artifact, score)
from .exits import ExitDecision, OpenPosition, evaluate             # noqa: F401
from .strategy import S3Signal, S3Strategy                          # noqa: F401
from .shadow import ShadowLedger                                    # noqa: F401

__version__ = "0.1.0"
