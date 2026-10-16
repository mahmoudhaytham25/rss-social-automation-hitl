#!/usr/bin/env python3
"""Normalize n8n workflow JSON exports in workflows/.

For every workflows/*.json file this script:
  - strips the top-level "meta" and "versionId" keys (instance-specific
    noise/fingerprint that churns diffs between exports),
  - sets "active" to false (exported workflows should not be active),
  - writes the file back as 2-space-indented JSON with a trailing newline.

Stdlib only. Idempotent: running it twice produces no further changes.

Usage:
    python3 scripts/normalize-exports.py
"""

import json
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"


def normalize(path: Path) -> bool:
    """Normalize a single workflow file. Returns True if it changed."""
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)

    data.pop("meta", None)
    data.pop("versionId", None)
    data["active"] = False

    normalized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if normalized != original:
        path.write_text(normalized, encoding="utf-8")
        return True
    return False


def main() -> None:
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        changed = normalize(path)
        print(f"{'normalized' if changed else 'unchanged '}: {path.name}")


if __name__ == "__main__":
    main()
