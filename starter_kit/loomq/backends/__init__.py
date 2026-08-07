"""Backend runner registry — one row per target, resolved by name."""

from typing import Callable, Dict

from . import braket, originq, spinq
from .base import Execution, MissingBackendError

RUNNERS: Dict[str, Callable[..., Execution]] = {
    "spinq": spinq.execute,
    "originq": originq.execute,
    "braket": braket.execute,
}

__all__ = ["RUNNERS", "Execution", "MissingBackendError"]
