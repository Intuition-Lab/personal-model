# Model snapshot contract

`persome.model` is the public, read-only projection of the model stored by the Runtime.
It does not define a second model or run capture/build jobs. It turns the current SQLite state into
one versioned JSON object that the local viewer and CLI export can consume without importing internal
DAOs. The MCP adapter reads that same live generation through a separately versioned, bounded
overview/page envelope; it does not serialize the complete object into one tool result.

The operator surface is:

```bash
persome model build
persome model status
persome model export --out model-snapshot.json
```

## Human-readable projection

After a build, Persome deterministically projects the raw current snapshot to
`<PERSOME_ROOT>/HUMAN.md` (`~/.persome/HUMAN.md` by default). The file is an
owner-local `0600` reading surface, not a second model contract: the versioned
JSON snapshot, build manifest, and evidence APIs remain the machine authority.
No additional capture or LLM call is needed to render it.

Daemon startup and `persome onboard` reconcile the file from an existing valid
Root, so upgrades backfill it without rebuilding the user's history. With no
verified Root, Persome writes a truthful forming placeholder and replaces that
placeholder after later model builds. Automatic refresh replaces only files
with Persome's projection marker. If an unrecognized, self-authored
`HUMAN.md` already occupies the path, Persome preserves it and reports the
conflict instead of overwriting it. Direct edits to a managed projection are
not a correction interface and may be replaced. Correct the model through
`persome correct` (natural language, LLM-mediated), the viewer (click a claim to
rewrite it), or `persome model edit` (both deterministic and object-addressed).

`model build` uses an exclusive `<PERSOME_ROOT>/model-build.lock`. It waits up to 30 seconds by
default; `--wait-seconds` changes the bound and `--no-wait` returns `busy` immediately. The kernel
releases the lock on process exit. A run first atomically records `status: building`, invalidating
any older completed manifest before a mutating stage starts. Successful or degraded runs replace it
with an owner-only `model-build.json`. That marker is exposed as `building` only while the process
holds `model-build.lock`; once the lock is free, a leftover marker is exposed as `not_built`, never
as the previous success. Missing Points, Lines, Faces, Volumes, or Root is recorded as degraded.
`model status` also requires a valid completed/degraded manifest before reporting `ready`.

## Geometry

The contract exposes:

- `points`: evomem nodes, including historical nodes needed to reconstruct evolution chains.
- `lines`: vertical `supersedes` edges and currently active horizontal relation
  edges. Directly observed `engaged_with` attention is active immediately;
  inferred semantic edges remain hidden until repeated evidence promotes them.
- `faces`: active level-1 schemas.
- `volumes`: active level-2 cross-domain schemas.
- `root`: zero or one active level-3 apex. More than one live root is a contract error.
- `receipts`: stable evidence handles for points and sourced relation lines. Face, Volume, and Root
  objects aggregate `source_receipts` through their member chain so the apex can be audited down to
  fact evidence.
- `build` and `stats`: build identity/timing plus auditable object and redaction counts.

`schema_version` starts at `1`. Consumers must branch on this field instead of inferring a version
from package releases.

Every `build` object records the core commit, stage model names, prompt hashes, a config hash, input
window, mock/real mode, timing, and degraded stages. Configuration values themselves are not copied
into the manifest. Fixed inputs and timestamps produce the same `build_id`.
The live HTTP and CLI-export snapshots, plus each successful MCP overview/page
projection, preserve that persisted manifest exactly. The manifest `build_id` must match the stable hash of every other manifest field, and
`complete`/`degraded` must agree with an empty/non-empty `degraded_stages` list. If there is no valid
completed or degraded manifest, the projection reports
`status: not_built`, `trigger: no_completed_build`, and a null `build_id`
instead of synthesizing a completed build from the current database contents.
The `not_built` and `building` states keep the same fixed build-object keys;
unavailable commit, config hash, mode, and timestamps are null, model and prompt
maps are empty, and the input-window bounds are null.

## MCP projection contract

`get_model_snapshot` defaults to an `overview` envelope with
`projection_schema_version: 1`, the underlying `model_schema_version`, build
metadata, canonical totals in `model_stats`, compact Root/Face/Volume objects,
and explicit coverage. Missing `points`, `lines`, or `receipts` mean “omitted
from this bounded response,” never “the model has none.”

