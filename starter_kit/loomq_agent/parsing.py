"""Pulling structured data out of free-form model replies.

Models wrap JSON in prose, fence it, or emit smart quotes. Rather than tighten
the prompt until it never happens, extract defensively: find the outermost
balanced object, and fall back to a re-ask only if that fails.
"""

import json
import re
from typing import Any, Dict, Optional

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)```", re.DOTALL)
_QASM = re.compile(r"OPENQASM\s+2\.0\s*;.*", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort recovery of one JSON object from a model reply."""
    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _json_candidates(text: str):
    for fenced in _FENCE.findall(text):
        yield fenced.strip()
    yield text.strip()
    balanced = _balanced_object(text)
    if balanced:
        yield balanced


def _balanced_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_qasm(text: str) -> Optional[str]:
    """Recover an OpenQASM 2.0 program, preferring a fenced block."""
    for fenced in _FENCE.findall(text):
        match = _QASM.search(fenced)
        if match:
            return _tidy(match.group(0))

    match = _QASM.search(text)
    if not match:
        return None
    # Outside a fence, stop at the first line that opens one.
    body = match.group(0)
    fence_at = body.find("```")
    if fence_at >= 0:
        body = body[:fence_at]
    return _tidy(body)


def _tidy(qasm: str) -> str:
    lines = [line.rstrip() for line in qasm.strip().splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"
