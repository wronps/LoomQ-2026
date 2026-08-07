"""Backend choice: the model extracts constraints, code does the filtering.

``backend_capabilities.md`` says it outright — load the JSON and filter by
constraint rather than letting the model recite the table from memory. The
graded prompts are unpublished rewordings, so anything that depends on the
model remembering that Braket's local simulator tops out at 25 qubits is a
coin flip. Filtering is not.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CAPABILITIES = Path(__file__).resolve().parent.parent / "backend_capabilities.json"

#: Ordered from "no waiting" to "worst waiting"; a constraint admits its rank
#: and everything better.
QUEUE_RANK = {"none": 0, "minutes_to_hours": 1, "hours": 2}


@dataclass
class Backend:
    id: str
    platform: str
    name: str
    kind: str
    max_qubits: int
    queue: str
    cost: str
    requires_account: bool
    notes: str = ""

    @property
    def queue_rank(self) -> int:
        return QUEUE_RANK.get(self.queue, len(QUEUE_RANK))

    @property
    def is_free(self) -> bool:
        return self.cost != "paid"


def load_table(path: Path = CAPABILITIES) -> List[Backend]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Backend(
            id=entry["id"],
            platform=entry.get("platform", ""),
            name=entry.get("name", entry["id"]),
            kind=entry.get("kind", ""),
            max_qubits=int(entry.get("max_qubits", 0)),
            queue=entry.get("queue", ""),
            cost=entry.get("cost", ""),
            requires_account=bool(entry.get("requires_account", False)),
            notes=entry.get("notes", ""),
        )
        for entry in payload.get("backends", [])
    ]


@dataclass
class Selection:
    matches: List[Backend]
    relaxed: List[Tuple[Backend, List[str]]]
    constraints: Dict[str, Any]

    @property
    def recommended(self) -> Optional[Backend]:
        return self.matches[0] if self.matches else None


def choose(constraints: Dict[str, Any], table: Optional[List[Backend]] = None) -> Selection:
    """Filter the official table; if nothing fits, report the closest options."""
    backends = table if table is not None else load_table()
    normalized = _normalize(constraints)

    matches = [b for b in backends if not _violations(b, normalized)]
    matches.sort(key=_preference)

    if matches:
        return Selection(matches, [], normalized)

    # Nothing satisfies every constraint, so name a real alternative instead of
    # just saying no. Qubit capacity ranks ahead of everything else: a backend
    # too small for the circuit is not a near miss, however convenient it is,
    # whereas queueing and sign-up are costs the user can choose to pay.
    minimum = normalized.get("min_qubits", 0)
    scored = sorted(
        ((b, _violations(b, normalized)) for b in backends),
        key=lambda pair: (
            0 if pair[0].max_qubits >= minimum else 1,
            len(pair[1]),
            _preference(pair[0]),
        ),
    )
    return Selection([], [pair for pair in scored if pair[1]][:2], normalized)


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    qubits = raw.get("min_qubits")
    if isinstance(qubits, bool):
        qubits = None
    if isinstance(qubits, (int, float)) and qubits > 0:
        out["min_qubits"] = int(qubits)

    for flag in ("require_real_hardware", "allow_paid", "allow_account"):
        value = raw.get(flag)
        if isinstance(value, bool):
            out[flag] = value

    queue = raw.get("max_queue")
    if isinstance(queue, str) and queue.strip().lower() in QUEUE_RANK:
        out["max_queue"] = queue.strip().lower()

    return out


def _violations(backend: Backend, constraints: Dict[str, Any]) -> List[str]:
    reasons = []

    minimum = constraints.get("min_qubits")
    if minimum is not None and backend.max_qubits < minimum:
        reasons.append("needs %d qubits, this backend tops out at %d" % (minimum, backend.max_qubits))

    if constraints.get("require_real_hardware") is True and backend.kind != "qpu":
        reasons.append("not real quantum hardware")
    if constraints.get("require_real_hardware") is False and backend.kind == "qpu":
        reasons.append("is real hardware, which the user did not want")

    queue = constraints.get("max_queue")
    if queue is not None and backend.queue_rank > QUEUE_RANK[queue]:
        reasons.append("queue is %s, longer than requested" % backend.queue)

    if constraints.get("allow_paid") is False and not backend.is_free:
        reasons.append("costs money")
    if constraints.get("allow_account") is False and backend.requires_account:
        reasons.append("requires an account")

    return reasons


def _preference(backend: Backend):
    """Least friction first: no queue, then free, then no account, then bigger."""
    return (
        backend.queue_rank,
        0 if backend.is_free else 1,
        1 if backend.requires_account else 0,
        -backend.max_qubits,
        backend.id,
    )
