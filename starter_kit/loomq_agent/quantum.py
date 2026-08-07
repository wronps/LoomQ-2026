"""The one place L2 reaches into the L1 middle layer.

The agent verifies its own output with the same parser and the same reference
semantics the transpiler is built on, so a circuit that passes here is a
circuit L1 can actually transpile and run.
"""

try:  # imported as ``starter_kit.loomq_agent.quantum``
    from ..loomq import qasm2, reference
    from ..loomq.gates import WHITELIST
except ImportError:  # imported with ``starter_kit`` itself on sys.path
    from loomq import qasm2, reference
    from loomq.gates import WHITELIST

QasmError = qasm2.QasmError

__all__ = ["QasmError", "WHITELIST", "qasm2", "reference"]
