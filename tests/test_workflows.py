"""Structural regression tests for the exported n8n workflows.

The JSON exports under ``workflows/`` are treated as data. These tests assert
the wiring and expression invariants behind recent bugfixes:

* merge ``resolveClash: addSuffix`` field references (facebook branch),
* the ``Final`` node having a single upstream input (post-creation flow),
* the regeneration-attempt cap (post-creation flow),
* ``JSON.stringify`` escaping instead of manual backslash escaping
  (bluesky / facebook / linkedin branches),
* non-fatal ``onError`` on the file-read node (post-creation flow),
* no plaintext secrets in any export.

Run with::

    pip install -r requirements-dev.txt
    python -m pytest tests/
"""

import json
import re

import pytest

from conftest import (
    WORKFLOW_FILES,
    ancestors,
    find_nodes,
    iter_edges,
    iter_js_code,
    iter_key_value_pairs,
    iter_string_values,
    load_all_workflows,
    load_workflow,
    merge_resolve_clash,
    node_names,
    upstream_node_names,
)

MAIN = "main.json"
POST_CREATION = "post-creation-flow.json"
BLUESKY = "bluesky-branch.json"
FACEBOOK = "facebook-branch.json"
LINKEDIN = "linkedin-branch.json"


# ---------------------------------------------------------------------------
# 1. Every export parses and has the basic n8n shape
# ---------------------------------------------------------------------------

class TestWorkflowShape:
    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_parses_and_has_nodes_and_connections(self, filename):
        workflow = load_workflow(filename)
        assert isinstance(workflow["nodes"], list), f"{filename}: 'nodes' missing"
        assert workflow["nodes"], f"{filename}: 'nodes' is empty"
        assert isinstance(workflow["connections"], dict), (
            f"{filename}: 'connections' missing"
        )

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_nodes_have_name_and_type(self, filename):
        for node in load_workflow(filename)["nodes"]:
            assert node.get("name"), f"{filename}: node missing 'name': {node}"
            assert node.get("type"), f"{filename}: node missing 'type': {node}"

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_node_names_are_unique(self, filename):
        """n8n keys ``connections`` by node name; duplicates corrupt wiring."""
        names = [node["name"] for node in load_workflow(filename)["nodes"]]
        assert len(names) == len(set(names)), (
            f"{filename}: duplicate node names: "
            f"{sorted(n for n in set(names) if names.count(n) > 1)}"
        )

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_has_workflow_id_and_name(self, filename):
        workflow = load_workflow(filename)
        assert workflow.get("id"), f"{filename}: missing top-level 'id'"
        assert workflow.get("name"), f"{filename}: missing top-level 'name'"


# ---------------------------------------------------------------------------
# 2. No dangling connection edges
# ---------------------------------------------------------------------------

class TestConnectionIntegrity:
    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_edge_targets_exist(self, filename):
        workflow = load_workflow(filename)
        names = node_names(workflow)
        for source, conn_type, edge in iter_edges(workflow):
            assert edge.get("node") in names, (
                f"{filename}: edge {source} -> {edge.get('node')!r} "
                f"({conn_type}) targets a node that does not exist"
            )
            assert edge.get("type"), (
                f"{filename}: edge {source} -> {edge.get('node')!r} "
                "is missing its 'type'"
            )

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_edge_sources_exist(self, filename):
        """A ``connections`` key that is not a node name is dead wiring."""
        workflow = load_workflow(filename)
        names = node_names(workflow)
        for source in workflow["connections"]:
            assert source in names, (
                f"{filename}: connections key {source!r} has no matching node"
            )


# ---------------------------------------------------------------------------
# 3. executeWorkflow nodes call exports that exist in the repo
# ---------------------------------------------------------------------------

