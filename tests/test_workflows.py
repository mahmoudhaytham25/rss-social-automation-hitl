"""Structural regression tests for the exported n8n workflows.

The JSON exports under ``workflows/`` are treated as data. These tests assert
the wiring and expression invariants behind recent bugfixes:

* merge ``resolveClash: addSuffix`` field references (facebook branch),
* the ``Final`` node having a single upstream input (post-creation flow),
* the regeneration-attempt cap (post-creation flow),
* ``JSON.stringify`` escaping instead of manual backslash escaping
  (bluesky / facebook / linkedin branches),
* non-fatal ``onError`` on the file-read node (post-creation flow),
* no plaintext secrets in any export,
* export hygiene — no ``meta``/``versionId`` keys, ``active`` falsy,
  unique workflow ids across all six exports,
* the shared Discord approval loop — 24h ``limitWaitTime`` on
  ``sendAndWait``, bounded re-ask via ``Classify``/``Route`` nodes with a
  notify-and-skip exit, decline-path field reset, and expression-based
  publish dispatch (approval-loop); the per-platform ``platformName`` /
  ``publishWorkflowId`` contract it consumes (main, post-creation flow),
* retry policy — transient fetches retry ``3x`` with ``1s`` backoff,
  publish calls never retry,
* every workflow routed to the shared ``error-handler`` sub-workflow,
* every non-trigger node reachable forward from a trigger (no dead
  wiring beyond the documented ``ai_*`` satellites).

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
    descendants,
    find_nodes,
    iter_edges,
    iter_js_code,
    iter_key_value_pairs,
    iter_string_values,
    load_all_workflows,
    load_workflow,
    merge_resolve_clash,
    node_by_name,
    node_names,
    successors,
    upstream_node_names,
    workflow_by_id,
)

MAIN = "main.json"
POST_CREATION = "post-creation-flow.json"
APPROVAL_LOOP = "approval-loop.json"
BLUESKY = "bluesky-branch.json"
FACEBOOK = "facebook-branch.json"
LINKEDIN = "linkedin-branch.json"
ERROR_HANDLER = "error-handler.json"

ORIGINAL_FILES = tuple(f for f in WORKFLOW_FILES if f != ERROR_HANDLER)


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
    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_execute_workflow_targets_are_exported(self, filename):
        """executeWorkflow nodes call sub-workflows by workflowId value; every
        statically referenced id must match the top-level ``id`` of one of the
        exported files. Expression values (``={{ ... }}``) are resolved at
        runtime and checked separately by TestApprovalLoop."""
        workflow = load_workflow(filename)
        id_to_name = {
            wf["id"]: wf.get("name") for wf in load_all_workflows().values()
        }

        callers = find_nodes(
            workflow, lambda n: n["type"].endswith("executeWorkflow")
        )
        if filename == MAIN:
            assert callers, "expected executeWorkflow nodes in main.json"

        for node in callers:
            ref = node["parameters"].get("workflowId")
            value = ref.get("value") if isinstance(ref, dict) else ref
            if isinstance(value, str) and value.startswith("="):
                continue  # dynamic dispatch — pinned in TestApprovalLoop
            assert value in id_to_name, (
                f"{filename} / {node['name']!r} calls workflow id "
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


# ---------------------------------------------------------------------------
# 6. Export hygiene — no per-instance state committed to git
# ---------------------------------------------------------------------------

# Top-level keys that are instance state or runtime bookkeeping — must match
# STRIPPED_KEYS in scripts/normalize-exports.py.
BANNED_TOP_LEVEL_KEYS = (
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


class TestExportHygiene:
    """Instance-specific bookkeeping (``meta``, ``versionId``, and the
    extras newer n8n versions add — see ``BANNED_TOP_LEVEL_KEYS``) churns
    the diff on every re-export, and ``active: true`` would auto-activate
    the workflow on import into another n8n instance. None of it belongs
    in git."""

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_no_instance_specific_top_level_keys(self, filename):
        workflow = load_workflow(filename)
        for key in BANNED_TOP_LEVEL_KEYS:
            assert key not in workflow, f"{filename}: top-level {key!r} leaked"

    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_workflow_is_exported_inactive(self, filename):
        workflow = load_workflow(filename)
        assert not workflow.get("active"), (
            f"{filename}: 'active' must be falsy, got "
            f"{workflow.get('active')!r}"
        )

    def test_workflow_ids_are_unique(self):
        """executeWorkflow / errorWorkflow settings reference workflows by
        id; a collision would silently point at the wrong export."""
        by_id = workflow_by_id()
        assert len(by_id) == len(WORKFLOW_FILES), (
            f"workflow ids are not unique — {len(WORKFLOW_FILES)} files "
            f"map to {len(by_id)} ids: {sorted(by_id)}"
        )


# ---------------------------------------------------------------------------
# 7. Shared approval loop (approval-loop.json) + main-side dispatch contract
# ---------------------------------------------------------------------------

# The three platforms used to duplicate the same 7-node approval loop in
# main.json. It now lives once in approval-loop.json; each platform calls it
# via an executeWorkflow node, and the loop dispatches the publish step back
# out through the item's ``publishWorkflowId`` field.

# Fields the wrapper passes to post-creation-flow on (re)generation. The
# decline path must emit exactly these — nothing else — so stale verdicts
# (``data.approved``, ``decision``, ``reasks``) or a previous ``output`` can
# never leak into the next wait and be misclassified.
RESET_VARS_FIELDS = {
    "title",
    "link",
    "imageUrl",
    "paragraphs",
    "sysPrompt",
    "userPrompt",
    "branch",
    "limit",
    "platformName",
    "publishWorkflowId",
}

# main.json: each platform's variables node feeds an executeWorkflow call to
# the shared loop, and emits the publish target for that platform.
PLATFORM_DISPATCH = {
    "Add Facebook Variables": ("Facebook", "rYRy7pbxfnStYz7H"),
    "Add Bluesky Variables": ("Bluesky", "zvxqmMaPcqiBRWOC"),
    "Add Linkedin Variables": ("LinkedIn", "q2qKF9EkXT8XnINX"),
}

APPROVAL_LOOP_ID = "Appr0va1L00pSh4r3d"


class TestApprovalLoop:
    """Pins the approval-loop contract in the shared sub-workflow: a 24h wait
    cap, a bounded number of re-asks decided by the Classify code node, a
    Route switch whose four outputs land on the right targets — including
    the re-ask edge looping back onto the sendAndWait node — and a decline
    path that regenerates through a strict field reset."""

    def test_send_and_wait_has_24h_limit(self):
        """Without ``limitWaitTime`` a sendAndWait waits forever — an
        unanswered approval stalls the whole pipeline indefinitely."""
        node = node_by_name(load_workflow(APPROVAL_LOOP), "Wait for approval")
        assert node["parameters"].get("operation") == "sendAndWait"
        values = (
            node.get("parameters", {})
            .get("options", {})
            .get("limitWaitTime", {})
            .get("values")
        )
        if isinstance(values, list):  # fixedCollection may export a list
            assert len(values) == 1, "unexpected limitWaitTime shape"
            values = values[0]
        assert isinstance(values, dict), "options.limitWaitTime.values missing"
        assert values.get("resumeAmount") == 24
        assert values.get("resumeUnit") == "hours"

    def test_loop_is_wired_generate_wait_merge_classify_route(self):
        workflow = load_workflow(APPROVAL_LOOP)
        for source, expected in (
            ("Start", "Generate post"),
            ("Generate post", "Wait for approval"),
            ("Generate post", "Merge"),
            ("Wait for approval", "Merge"),
            ("Merge", "Classify approval"),
            ("Classify approval", "Route approval"),
        ):
            targets = {t for _i, t in successors(workflow, source)}
            assert expected in targets, (
                f"{source!r} must feed {expected!r}; got {sorted(targets)}"
            )

    def test_generate_post_calls_post_creation_flow(self):
        node = node_by_name(load_workflow(APPROVAL_LOOP), "Generate post")
        assert node["type"].endswith("executeWorkflow")
        assert (
            node["parameters"]["workflowId"]["value"]
            == load_workflow(POST_CREATION)["id"]
        )

    def test_merge_combines_post_with_verdict(self):
        """A button click returns only ``{data: {approved}}`` — the generated
        post lives on input 0 and must be recombined positionally."""
        node = node_by_name(load_workflow(APPROVAL_LOOP), "Merge")
        params = node["parameters"]
        assert params.get("mode") == "combine"
        assert params.get("combineBy") == "combineByPosition"

    def test_classify_is_code_node_with_bounded_reask_logic(self):
        node = node_by_name(load_workflow(APPROVAL_LOOP), "Classify approval")
        assert node["type"].endswith(".code")
        code = node.get("parameters", {}).get("jsCode", "")
        for token in ("MAX_REASKS", "decision", "reasks"):
            assert token in code, (
                f"classifier jsCode is missing {token!r} — "
                "the re-ask loop may be unbounded"
            )

    def test_route_switch_outputs_hit_the_right_targets(self):
        workflow = load_workflow(APPROVAL_LOOP)
        route = node_by_name(workflow, "Route approval")
        assert route["type"].endswith(".switch")
        outputs = (
            workflow.get("connections", {})
            .get("Route approval", {})
            .get("main", [])
        )
        assert len(outputs) == 4, (
            f"Route approval: expected exactly 4 outputs "
            "(publish / regenerate / re-ask / skip), "
            f"got {len(outputs)}"
        )
        by_index = {}
        for output_index, target in successors(workflow, "Route approval"):
            by_index.setdefault(output_index, []).append(target)
        assert by_index.get(0) == ["Publish post"], (
            "output[0] must publish to the platform"
        )
        assert by_index.get(1) == ["Reset post vars"], (
            "output[1] must regenerate via the field reset"
        )
        assert by_index.get(2) == ["Wait for approval"], (
            "output[2] is the re-ask loop — it must feed back into the "
            "sendAndWait node"
        )
        assert by_index.get(3) == ["Notify skipped"], (
            "output[3] must notify that the post was skipped"
        )

    def test_publish_dispatches_by_publish_workflow_id(self):
        """The shared loop publishes via ``{{ $json.publishWorkflowId }}`` —
        the per-platform value emitted by main's variables nodes."""
        node = node_by_name(load_workflow(APPROVAL_LOOP), "Publish post")
        assert node["type"].endswith("executeWorkflow")
        assert node["parameters"].get("mode") == "each"
        ref = node["parameters"]["workflowId"]
        assert ref.get("value") == "={{ $json.publishWorkflowId }}"

    def test_decline_resets_item_before_regenerating(self):
        """The decline route loops back through a Set node that re-emits
        exactly the post-vars fields. A bare ``{...item}`` passthrough would
        carry ``data.approved: false`` into the next wait; the following
        timeout passthrough would then read a stale verdict and misclassify
        the timeout as another decline."""
        workflow = load_workflow(APPROVAL_LOOP)
        reset = node_by_name(workflow, "Reset post vars")
        assert reset["type"].endswith(".set")
        assigned = {
            a["name"]
            for a in reset["parameters"]["assignments"]["assignments"]
        }
        assert assigned == RESET_VARS_FIELDS
        # And the reset must feed back into generation — not the wait —
        # so a decline always regenerates the post.
        targets = {t for _i, t in successors(workflow, "Reset post vars")}
        assert targets == {"Generate post"}, (
            f"Reset post vars must feed Generate post; got {sorted(targets)}"
        )

    def test_notify_node_is_a_dead_end_discord_message(self):
        workflow = load_workflow(APPROVAL_LOOP)
        node = node_by_name(workflow, "Notify skipped")
        assert node["type"].endswith("discord")
        outgoing = [
            edge
            for source, _conn_type, edge in iter_edges(workflow)
            if source == "Notify skipped"
        ]
        assert not outgoing, (
            "'Notify skipped' must be a terminal notification — "
            "nothing should consume its output"
        )

    def test_sub_workflow_emits_platform_fields(self):
        """``platformName``/``publishWorkflowId`` reach the publish and
        notify nodes via post-creation-flow's output — they must be
        carried through ``Output data`` and the internal ``Code2``
        regeneration rebuild."""
        workflow = load_workflow(POST_CREATION)
        out = node_by_name(workflow, "Output data")
        names = {
            a["name"] for a in out["parameters"]["assignments"]["assignments"]
        }
        assert {"platformName", "publishWorkflowId"} <= names, (
            f"'Output data' drops platform fields: {sorted(names)}"
        )
        code2 = node_by_name(workflow, "Code2")["parameters"]["jsCode"]
        for field in ("platformName", "publishWorkflowId"):
            assert field in code2, (
                f"Code2 rebuild drops {field!r} on the over-length "
                "regeneration path"
            )


