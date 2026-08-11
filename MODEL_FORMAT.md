# Personal model format

The public personal model is a versioned, read-only JSON projection of local
Runtime state. CLI export and the `model` object inside `/model/graph` expose
the complete schema below; consumers must not import internal DAOs or query
SQLite tables directly. MCP `get_model_snapshot` overview/page reads use the
same live model generation but return a separately versioned, bounded projection so a growing
audit history cannot overflow an MCP client. Redaction policy differs: export
and MCP redact by default, while the owner-only loopback viewer uses raw local
content.

`<PERSOME_ROOT>/HUMAN.md` (`~/.persome/HUMAN.md` by default) is a separate,
deterministic reading view of that snapshot, not another model or import
format. It contains raw local Root, Volume, and Face text, is written
owner-only with mode `0600`, and may change when Persome's renderer changes.
The versioned JSON snapshot and its build manifest remain authoritative for
machines, reproducibility, receipts, and redacted export. A missing Root renders
an explicit still-forming placeholder rather than a fabricated identity.

## Top-level schema

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-10T09:06:00+00:00",
  "build": {},
  "points": [],
  "lines": [],
  "faces": [],
  "volumes": [],
  "root": null,
  "receipts": [],
  "stats": {}
}
```

Consumers must branch on `schema_version`. Package versions do not substitute
for a schema check.

## MCP bounded projection

`get_model_snapshot(section="overview")` is the default agent-facing read. It
returns `projection_schema_version`, `model_schema_version`, build metadata,
canonical `model_stats`, and compact Root/Face/Volume objects. It deliberately
omits `points`, `lines`, and `receipts` instead of returning empty arrays that
could be mistaken for an empty model. `coverage` states what was returned and
the full canonical totals.

Use the `points`, `lines`, `faces`, `volumes`, `root`, or `receipts` sections
with the opaque `cursor` and a limit of at most 100. Up to 20 exact `ids` can replace a
cursor for a focused read. Aggregate member and receipt arrays are summarized
by default; `include_evidence_refs=true` opts into them while retaining the
same hard 64 KiB budget for the JSON string in the MCP result's
`content[0].text`; JSON-RPC framing and escaping are outside that payload
budget. A page may therefore return fewer items than requested, and one
individually oversized object returns an explicit bounded error with a
`resume_cursor` when later items remain. Each call is transactionally stable;
pages do not claim a
cross-call frozen database revision.

`section="full"` never serializes the full object over MCP. It returns the
local, redacted-by-default export instruction instead:

```bash
persome model export --out ./model-snapshot.json
```

This projection does not change canonical `schema_version: 1`: CLI export and
`/model/graph` still include historical Points, evolution Lines, and complete
receipts.

The bounded envelope replaces the v0.3.x default MCP response starting with
the next minor release, v0.4.0. Existing integrations that parsed canonical
top-level arrays from `get_model_snapshot()` must migrate to
`projection_schema_version`, request the needed section pages, or use
`persome model export` for one complete canonical object. This breaking
response change must not be published as a v0.3.x patch.

The `build` object has one fixed key set in every state. While a build is in
progress or no valid completed build exists, unavailable identity fields are
`null`, maps are empty, and input-window bounds are `null`; these sentinels do
not claim a successful build.

## Geometry

### Point

A Point is one evomem node, including historical nodes needed to reconstruct
evolution. Important fields include:

```text
id, content, layer, status, is_latest,
supersedes, superseded_by,
occurred_at, valid_from, valid_until, created_at,
file_name, tags, confidence, conflicted, receipt
```

`is_latest` identifies a current chain head; historical Points remain available
for audit and time travel. The snapshot retains both, while the default constellation renders only
the chain head valid at its selected cutoff. Historical versions remain available through cutoff-
bounded search, History, receipts, and evolution Lines rather than appearing beside the current
Point as another live sphere. Historical evidence requests carry the same `as_of` boundary, so
future successors and later nearby captures are not returned during drill-down; **Now** remains
unbounded.

The memory owner is the reserved identity `self`, not a person Point. Names and
handles learned from quoted owner-identity evidence resolve to `self`; if an
owner alias was previously minted as a person, promotion retires that live
projection while preserving its historical Point receipts.

For the default windowed writer, a previously unseen machine-derived entity or assertion is not a
Point yet. It is held in the owner-local candidate ledger until two distinct known sessions support
the same canonical candidate. Same-session retries and extra windows are audit evidence, not
independent promotion votes. Owner edits and already-live Points use their existing authority paths.

### Line

Lines have two forms:

- `kind: evolution`: one Point supersedes another;
- `kind: relation`: a semantic/entity relation with predicate, confidence,
  validity, and provenance.

Relation endpoints retain canonical identity strings in the public contract. The viewer resolves an
unambiguous canonical endpoint to the current entity Point for placement and creates a context node
only when no safe match exists. Case, width, and whitespace variants share one projected context
node, and semantically identical projected Lines render once while every original Line remains in
local search and evidence audit. The projection does not rewrite the Line or mutate snapshot IDs.

Activity-derived relation Lines carry the atomic source triplet
`source_kind`, `source_id`, and `source_receipt`. New activity identities use
`event:occurrence:<id>`, `event:entry:<id>`, or `event:session:<id>`. A windowed
memory-delta event uses `source_kind: occurrence`; its stable occurrence ID is
separate from the recurring series ID stored in `event_occurrences`.
`event:intent:<id>` exists only for read-only migration of old data.

### Face

A Face is one active level-1 `schema_faces` row. It contains a behavioral
signature, members, observations, confidence, provenance, anchors, and source
receipts. Promotion requires stable repeated support. Production observations are deduplicated by
producer, UTC sample day, and canonical input hash before they can increment the count.

Every projected Point and schema object carries `edit_refusal`: an empty string when the owner can
correct it, otherwise a stable reason code naming why not — a newer version exists, the object was
withdrawn, the pattern is not promoted, or nothing backs the Point. Readers should treat a non-empty
value as "do not offer a correction here" rather than deriving that themselves; the Runtime knows
which layer backs each object and whether a newer version exists, and a client does not.

`provenance` names which extractor reached the object: `mined`, `emergent`,
`both`, or `synth` for a synthesized Root. The value `authored` means the
memory owner replaced the signature by hand. Derivation continues under an
authored object, so its observations and confidence keep moving, but no
extractor rewrites its signature. A Point the owner wrote or corrected carries
the `source:owner-edit` tag, which is the same distinction at the Point layer.
Both are visible in every snapshot reader, so an owner's own claim is never
mistaken for a derived one.

### Volume

A Volume is one active level-2 cross-domain schema. It relates behavior across
otherwise separate owner-scoped topics and carries the same audit fields as a
Face. Person schemas are excluded so evidence about a collaborator cannot be
fused into the memory owner's behavior. A same-day replay of one cross-domain input cannot satisfy
the two-resample promotion threshold.

### Root

`root` is `null` or one active level-3 apex. More than one live Root is a
contract error. Root receipts aggregate evidence through its members so the
summary can be expanded back to Points. Identical same-day synthesis input reuses its input receipt
and does not supersede the resident Root.

## Receipts

Receipts are stable evidence handles rather than embedded raw capture payloads.
Each Point has a receipt; sourced relations and aggregate geometry preserve or
collect those handles. The MCP `resolve_evidence` tool and authenticated
`GET /model/evidence?ref=...` route resolve model, memory, activity, and capture
references through one progressive contract. Explicit lineage is returned as
`sources`; time-adjacent capture clues are returned separately as `context` and
must not be described as direct proof. Resolved nodes and links include a
human-readable `label`; Point predecessor/successor links are returned in
`history`, separate from derivation sources. Raw receipts remain stable
technical handles. `read_receipt` remains the focused,
backward-compatible memory-entry resolver.

## Build record

The `build` object records:

```text
build_id, core_commit, models, prompt_hashes, config_hash,
input_window, mode, trigger, started_at, completed_at,
duration_ms, degraded_stages, status
```

No API keys or full configuration values are copied into the manifest.
`build_id` is the stable hash of every other manifest field; `complete` requires
an empty `degraded_stages`, while `degraded` requires at least one stage.

Live HTTP, MCP, and CLI-export snapshots use the last persisted completed or
degraded build record. If no valid build record exists, they report `status: not_built`,
`trigger: no_completed_build`, and a null `build_id`; inspecting the current
database projection never fabricates a successful build. A raw
`model-build.json` with `status: building` is exposed as `building` only while
the process still holds `model-build.lock`. If that lock is free, the marker is
an interrupted-build remnant and public surfaces report `not_built`.

## Stats

`stats` reports Point count, evolution/relation Line counts, Face/Volume/Root
counts, receipt count, and redaction counts. Complete geometry requires
non-empty Points, at least one Line, at least one Face and Volume, and exactly
one Root.

## Redaction and export

```bash
persome model export
persome model export --out ./model-snapshot.json
persome model export --raw  # explicit sensitive-data opt-out
```

Default export applies the Runtime's deterministic secret/PII scrubber, removes
detectable absolute paths and sensitive text categories, writes atomically, and
sets mode `0600`. This is a sharing aid, not a guarantee that all names or
organizations are anonymous. Publishing an export requires separate consent
and anonymization review.

The detailed implementation contract is in
[`docs/model-contract.md`](docs/model-contract.md). The schema golden is
[`tests/fixtures/runtime_model/model_snapshot_v1.golden.json`](tests/fixtures/runtime_model/model_snapshot_v1.golden.json).
