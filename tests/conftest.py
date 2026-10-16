"""Shared helpers and fixtures for validating the exported n8n workflows.

The JSON exports under ``workflows/`` are treated as data. Helpers here wrap
the n8n export shape (``nodes`` list, ``connections`` dict keyed by *source*
node name) so the tests stay readable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / "workflows"

WORKFLOW_FILES = (
    "main.json",
    "post-creation-flow.json",
    "bluesky-branch.json",
    "facebook-branch.json",
    "linkedin-branch.json",
    "error-handler.json",
)


@lru_cache(maxsize=None)
def load_workflow(filename: str) -> dict:
    """Parse one workflow export into a dict."""
    with (WORKFLOWS_DIR / filename).open(encoding="utf-8") as fh:
        return json.load(fh)


def load_all_workflows() -> dict:
    """Return ``{filename: parsed_workflow}`` for every exported file."""
    return {name: load_workflow(name) for name in WORKFLOW_FILES}


def workflow_by_id() -> dict:
    """Return ``{workflow_id: (filename, workflow_name)}`` across all exports."""
    return {
        workflow["id"]: (filename, workflow.get("name"))
        for filename, workflow in load_all_workflows().items()
    }


@pytest.fixture(scope="session")
def workflows() -> dict:
    """All parsed workflow exports, keyed by filename."""
    return load_all_workflows()


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------

def node_names(workflow: dict) -> set:
    return {node["name"] for node in workflow["nodes"]}


def find_nodes(workflow: dict, predicate) -> list:
    return [node for node in workflow["nodes"] if predicate(node)]


def node_by_name(workflow: dict, name: str) -> dict:
    for node in workflow["nodes"]:
        if node["name"] == name:
            return node
    raise KeyError(f"node {name!r} not found")


def iter_js_code(workflow: dict):
    """Yield ``(node_name, jsCode)`` for every node carrying a jsCode param."""
    for node in workflow["nodes"]:
        code = node.get("parameters", {}).get("jsCode")
        if isinstance(code, str):
            yield node["name"], code


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def iter_edges(workflow: dict):
    """Yield ``(source_name, connection_type, edge)`` for every wired edge.

    ``connections`` maps a *source* node name to
    ``{connection_type: [[edge, ...], ...]}``; each inner list is one output
    index and each edge looks like ``{"node": <target>, "type": ..., "index": n}``.
    """
    for source, outputs in workflow.get("connections", {}).items():
        for conn_type, output_arrays in outputs.items():
            for edge_list in output_arrays:
                for edge in edge_list:
                    yield source, conn_type, edge


def upstream_node_names(workflow: dict, target_name: str) -> set:
    """Direct upstream sources of ``target_name`` (any connection type)."""
    return {
        source
        for source, _conn_type, edge in iter_edges(workflow)
        if edge.get("node") == target_name
    }


def ancestors(workflow: dict, node_name: str) -> set:
    """All transitive upstream node names (cycle-safe)."""
    seen: set = set()
    stack = [node_name]
    while stack:
        current = stack.pop()
        for parent in upstream_node_names(workflow, current):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def successors(workflow: dict, node_name: str) -> list:
    """``[(output_index, target_name), ...]`` for a node's outgoing main edges.

    ``connections[source]["main"]`` is a list of edge lists; the position in
    that outer list is the node's output index (``[[]]`` = unconnected
    output).
    """
    outputs = (
        workflow.get("connections", {}).get(node_name, {}).get("main", [])
    )
    return [
        (output_index, edge["node"])
        for output_index, edge_list in enumerate(outputs)
        for edge in edge_list
    ]


def descendants(workflow: dict, node_name: str) -> set:
    """All transitive downstream node names via main edges (cycle-safe —
    the approval re-ask edges loop back onto earlier nodes)."""
    seen: set = set()
    stack = [node_name]
    while stack:
        current = stack.pop()
        for _output_index, target in successors(workflow, current):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    seen.discard(node_name)
    return seen


def merge_resolve_clash(node: dict):
    """Return the merge node's resolveClash option (e.g. 'addSuffix')."""
    return (
        node.get("parameters", {})
        .get("options", {})
        .get("clashHandling", {})
        .get("values", {})
        .get("resolveClash")
    )


# ---------------------------------------------------------------------------
# Generic JSON walkers (secrets scan, expression checks)
# ---------------------------------------------------------------------------

def iter_string_values(obj, path: str = "$"):
    """Yield ``(path, string)`` for every string in a JSON structure."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_string_values(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_string_values(value, f"{path}[{index}]")


def iter_key_value_pairs(obj, path: str = "$"):
    """Yield ``(path, key, value)`` for every dict entry in a JSON structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield path, key, value
            yield from iter_key_value_pairs(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_key_value_pairs(value, f"{path}[{index}]")