class TestSubWorkflowReferences:
    def test_execute_workflow_targets_are_exported(self):
        """main.json calls sub-workflows by workflowId value; every referenced
        id must match the top-level ``id`` of one of the exported files."""
        main = load_workflow(MAIN)
        id_to_name = {
            wf["id"]: wf.get("name") for wf in load_all_workflows().values()
        }

        callers = find_nodes(
            main, lambda n: n["type"].endswith("executeWorkflow")
        )
        assert callers, "expected at least one executeWorkflow node in main.json"

        for node in callers:
            ref = node["parameters"].get("workflowId")
            value = ref.get("value") if isinstance(ref, dict) else ref
            assert value in id_to_name, (
                f"executeWorkflow node {node['name']!r} calls workflow id "
                f"{value!r}, which is not among the exported ids "
                f"{sorted(id_to_name)}"
            )
            # The cached display name should match the export it points at,
            # otherwise the node was re-pointed without re-selecting it.
            cached = ref.get("cachedResultName") if isinstance(ref, dict) else None
            if cached:
                assert cached == id_to_name[value], (
                    f"{node['name']!r}: cachedResultName {cached!r} does not "
                    f"match export name {id_to_name[value]!r} for id {value!r}"
                )


# ---------------------------------------------------------------------------
# 4. Regression guards for the recent bugfixes
# ---------------------------------------------------------------------------

class TestFacebookMergeSuffix:
    """The image path merges the photo-upload result with the incoming item via
    a ``resolveClash: addSuffix`` merge, so downstream fields are ``id_1`` /
    ``id_2`` / ``facebook_2`` — never bare ``id``/``facebook``. The non-image
    path bypasses that merge and must keep using bare ``$json.id``."""

    def _fb_post_nodes(self):
        workflow = load_workflow(FACEBOOK)
        return workflow, find_nodes(
            workflow,
            lambda n: n["type"].endswith("httpRequest")
            and "graph.facebook.com" in n["parameters"].get("url", ""),
        )

    def _suffix_merges(self, workflow):
        return {
            node["name"]
            for node in find_nodes(
                workflow,
                lambda n: n["type"].endswith("merge")
                and merge_resolve_clash(n) == "addSuffix",
            )
        }

    def test_add_suffix_merge_exists(self):
        workflow = load_workflow(FACEBOOK)
        assert self._suffix_merges(workflow), (
            "expected a merge node with resolveClash=addSuffix"
        )

    def test_downstream_of_suffix_merge_uses_suffixed_fields(self):
        workflow, posts = self._fb_post_nodes()
        suffix_merges = self._suffix_merges(workflow)
        downstream = [
            n for n in posts if ancestors(workflow, n["name"]) & suffix_merges
        ]
        assert downstream, "no facebook post node sits below the addSuffix merge"
        for node in downstream:
            url = node["parameters"]["url"]
            assert re.search(r"\$json\.id_\d+", url), (
                f"{node['name']!r}: url {url!r} must use a suffixed page id "
                "(e.g. $json.id_2) — it is downstream of the addSuffix merge"
            )
            params_text = json.dumps(node["parameters"])
            assert not re.search(r"\$json\.(id|facebook)\b", params_text), (
                f"{node['name']!r}: bare $json.id/$json.facebook reference "
                "downstream of the addSuffix merge reads the wrong field"
            )
            # Known wiring: id_1 = uploaded photo id, id_2 = page id,
            # facebook_2 = post text.
            assert "$json.id_1" in params_text
            assert "$json.facebook_2" in params_text

    def test_non_image_path_uses_bare_id(self):
        """Post Creation FB (If false-branch) and the photo upload sit upstream
        of the addSuffix merge; bare $json.id is correct there."""
        workflow, posts = self._fb_post_nodes()
        suffix_merges = self._suffix_merges(workflow)
        upstream = [
            n for n in posts if not ancestors(workflow, n["name"]) & suffix_merges
        ]
        assert upstream, "expected facebook nodes outside the addSuffix merge"
        for node in upstream:
            params_text = json.dumps(node["parameters"])
            assert not re.search(r"\$json\.\w+_\d+\b", params_text), (
                f"{node['name']!r}: suffixed field reference but no addSuffix "
                "merge upstream"
            )
        # At least one of them actually posts using the bare page id.
        assert any(
            re.search(r"\$json\.id\b", n["parameters"]["url"]) for n in upstream
        ), "no facebook post node uses the bare $json.id page id"


