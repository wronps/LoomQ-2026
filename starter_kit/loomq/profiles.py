"""Target profiles: the whole per-backend knowledge of this transpiler.

A profile is *data*, not code. Adding a fourth vendor means adding a row here
plus a thin runner — the parser, the lowering pass and the emitters do not
change. That is the difference between a middle layer and three ``if target ==``
branches.

Each vendor has two profiles:

``<vendor>.ir``
    What ``transpile()`` returns. Must satisfy ``target_ir_contract.md``,
    because the organizers parse and simulate this string themselves.

``<vendor>.native``
    What the local SDK actually accepts. Empirically probed, not assumed —
    e.g. Braket's ``LocalSimulator`` has no ``sdg``/``tdg``/``cx`` and cannot
    open ``stdgates.inc`` from disk, while pyQPanda's OriginIR parser rejects
    ``SDAG``/``TDAG``/``CU1`` even though the contract accepts them.

Both profiles are fed through the *same* lowering pass, so the two strings are
always two renderings of one circuit.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Mapping

TARGETS = ("spinq", "originq", "braket")

#: Canonical backend ids from ``backend_capabilities.json``.
SIMULATOR_IDS = {
    "spinq": "spinq_taurus_simulator",
    "originq": "originq_local_simulator",
    "braket": "braket_local_simulator",
}


@dataclass(frozen=True)
class Profile:
    """How one dialect of one target renders and constrains a circuit."""

    name: str
    syntax: str  # "qasm2" | "qasm3" | "originir"
    supported: FrozenSet[str]
    aliases: Mapping[str, str] = field(default_factory=dict)
    include: str = ""

    def emitted_name(self, canonical: str) -> str:
        return self.aliases.get(canonical, canonical)


_WHITELIST_12 = frozenset(
    ["h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap", "ccx"]
)


PROFILES: Dict[str, Profile] = {
    # --- SpinQ ---------------------------------------------------------------
    # spinqit's QASM2 compiler accepts all 12 whitelist gates natively, so the
    # contract IR and the runtime string are identical: no lowering at all.
    "spinq.ir": Profile(
        name="spinq.ir",
        syntax="qasm2",
        supported=_WHITELIST_12,
        include='include "qelib1.inc";',
    ),
    "spinq.native": Profile(
        name="spinq.native",
        syntax="qasm2",
        supported=_WHITELIST_12,
        include='include "qelib1.inc";',
    ),
    # --- OriginQ -------------------------------------------------------------
    # Contract-allowed OriginIR names: H X S SDAG T TDAG RY RZ CNOT CU1/CR SWAP
    # TOFFOLI/CCX. pyQPanda's own parser is narrower: SDAG/TDAG/CU1/CCX fail, so
    # the native profile drops sdg/tdg (lowered to U1) and renames cu1 -> CR.
    "originq.ir": Profile(
        name="originq.ir",
        syntax="originir",
        supported=_WHITELIST_12,
        aliases={
            "h": "H",
            "x": "X",
            "s": "S",
            "sdg": "SDAG",
            "t": "T",
            "tdg": "TDAG",
            "rz": "RZ",
            "ry": "RY",
            "cx": "CNOT",
            "cu1": "CU1",
            "swap": "SWAP",
            "ccx": "TOFFOLI",
        },
    ),
    "originq.native": Profile(
        name="originq.native",
        syntax="originir",
        supported=frozenset(
            ["h", "x", "s", "t", "rz", "ry", "u1", "cx", "cu1", "swap", "ccx"]
        ),
        aliases={
            "h": "H",
            "x": "X",
            "s": "S",
            "t": "T",
            "rz": "RZ",
            "ry": "RY",
            "u1": "U1",
            "cx": "CNOT",
            "cu1": "CR",
            "swap": "SWAP",
            "ccx": "TOFFOLI",
        },
    ),
    # --- Braket --------------------------------------------------------------
    # Contract IR is stdgates.inc-flavoured OpenQASM 3. The LocalSimulator has
    # no include-file resolution and uses AWS gate names, hence a second render.
    "braket.ir": Profile(
        name="braket.ir",
        syntax="qasm3",
        supported=_WHITELIST_12,
        aliases={"cu1": "cp"},
        include='include "stdgates.inc";',
    ),
    "braket.native": Profile(
        name="braket.native",
        syntax="qasm3",
        supported=frozenset(
            ["h", "x", "s", "t", "rz", "ry", "u1", "cx", "cu1", "swap", "ccx"]
        ),
        aliases={
            "u1": "phaseshift",
            "cx": "cnot",
            "cu1": "cphaseshift",
            "ccx": "ccnot",
        },
        include="",
    ),
}


def profile_for(target: str, dialect: str) -> Profile:
    key = "%s.%s" % (target, dialect)
    try:
        return PROFILES[key]
    except KeyError:
        raise ValueError(
            "unknown profile %r; targets are %s" % (key, ", ".join(TARGETS))
        ) from None


def normalize_target(target: str) -> str:
    if not isinstance(target, str):
        raise ValueError("target must be a string, got %r" % type(target).__name__)
    key = target.strip().lower()
    if key not in TARGETS:
        raise ValueError(
            "unknown target %r; expected one of %s" % (target, ", ".join(TARGETS))
        )
    return key
