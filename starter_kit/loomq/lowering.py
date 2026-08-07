"""Profile-driven lowering.

One rewrite loop for every backend: keep expanding gates the profile does not
support, using the rule table in :mod:`gates`, until only supported gates
remain. Vendors differ only by which set they declare supported.
"""

from typing import List

from .gates import DECOMPOSITIONS
from .ir import Circuit, Gate, Measure, Op
from .profiles import Profile

_MAX_PASSES = 8


class LoweringError(ValueError):
    """A gate cannot be expressed in the requested profile."""


def lower(circuit: Circuit, profile: Profile) -> Circuit:
    """Rewrite ``circuit`` until every gate is supported by ``profile``."""
    ops: List[Op] = list(circuit.ops)

    for _ in range(_MAX_PASSES):
        if all(op.name in profile.supported for op in ops if isinstance(op, Gate)):
            return circuit.copy_with_ops(ops)
        ops = _expand_once(ops, profile)

    unsupported = sorted(
        {op.name for op in ops if isinstance(op, Gate) and op.name not in profile.supported}
    )
    raise LoweringError(
        "lowering did not converge for profile %s; still unsupported: %s"
        % (profile.name, ", ".join(unsupported))
    )


def _expand_once(ops: List[Op], profile: Profile) -> List[Op]:
    expanded: List[Op] = []
    for op in ops:
        if isinstance(op, Measure) or op.name in profile.supported:
            expanded.append(op)
            continue
        rule = DECOMPOSITIONS.get(op.name)
        if rule is None:
            raise LoweringError(
                "no decomposition rule for gate %r required by profile %s"
                % (op.name, profile.name)
            )
        expanded.extend(rule(op.qubits, op.params))
    return expanded