class TestMainApprovalDispatch:
    """main.json holds one variables node + one shared-loop call per
    platform; the variables node emits the platform identity and publish
    target the loop needs."""

    @pytest.mark.parametrize(
        "add_vars,publish_id",
        [(v, meta[1]) for v, meta in PLATFORM_DISPATCH.items()],
        ids=list(PLATFORM_DISPATCH),
    )
    def test_variables_node_feeds_shared_loop(self, add_vars, publish_id):
        workflow = load_workflow(MAIN)
        targets = {t for _i, t in successors(workflow, add_vars)}
        assert len(targets) == 1, (
            f"{add_vars!r} should feed exactly one node; got {sorted(targets)}"
        )
        target = node_by_name(workflow, next(iter(targets)))
        assert target["type"].endswith("executeWorkflow")
        assert (
            target["parameters"]["workflowId"]["value"] == APPROVAL_LOOP_ID
        )

    @pytest.mark.parametrize(
        "add_vars,meta", list(PLATFORM_DISPATCH.items()), ids=list(PLATFORM_DISPATCH)
    )
    def test_variables_node_emits_platform_metadata(self, add_vars, meta):
        platform_name, publish_id = meta
        code = node_by_name(load_workflow(MAIN), add_vars)["parameters"][
            "jsCode"
        ]
        assert f'platformName: "{platform_name}"' in code
        assert f'publishWorkflowId: "{publish_id}"' in code

    @pytest.mark.parametrize(
        "add_vars", list(PLATFORM_DISPATCH), ids=list(PLATFORM_DISPATCH)
    )
    def test_enclosure_access_is_guarded(self, add_vars):
        """A feed item without an ``<enclosure>`` (no image) must not crash
        the run — ``enclosure?.url`` yields undefined and the branch's
        image-If routes the no-image path."""
        code = node_by_name(load_workflow(MAIN), add_vars)["parameters"][
            "jsCode"
        ]
        assert "enclosure?.url" in code
        assert not re.search(r"enclosure\.url", code.replace("enclosure?.url", ""))

    def test_legacy_if_nodes_are_gone(self):
        """The single-branch If/If1/If2 approval checks were replaced by the
        Classify/Route loop; if they reappear the re-ask cap is bypassed."""
        leftover = node_names(load_workflow(MAIN)) & {"If", "If1", "If2"}
        assert not leftover, (
            f"legacy approval nodes still present: {sorted(leftover)}"
        )