class TestPostCreationFlow:
    def test_final_node_has_exactly_one_upstream(self):
        """Regression: the file-write branch was once also wired into Final,
        producing duplicate/empty outputs. Final must be fed only by the
        'Output data' set node."""
        incoming = upstream_node_names(load_workflow(POST_CREATION), "Final")
        assert incoming == {"Output data"}, (
            f"'Final' should have exactly one upstream ('Output data'), "
            f"got {sorted(incoming)}"
        )

    def test_regeneration_attempt_cap_exists(self):
        """An If node must cap regeneration at attempts >= 3 so the agent
        loop cannot spin forever on over-limit output."""
        workflow = load_workflow(POST_CREATION)
        if_nodes = find_nodes(workflow, lambda n: n["type"].endswith(".if"))

        attempt_conditions = [
            (node["name"], cond)
            for node in if_nodes
            for cond in node["parameters"]
            .get("conditions", {})
            .get("conditions", [])
            if "attempts" in str(cond.get("leftValue", ""))
        ]
        assert attempt_conditions, "no If condition references 'attempts'"
        assert any(
            cond.get("operator", {}).get("operation") == "gte"
            and cond.get("rightValue") == 3
            for _name, cond in attempt_conditions
        ), (
            f"expected an 'attempts >= 3' cap; found "
            f"{[(n, c.get('operator', {}).get('operation'), c.get('rightValue')) for n, c in attempt_conditions]}"
        )

    def test_file_read_node_tolerates_missing_file(self):
        """The xlsx may not exist on first run; the read node must continue
        instead of failing the whole sub-workflow."""
        workflow = load_workflow(POST_CREATION)
        readers = find_nodes(
            workflow,
            lambda n: n["type"].endswith("readWriteFile")
            and n["parameters"].get("operation", "read") != "write",
        )
        assert readers, "no file-read node found"
        for node in readers:
            assert node.get("onError") in (
                "continueErrorOutput",
                "continueRegularOutput",
            ), f"{node['name']!r}: onError is {node.get('onError')!r}"


class TestBlueskyEscaping:
    """Post text, title and facets are interpolated into a JSON body, so they
    must be escaped via JSON.stringify — never via hand-rolled backslashes."""

    def test_code_nodes_use_json_stringify(self):
        codes = list(iter_js_code(load_workflow(BLUESKY)))
        assert codes, "no code nodes found in bluesky branch"
        for name, code in codes:
            assert "JSON.stringify" in code, (
                f"{name!r}: jsCode does not use JSON.stringify — post/title "
                "may be manually escaped again"
            )

    def test_create_record_bodies_serialize_facets_via_json_stringify(self):
        workflow = load_workflow(BLUESKY)
        posts = find_nodes(
            workflow,
            lambda n: n["type"].endswith("httpRequest")
            and "createRecord" in n["parameters"].get("url", ""),
        )
        assert posts, "no createRecord node found"
        for node in posts:
            body = node["parameters"].get("jsonBody", "")
            assert "JSON.stringify" in body, (
                f"{node['name']!r}: jsonBody embeds facets without "
                "JSON.stringify"
            )

    def test_facet_math_uses_utf8_byte_helper(self):
        """Bluesky facet indexes are UTF-8 *byte* offsets; dropping the helper
        breaks facets on multibyte characters (emoji, accents)."""
        facet_codes = [
            code
            for _name, code in iter_js_code(load_workflow(BLUESKY))
            if "facets" in code or "byteStart" in code
        ]
        assert facet_codes, "no facet-calculation code node found"
        for code in facet_codes:
            assert "utf8Length" in code
            assert "getUtf8ByteIndex" in code
            assert "charCodeAt" in code  # surrogate-pair aware counting


