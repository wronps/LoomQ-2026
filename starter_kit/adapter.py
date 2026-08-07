#!/usr/bin/env python3
"""LoomQ submission adapter contract v1.0.

L1 is implemented in the :mod:`loomq` package next to this file; this module is
only the graded surface. L2/L3 are not entered and stay unimplemented.
"""

from typing import Any, Dict, List, Tuple

try:  # imported as ``starter_kit.adapter``
    from . import loomq
except ImportError:  # imported as top-level ``adapter`` by evaluator.py
    import loomq


SUPPORTED_TARGETS = ("spinq", "originq", "braket")


def transpile(qasm_str: str, target: str) -> str:
    """Translate OpenQASM 2.0 into the target backend's native representation."""
    return loomq.transpile(qasm_str, target)


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute a circuit and return the unified result schema from the rules."""
    return loomq.run(qasm_str, target, shots)


def agent_chat(prompt: str) -> str:
    """Optional L2 entry point using the documented LOOMQ_LLM_* environment."""
    raise NotImplementedError("L2 is optional; implement agent_chat(prompt) to enter")


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Optional L3 entry point. Return quantum operations and RISC-V assembly."""
    raise NotImplementedError(
        "L3 is optional; implement compile_hybrid(hybrid_qasm_str) to enter"
    )