# ---------------------------------------------------------------------------
# 8. Retry policy — transient fetches retry, publish calls never do
# ---------------------------------------------------------------------------

# External fetches that are safe to repeat: they don't publish anything, so
# a retry can't produce duplicates.
RETRY_NODES = (
    (BLUESKY, "Open BlueSky Session"),
    (BLUESKY, "Download Image from RSS"),
    (BLUESKY, "Uploading Image to BlueSky Blob Storage"),
    (FACEBOOK, "Get Facebook Page ID"),
    (FACEBOOK, "Image upload to FB"),
    (LINKEDIN, "Download Image from RSS"),
)

# The actual publish calls. Retrying these can double-post when the first
# attempt succeeded but the response was lost.
PUBLISH_NODES = (
    (BLUESKY, "Post Creation w/ Clickable Link, Hashtag, Embedded Image"),
    (BLUESKY, "Post Creation w/ Clickable Link, Hashtag, Embedded Image1"),
    (FACEBOOK, "Post Creation FB"),
    (FACEBOOK, "Post Creation FB1"),
    (LINKEDIN, "Create a post WO/img"),
    (LINKEDIN, "Create a post W/img"),
)


class TestRetries:
    @pytest.mark.parametrize("filename,node_name", RETRY_NODES)
    def test_transient_fetch_nodes_retry_3x_with_1s_backoff(
        self, filename, node_name
    ):
        node = node_by_name(load_workflow(filename), node_name)
        assert node.get("retryOnFail") is True, (
            f"{filename} / {node_name!r}: retryOnFail must be true"
        )
        assert node.get("maxTries") == 3, (
            f"{filename} / {node_name!r}: maxTries must be 3, "
            f"got {node.get('maxTries')!r}"
        )
        assert node.get("waitBetweenTries") == 1000, (
            f"{filename} / {node_name!r}: waitBetweenTries must be 1000, "
            f"got {node.get('waitBetweenTries')!r}"
        )

    @pytest.mark.parametrize("filename,node_name", PUBLISH_NODES)
    def test_publish_nodes_never_retry(self, filename, node_name):
        node = node_by_name(load_workflow(filename), node_name)
        assert not node.get("retryOnFail"), (
            f"{filename} / {node_name!r}: publish calls must not retry — "
            "a lost response would double-post"
        )


