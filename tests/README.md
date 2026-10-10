# Workflow Validation Tests

**Yes, this is more rigor than an n8n export repo strictly needs.** These are exported
workflow definitions — data, not a codebase under active development. A typical n8n
repo would have no tests at all.

The suite exists anyway, for three specific reasons:

1. **Real bugs were found and fixed in these exports.** The most instructive one:
   a Merge node using `combineByPosition` + `resolveClash: addSuffix` silently renames
   *every* field (`id` → `id_2`, `facebook` → `facebook_2`), so the Facebook post node
   was hitting `/undefined/feed` with an empty message — while looking correct in the
   n8n editor. The tests pin that wiring so it can't regress unnoticed.
2. **They document invariants that are invisible in the UI.** Which fields each node
   expects, which sub-workflow IDs are called, where the approval loop exits, the
   3-attempt regeneration cap — the tests are executable documentation of how the
   pipeline is actually wired.
3. **They catch corrupted or leaky exports.** Every run validates JSON structure,
   checks for dangling node connections, and scans for accidentally exported
   plaintext secrets — real failure modes when moving workflows between instances.

The tests treat the workflow JSONs as data and assert structural properties — they
don't execute the workflows. Run them with:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```