The six Point/Line/Face/Volume/Root/receipt sections are cursor-paged with a maximum of 100 items, and
up to 20 exact IDs can be selected. Aggregate evidence arrays are counts unless
explicitly requested. The JSON string in the MCP result's `content[0].text` is
at most 64 KiB; JSON-RPC framing and escaping are outside that payload budget.
A smaller effective page or a bounded oversized-item error preserves the text
payload limit. The error includes a `resume_cursor` when later page items remain.
The consistency guarantee covers one call, not a sequence of pages while the
Runtime continues writing.

Full schema-v1 data remains available through `persome model export` and the
owner-local `/model/graph`. Asking MCP for `section="full"` returns that CLI
instruction without constructing or sending an unbounded result. This keeps
historical shadow Points and their evolution/receipt chain in the canonical
contract while keeping transport behavior safe.

New machine-derived entity and assertion Points remain outside the snapshot as auditable candidates
until the same canonical candidate has evidence from two known, distinct sessions. Repeated windows
inside one session do not satisfy that independence gate. Existing Points and explicit owner edits
retain their established authority.

A Face becomes active only after mined and emergent signals agree across stable footprints. A
Volume has one honest producer (the cross-domain sweeper), so it becomes active after two stable
sweeper resamples. Production samples carry `(producer, UTC day, canonical input hash)` receipts, so
a same-day retry cannot provide a second vote. Root synthesis uses the same receipt boundary to
avoid replacing the live apex for an identical same-day input.

## Viewer layout

The loopback viewer projects the snapshot as a deterministic hierarchy. Rendering never mutates or
re-clusters stored model objects; the only writes it can make are the owner's own explicit
corrections, described below. Root stays at the center, Volumes occupy the inner shell, Faces
form outward semantic clusters, and Points grow as stable local clouds around their primary Face.
The viewer resolves Face membership through `member_receipts` and infers Volume-to-Face membership
from inherited `source_receipts`, because stored `members` may be internal stable keys rather than
public node IDs. Evolution-chain and same-source evidence inherit a Face cluster when possible;
unpromoted evidence remains in deterministic source clusters instead of a flat global ring.

The layout is append-stable for normal chronological growth: existing nodes keep their local
coordinates while later evidence expands the surrounding cloud. `window.__persomeLayoutState`
exposes aggregate layout health for local visual smoke tests without exposing node content or IDs.
The constellation renders only the Point chain head valid at the selected cutoff. Superseded and
other snapshot-retained inactive Points stay in local search, History, and evidence surfaces, but
are not drawn beside the current head; archived and unlinked retired/withdrawn records remain in
the durable store rather than the live snapshot. A relation endpoint that names an unambiguous
current entity Point reuses that Point's position; the viewer creates a context node only when no
such Point exists.

Search is a local view over current objects plus audit history that already existed at the selected
model-history cutoff. It never reveals a successor before its effective start (`valid_from`, then
`created_at`, then `occurred_at`). It ranks
human-readable text from the already-loaded snapshot; it does not call another service, persist
queries, or expand the canonical model. Choosing a result opens the same detail surface as selecting
its rendered object. Point heads that are active at the selected cutoff rank ahead of shadow and
historical Points. Those
non-active Points remain discoverable by specific content or state searches and are labeled as
shadow or historical results, preserving the snapshot's audit and time-travel boundary. The search
surface states this scope narrowly: search queries stay on the Mac;
the separate, explicit share actions retain their own review-before-posting boundary.

Selection also creates a first-order "model neighborhood" from explicit Lines and the deterministic
Root, Volume, Face, and Point hierarchy already used for layout. The selected object and its direct
semantic and hierarchy neighbors remain prominent while unrelated geometry is visually muted.
Hierarchy membership may be inferred from receipts for layout, so this focus is navigation, not
evidence; only explicit Lines and the Evidence surface establish provenance. Focusing does not mutate
snapshot objects, coordinates, or persisted model state. Search reveal may enable the selected
object's layer when it was hidden; ordinary focus leaves layer visibility unchanged. The detail
surface labels the state as a navigation view, offers `Show all`, and temporarily adds a bounded set
of labels for otherwise-unlabeled first-order neighbors.