# ---------------------------------------------------------------------------
# 9. Error workflow — every original routes failures to error-handler
# ---------------------------------------------------------------------------

class TestErrorWorkflow:
    """The five originals must name the shared error-handler workflow in
    ``settings.errorWorkflow``, and that handler must fan an ``errorTrigger``
    into a discord notification."""

    @pytest.mark.parametrize("filename", ORIGINAL_FILES)
    def test_settings_point_at_error_handler(self, filename):
        workflow = load_workflow(filename)
        handler_id = load_workflow(ERROR_HANDLER)["id"]
        assert workflow.get("settings", {}).get("errorWorkflow") == (
            handler_id
        ), (
            f"{filename}: settings.errorWorkflow must be {handler_id!r}, "
            f"got {workflow.get('settings', {}).get('errorWorkflow')!r}"
        )

    def test_error_trigger_feeds_a_discord_node(self):
        workflow = load_workflow(ERROR_HANDLER)
        triggers = find_nodes(
            workflow, lambda n: n["type"].endswith("errorTrigger")
        )
        assert triggers, f"{ERROR_HANDLER}: no errorTrigger node found"
        for trigger in triggers:
            targets = {t for _i, t in successors(workflow, trigger["name"])}
            discord = sorted(
                t
                for t in targets
                if node_by_name(workflow, t)["type"].endswith("discord")
            )
            assert discord, (
                f"{ERROR_HANDLER}: errorTrigger {trigger['name']!r} has no "
                f"discord node directly downstream (targets: {sorted(targets)})"
            )


