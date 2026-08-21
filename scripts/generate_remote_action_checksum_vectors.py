"""Regenerate the cross-language RFC 8785 checksum fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from remote_actions.checksums import content_checksum, release_checksum  # noqa: E402


OUTPUT = ROOT / "tests" / "fixtures" / "remote_actions" / "checksum_vectors.json"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _action_id(index: int) -> str:
    value = index + 1
    encoded = ["0"] * 26
    for position in range(25, -1, -1):
        encoded[position] = CROCKFORD[value & 31]
        value >>= 5
    return f"act_{''.join(encoded)}"


def _content(index: int) -> dict:
    numeric_values = [0, -0.0, 1, -7, 1.5, 0.000001, 1e-7, 1e-20, 1e-21, 333333333.3333333]
    snapshot = {
        "index": index,
        "label": ["plain", "中文", "emoji-🚀", "line\nbreak", "quote-\""][index % 5],
        "numeric": numeric_values[index % len(numeric_values)],
        "nested": {
            "z": [index, True, None],
            "a": {"β": "beta", "𐀀": "astral", "€": "euro"},
        },
    }
    if index % 2:
        snapshot = dict(reversed(list(snapshot.items())))
    return {
        "executor_kind": "comment_campaign" if index % 2 else "browser_strategy",
        "definition_schema_version": "1.0",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "target_url": {"type": "string", "maxLength": 2048},
                "input_text": {"type": "string", "maxLength": 4096},
            },
            "additionalProperties": False,
        },
        "result_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "snapshot": snapshot,
        "execution_defaults": {
            "navigation_timeout_seconds": 10 + index,
            "retry": index % 3,
        },
    }


def main() -> None:
    vectors = []
    for index in range(50):
        content_input = _content(index)
        content_digest = content_checksum(content_input)
        release_input = {
            "action_id": _action_id(index),
            "revision": index + 1,
            "content_checksum": content_digest,
        }
        vectors.append(
            {
                "name": f"vector-{index + 1:02d}",
                "content_input": content_input,
                "content_checksum": content_digest,
                "release_input": release_input,
                "release_checksum": release_checksum(**release_input),
            }
        )
    OUTPUT.write_text(
        json.dumps({"vectors": vectors}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