The viewer presents that hierarchy as a personal constellation: a Root-centered luminous core,
Volume and Face orbit structures, Point clouds, and restrained ambient depth cues. Its editorial
frame uses the live Root signature as the model's plain-language identity statement so each view is
recognizably personal without changing the snapshot. The `Local only` treatment is descriptive, not
a publishing control. The viewer never uploads the model or exposes its owner-only URL. An explicit
`Share` action renders a fixed-size PNG locally from the unlabeled WebGL constellation, adds the Root
identity statement, up to three highest-level Volume or Face signatures, aggregate layer counts, and
Persome branding, downloads it, and opens an X composer with one of three standard copy variants,
each carrying the Personal Model tag and official account mention. Individual
Point labels, receipts, source names, timestamps, and viewer credentials are excluded from the share
artifact; the owner attaches the downloaded image and confirms the post in X.
Both share actions take their written summaries and aggregate counts from the canonically scrubbed
`/model/share-card` projection rather than the owner-only graph. The adjacent `Card` action remains
a separate renderer that downloads the portrait `my-human-card.png` without opening X. Neighborhood
dimming and layer toggles are suspended only while the WebGL constellation is rendered for sharing.
The constellation share always renders the latest complete time slice from the same cached snapshot
generation as the current narrative and aggregate counts returned by `/model/share-card`, even when
the owner is inspecting history. The client rejects a version mismatch instead of combining two
generations. The export uses a fitted 16:9 overview at the artifact's own 1200-by-675 dimensions
rather than cropping the owner's focused, zoomed, or portrait viewport. Renderer resolution, camera
position and target,
timeline cutoff, hidden-layer choices, auto-rotation, any in-flight camera/zoom animation, selection,
the inspected snapshot, and focus return target are restored immediately after the pixels are copied,
so the owner's inspection state is unchanged.

Visible node labels and their Point, Face, Volume, Root, or context meshes open the same provenance
detail panel. Overview summarizes the evidence footprint, Evidence presents human-readable source
cards with drill-down breadcrumbs, and History keeps Point predecessor/successor versions separate
from derivation sources. Raw IDs, paths, and receipts stay collapsed under technical details. Labels
and tabs are keyboard-focusable; Escape closes the selection. Nodes retain a 12-pixel minimum
screen-space hit target so distant geometry stays selectable. Evolution and relation Lines open
their own human-readable endpoint, exact predicate, and evidence detail through an 8-pixel
screen-space hit target; node hits always win where geometry overlaps. Keyboard focus reveals a
line picker with the same detail action. Raw line and endpoint IDs remain inside collapsed technical
details. Explicit Lines and their count/toggle remain separate from derived hierarchy guides. The
guides follow endpoint-layer visibility and are named in the legend as inferred placement, not
evidence; they remain visual-only.
`window.__persomeInteractionState` exposes aggregate interaction counts and hit-target bounds for
local smoke tests.

Layer controls expose current snapshot counts, and the explanatory legend is collapsible without
changing layer visibility. On narrow screens the detail panel becomes a safe-area-aware bottom sheet
while retaining the same Overview, Evidence, History, and correction controls. Coarse-pointer node
and Line hit radii expand beyond the 12- and 8-pixel desktop minimums, and primary mobile controls
provide at least a 44-pixel target. The full legend yields to a compact, collapsed Guide disclosure
above the timeline; its summary keeps `inferred placement · not evidence` visible without covering
the primary controls, uses normal-text contrast for that safety boundary, and expanding it explains
where to find sourced support.

Zoom is relative to the fitted model: the visible minus, percentage, and plus controls cover 50%
through 400%, the percentage resets to 100%, and the plus, minus, and zero keys provide the same
actions in 25% steps. Wheel and trackpad pinch gestures zoom continuously toward the pointer,
gliding to their goal under the same damping that carries orbit and pan, so releasing a gesture
coasts to a stop rather than stopping dead. Wheel deltas are normalized to pixels first, so a
gesture means the same amount of zoom whether the browser reports pixels, lines, or pages, and one
comfortable trackpad pinch is worth roughly a doubling on every display. A pinch is read from
`ctrlKey` wheel events and, on browsers that report one instead, from gesture events; a touchscreen
pinch and a middle-button drag keep the orbit controller's own dolly, which already tracks the
fingers directly. `window.__persomeZoomState` exposes only aggregate distance and percentage values
for local visual smoke tests.

Choosing a search result, or pressing `F` for the current selection, smoothly flies the camera to
that object without changing the deterministic layout. With reduced motion requested, the same move
is immediate. `Frame` restores the fitted overview independently of the selection and active layers.

Dragging works across the model canvas, including on a label, while search, the collapsible legend,
and the detail surfaces retain their own pointer and scroll behavior. A second finger cancels the
click so a pinch never selects a node.
`window.__persomeFrameStats` exposes rolling p50, p95, and worst frame costs in milliseconds over
the last 240 frames, so a smoothness regression is measurable locally rather than only visible.

## Owner corrections