# ---------------------------------------------------------------------------
# 10. Reachability — no dead wiring beyond the documented exceptions
# ---------------------------------------------------------------------------

# Trigger/start node types that seed the forward traversal.
TRIGGER_TYPES = (
    "rssFeedReadTrigger",
    "executeWorkflowTrigger",
    "errorTrigger",
)

# Nodes legitimately not forward-reachable from a trigger via ``main``
# edges, keyed by filename. Pre-existing: in post-creation-flow these three
# satellites feed ``AI Agent`` through ai_* connection types (language
# model, tool, output parser) — those edges point *into* the agent, so
# forward main-edge traversal can never reach them. They are wired, not
# dead. Anything else landing in the unreachable set is genuinely orphaned
# wiring and must fail.
KNOWN_UNREACHABLE = {
    POST_CREATION: {
        "Structured Output Parser",
        "Google Gemini Chat Model",
        "Character Count",
    },
}


class TestReachability:
    @pytest.mark.parametrize("filename", WORKFLOW_FILES)
    def test_unreachable_nodes_match_the_known_set(self, filename):
        workflow = load_workflow(filename)
        triggers = {
            node["name"]
            for node in workflow["nodes"]
            if any(node["type"].endswith(t) for t in TRIGGER_TYPES)
        }
        assert triggers, f"{filename}: no trigger/start node found"
        reachable = set(triggers)
        for name in triggers:
            reachable |= descendants(workflow, name)
        unreachable = node_names(workflow) - reachable
        assert unreachable == KNOWN_UNREACHABLE.get(filename, set()), (
            f"{filename}: unreachable-from-trigger nodes are "
            f"{sorted(unreachable)}; expected exactly "
            f"{sorted(KNOWN_UNREACHABLE.get(filename, set()))} "
            "(update KNOWN_UNREACHABLE only for legitimately ai_*-wired "
            "satellite nodes)"
        )
