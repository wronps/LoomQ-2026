"""Backend runner interface.

A runner receives the *already lowered* circuit plus the native-dialect text and
returns competition-ordered counts. It never parses QASM and never decides which
gates to decompose — that all happened in the shared pipeline.
"""

from typing import Any, Dict, NamedTuple, Protocol

from ..ir import Circuit


class Execution(NamedTuple):
    counts: Dict[str, int]
    job_id: str
    backend_id: str
    extra: Dict[str, Any] = {}


class MissingBackendError(RuntimeError):
    """The vendor SDK is not installed.

    Raised instead of returning fabricated numbers: a result carrying
    ``is_mock`` or an untraceable ``job_id`` scores zero, so a loud failure is
    strictly better than a quiet fake.
    """


class Runner(Protocol):
    def execute(self, circuit: Circuit, native_text: str, shots: int) -> Execution: ...


def require(module_name: str, pip_name: str):
    """Import a vendor SDK or explain exactly how to install it."""
    try:
        return __import__(module_name)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingBackendError(
            "%s is not installed; add `%s` to requirements.txt (original error: %s)"
            % (module_name, pip_name, exc)
        ) from exc
