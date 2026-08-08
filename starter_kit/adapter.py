#!/usr/bin/env python3
"""LoomQ submission adapter contract v1.0.

L1 lives in :mod:`loomq`, L2 in :mod:`loomq_agent` and L3 in
:mod:`loomq_hybrid`, all next to this file; this module is only the graded
surface.
"""

from typing import Any, Dict, List, Tuple

try:  # imported as ``starter_kit.adapter``
    from . import loomq, loomq_agent, loomq_hybrid
except ImportError:  # imported as top-level ``adapter`` by evaluator.py
    import loomq
    import loomq_agent
    import loomq_hybrid


SUPPORTED_TARGETS = ("spinq", "originq", "braket")


def transpile(qasm_str: str, target: str) -> str:
    """Translate OpenQASM 2.0 into the target backend's native representation."""
    return loomq.transpile(qasm_str, target)


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute a circuit and return the unified result schema from the rules."""
    return loomq.run(qasm_str, target, shots)


def agent_chat(prompt: str) -> str:
    """Optional L2 entry point using the documented LOOMQ_LLM_* environment."""
    return loomq_agent.agent_chat(prompt)


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Optional L3 entry point. Return quantum operations and RISC-V assembly."""
    return loomq_hybrid.compile_hybrid(hybrid_qasm_str)
