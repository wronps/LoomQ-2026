#!/usr/bin/env python3
"""End-to-end L1 self-check against the real vendor SDKs.

The public ``evaluator.py`` only covers Bell and GHZ-3, and both are symmetric
under bit reversal — they cannot fail on a bit-order bug. This runs the full
local circuit suite (including an asymmetric probe, QFT-4, Grover-3 and random
circuits) on every installed backend and scores each one against the ideal
distribution computed by :mod:`loomq.reference`.

    python3 starter_kit/tools/selfcheck_l1.py
    python3 starter_kit/tools/selfcheck_l1.py --target braket --shots 8192

Backends whose SDK is missing are reported as SKIP, not as failures.
"""

import argparse
import json
import sys
from pathlib import Path

STARTER_KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STARTER_KIT))

from loomq import qasm2, reference  # noqa: E402
from loomq.api import run  # noqa: E402
from loomq.backends.base import MissingBackendError  # noqa: E402
from loomq.profiles import TARGETS  # noqa: E402
from loomq.samples import suite  # noqa: E402

THRESHOLD = 0.97


def check(target: str, shots: int, verbose: bool):
    rows = []
    for label, source in suite().items():
        ideal = reference.probabilities(qasm2.parse(source))
        try:
            payload = run(source, target, shots)
        except MissingBackendError as exc:
            return None, str(exc)
        except Exception as exc:  # surface the real reason, do not mask it
            rows.append((label, 0.0, "%s: %s" % (type(exc).__name__, exc)))
            continue

        observed = {k: v / shots for k, v in payload["counts"].items()}
        fidelity = reference.hellinger_fidelity(observed, ideal)
        note = "" if fidelity >= THRESHOLD else "top ideal=%s observed=%s" % (
            _top(ideal), _top(observed)
        )
        rows.append((label, fidelity, note))
        if verbose:
            print("      counts:", json.dumps(payload["counts"], sort_keys=True))
            print("      meta:  ", json.dumps(payload["meta"], sort_keys=True, default=str))
    return rows, None


def _top(distribution, k=3):
    ranked = sorted(distribution.items(), key=lambda kv: -kv[1])[:k]
    return ", ".join("%s=%.3f" % (state, weight) for state, weight in ranked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=",".join(TARGETS))
    parser.add_argument("--shots", type=int, default=8192)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    failures = 0
    skipped = []
    for target in [t.strip() for t in args.target.split(",") if t.strip()]:
        print("== %s ==" % target)
        rows, missing = check(target, args.shots, args.verbose)
        if missing:
            print("   SKIP  %s" % missing)
            skipped.append(target)
            continue
        for label, fidelity, note in rows:
            status = "PASS" if fidelity >= THRESHOLD else "FAIL"
            failures += status == "FAIL"
            print("   %-4s  %-16s fidelity=%.4f  %s" % (status, label, fidelity, note))

    print()
    print("failures=%d  skipped=%s" % (failures, ",".join(skipped) or "none"))
    if len(skipped) > len(TARGETS) - 2:
        print("WARNING: fewer than two backends ran; the entry tier needs two.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
