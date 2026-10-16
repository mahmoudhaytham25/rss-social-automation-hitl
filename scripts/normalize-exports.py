#!/usr/bin/env python3
"""Normalize n8n workflow JSON exports in workflows/.

For every workflows/*.json file this script:
  - strips the instance-specific/runtime-only top-level keys in
    STRIPPED_KEYS (instance fingerprints and diff churn — e.g. "meta",
    "versionId", and the extra fields newer n8n versions like 1.123.x add:
    "activeVersionId", "versionCounter", "isArchived", "shared",
    "staticData", "triggerCount", "createdAt", "updatedAt"),
  - sets "active" to false (exported workflows should not be active),
  - writes the file back as 2-space-indented JSON with a trailing newline.

Stdlib only. Idempotent: running it twice produces no further changes.

Usage:
    python3 scripts/normalize-exports.py
"""

import json
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"

# Top-level keys that are instance state or runtime bookkeeping, never
# workflow definition. Older exports carry meta/versionId; n8n 1.123.x adds
# the rest on export.
STRIPPED_KEYS = (
    "meta",
    "versionId",
    "activeVersionId",
    "versionCounter",
    "isArchived",
    "shared",
    "staticData",
    "triggerCount",
    "createdAt",
    "updatedAt",
)


def normalize(path: Path) -> bool:
    """Normalize a single workflow file. Returns True if it changed."""
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)

    for key in STRIPPED_KEYS:
        data.pop(key, None)
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