The memory owner can rewrite a modeled object's wording or reject it outright, from the viewer,
`POST /model/edit`, or `persome model edit`. In the viewer the claim itself is the editing surface:
clicking the text opens it for rewriting in place, blur or Cmd+Enter commits, and Escape discards.
There is no separate edit mode, and provenance folds beneath the claim rather than competing with it
for the reader's attention. All three share one deterministic,
LLM-free writer, so a correction applies offline and cannot fail to find its target: the caller
addresses the object by the id the snapshot published. Points, Faces, Volumes, and the Root are
editable. Lines are not — a relation or evolution Line is derived from the objects it connects, so
the objects are what get corrected.

The two layers use different mechanisms because different machinery would otherwise discard the
edit.

A **Point** correction goes through the ordinary supersede path. The owner's wording becomes a new
entry carrying the `source:owner-edit` tag; the superseded fact keeps its own row, its receipts,
and its place on the evolution Line. Rejecting a Point retires it without a successor, and a Point
retired without a successor leaves the live model the way a closed Line or an archived Face does.
Its row stays queryable, so the rejection remains auditable and reversible.

A **Face**, **Volume**, or **Root** correction sets `provenance` to `authored`. Derivation keeps
running underneath an authored object — observations accumulate and confidence still ratchets — but
the schema miner stops rewriting its signature and root synthesis stops replacing the apex. That
marker is what makes the correction durable, and it is what distinguishes an owner's claim from a
derived one in every reader: the viewer, the MCP snapshot, `HUMAN.md`, and exports. Rejecting one
archives it, which removes it from the live geometry while preserving its members, footprints, and
observation count.

Every correction records the text it displaced. An owner-authored object never accrues
time-adjacent screen captures as context: an assertion the owner typed is not an observation, and
presenting it as one would misrepresent its provenance.

An authored Face is matched on re-mine by member overlap rather than by its signature. If its
membership later drifts past the folding threshold, derivation starts a separate Face beside it
rather than reclaiming the authored one.

Two consequences of correcting a Face, Volume, or Root in place are worth stating plainly.

The correction keeps the object's `face_id`, because a new row would mint a new id and dangle every
child's `parent_face` and every parent's `members` entry. Identity is what holds the geometry
together, so identity is what is preserved — and the cost is that `schema_faces` carries only the
current wording. The full sequence of corrections is replayable from the `memory_deltas` audit rows,
each of which records the text it displaced, but an as-of query against `schema_faces` itself
returns the object as it reads now. Points do not share this limitation: their corrections supersede
in the ordinary way and remain fully bitemporal.

Retiring a Face does not cascade. A Volume that listed it keeps naming it in `members`, and the
member resolves to no receipt until the model is rebuilt — which the retirement schedules by
flagging the structure dirty. Rejecting one regularity is not a claim about the larger pattern
built over it, so the rebuild re-derives that pattern from what is still live rather than deleting
it outright.

## Evidence sources

Relation edges may carry the nullable triplet `source_kind`, `source_id`, and `source_receipt`.
The triplet is atomic: callers either provide all three fields or none. Activity-derived edges use
stable IDs `event:occurrence:<id>`, `event:entry:<id>`, or `event:session:<id>`.
Occurrence receipts resolve through the owner-only `event_occurrences` projection; occurrence
identity is window-specific while series identity groups only equal normalized titles and canonical
participant sets. `event:intent:<id>` is read-only compatibility for an old store.

The read-only `resolve_evidence` MCP tool and authenticated `GET /model/evidence?ref=...` endpoint
return `label` for human display and retain `reference` as the stable technical handle. Explicit
derivation edges are in `sources`, time-adjacent investigation clues in `context`, and Point version
edges in `history`. A missing retained payload stays inspectable as `status=missing`.

## Privacy and reproducibility

`export_snapshot` redacts deterministic secret/PII categories by default and writes atomically with
mode `0600`. Callers must opt out explicitly with `redact=False`. A fixed `generated_at` and fixed
`build_metadata` produce byte-equivalent model data, assuming the underlying database is unchanged.

The `model` object in loopback `/model/graph` uses the same schema but raw local
content so the owner can inspect the real model. It is not a publication export.
`HUMAN.md` uses the same raw owner-local boundary and likewise must not be
treated as a safe sharing artifact.

The synthetic contract fixture lives at
[`tests/fixtures/runtime_model/model_seed.json`](../tests/fixtures/runtime_model/model_seed.json). It
contains no screenshots or harvested user data and exercises Point, evolution Line, relation Line,
Face, Volume, Root, and receipt projection from a fresh temporary data root.