class TestNoManualJsonEscaping:
    """Regression: jsCode once did ``.replace(/\\\\/g, ...)``-style manual JSON
    escaping, which double-escaped content. That pattern must stay gone."""

    BANNED = re.compile(r"replace\(/\\\\/g")

    @pytest.mark.parametrize("filename", [FACEBOOK, LINKEDIN])
    def test_no_manual_escaping_in_js_code(self, filename):
        for name, code in iter_js_code(load_workflow(filename)):
            assert not self.BANNED.search(code), (
                f"{filename} / {name!r}: manual backslash escaping "
                "(replace(/\\\\/g)) found — use JSON.stringify or "
                "multipart fields instead"
            )


# ---------------------------------------------------------------------------
# 5. No plaintext secrets
# ---------------------------------------------------------------------------

# Dict keys whose values must be expressions/placeholders, not literals.
# Normalized by lowercasing and stripping non-letters, so 'passwordLabel'
# (-> 'passwordlabel') and credential 'id' fields do not match.
SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "privatekey",
    "authorization",
    "bearer",
    "sessionid",
}

# Literal secret shapes that are suspicious no matter which key holds them.
SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                    # OpenAI/Stripe style
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),           # Slack tokens
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),                 # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_-]{10,}"),               # Google OAuth token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),       # literal bearer token
]

_PLACEHOLDER = re.compile(
    r"^\s*(?:"
    r"=|"                       # n8n expressions always start with '='
    r"\{\{.*\}\}|"              # inline {{ }} expression
    r"<[^>]*>|"                 # <YOUR_TOKEN>
    r"your[-_ ].*|"             # your-token-here
    r"(?:change-?me|placeholder|example|dummy|redacted|xxx+|\*+|todo)\b.*"
    r")\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _is_sensitive_key(key) -> bool:
    return re.sub(r"[^a-z]", "", str(key).lower()) in SENSITIVE_KEYS


def _is_allowed_secret_value(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True  # non-strings and empty values carry no secret
    if "{{" in value or value.lstrip().startswith("="):
        return True  # n8n expression / credential interpolation
    return bool(_PLACEHOLDER.match(value))


class TestNoPlaintextSecrets:
    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_no_secret_shaped_values(self, filename):
        workflow = load_workflow(filename)
        hits = [
            f"{path}: {value[:40]!r}"
            for path, value in iter_string_values(workflow)
            if "{{" not in value
            and any(p.search(value) for p in SECRET_VALUE_PATTERNS)
        ]
        assert not hits, f"{filename}: possible plaintext secrets:\n" + "\n".join(
            hits
        )

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_no_literal_values_under_sensitive_keys(self, filename):
        workflow = load_workflow(filename)
        hits = []
        for path, key, value in iter_key_value_pairs(workflow):
            if not _is_sensitive_key(key):
                continue
            # n8n parameter arrays look like {"name": ..., "value": ...};
            # those are covered when 'value' itself is checked below.
            candidate = value
            if isinstance(candidate, dict) and isinstance(
                candidate.get("value"), str
            ):
                candidate = candidate["value"]
            if isinstance(candidate, str) and not _is_allowed_secret_value(
                candidate
            ):
                hits.append(f"{path}.{key} = {candidate[:40]!r}")
        assert not hits, (
            f"{filename}: literal values under sensitive keys:\n"
            + "\n".join(hits)
        )

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_no_literal_values_for_sensitive_named_params(self, filename):
        """n8n parameter lists use {"name": "token", "value": "..."} pairs —
        check the 'value' side when 'name' is a sensitive word."""
        workflow = load_workflow(filename)
        hits = []

        def walk(obj, path=""):
            if isinstance(obj, dict):
                if _is_sensitive_key(obj.get("name", "")):
                    value = obj.get("value")
                    if isinstance(value, str) and not _is_allowed_secret_value(
                        value
                    ):
                        hits.append(f"{path}[{obj.get('name')!r}] = {value[:40]!r}")
                for key, child in obj.items():
                    walk(child, f"{path}.{key}")
            elif isinstance(obj, list):
                for index, child in enumerate(obj):
                    walk(child, f"{path}[{index}]")

        walk(workflow)
        assert not hits, (
            f"{filename}: literal values for sensitive named parameters:\n"
            + "\n".join(hits)
        )
