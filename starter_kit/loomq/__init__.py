"""LoomQ universal middle layer (L1).

One parser, one IR, one lowering pass, three profile-driven emitters.
"""

from .api import compile_for, run, transpile
from .ir import Circuit, Gate, Measure
from .profiles import PROFILES, TARGETS

__all__ = [
    "Circuit",
    "Gate",
    "Measure",
    "PROFILES",
    "TARGETS",
    "compile_for",
    "run",
    "transpile",
]
