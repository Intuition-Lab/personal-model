# Gator quality boundary

Gator is the Runtime's cross-stage admission policy, not a second capture daemon or a parallel
model writer. It keeps raw local observation, normalized activity, and promoted personal-model
geometry distinct so recall-oriented capture does not imply first-sighting memory.

```text
capture evidence
  -> privacy boundary
  -> S0 event/content suppression
  -> S1 focused content
  -> timeline provenance
  -> reducer / memory-delta evidence gate
  -> independent-session Point candidates and retry-safe Lines
  -> independent-input promotion for Face, Volume, Root
```

## Current enforced gates

- Secure or paused capture follows the existing privacy boundary. Gator never restores content
  removed there.
- Successful writes through the live capture runner publish a bounded content-fingerprint receipt.
  The latest committed head survives daemon restart while its backing raw capture exists, window
  title remains part of exact context identity, and the receipt is never used as a substitute for
  the explicit identity of a mobile event. Raw retention removes the matching receipt; direct
  no-runner writes durably invalidate older heads and deliberately fail open instead of minting one.
  The isolated `capture-once` diagnostic holds the Runtime lifetime lock and cannot race the daemon.
- Focus transitions prefer the more specific focused-window notification: Activation waits one
  short trailing interval, Focus replaces it, and Focus followed by Activation remains one capture.
  A newer cross-surface focus, click, or text signal invalidates every older pending Activation—even
  if the newer capture is rate-limited—so a delayed trigger cannot label another app's later screen;
  same-type Focus events still reach content dedup because they can name real windows. Shutdown
  waits for a capture callback that has already crossed the admission boundary.
- The capture worker reconciles each queued watcher trigger with the surface actually sampled. A
  stable-surface or explicit-title mismatch becomes a non-actionable `QueuedSurfaceRefresh`, keeps
  only the sanitized source event type, discards stale pointer details, and updates the session from
  the persisted surface rather than the event-time app.
- Timeline requires sanitized content signal before calling its LLM. Metadata-only windows are
  recorded but are not model evidence.
- Timeline fallback and explicit-empty outcomes carry a durable `normalization_status`; downstream
  model consumers fail closed on ineligible statuses while still advancing watermarks.
- Exact normalized timeline duplicates and generic `active / worked in window` records are removed
  before an `llm` block becomes eligible.
- A producer that omits timeline provenance gets `unknown` and is ineligible; only the database
  migration assigns `legacy` to rows whose earlier origin cannot be reconstructed.
- The current constellation renders only the Point chain head valid at the selected cutoff.
  Historical Points and evolution Lines remain audit-searchable after they occurred.
- Canonical relation endpoints reuse an unambiguous current entity Point. A context node is created
  only when no safe Point match exists; reserved `self` and ambiguous identities fail closed.
  Context case/width/whitespace variants share one display node, and duplicate projected strokes
  render once without deleting their separate audit Lines.
- A canonical session-window claim is acquired before memory-delta extraction. Timezone-equivalent
  bounds share one key; a lease/token compare-and-swap prevents a stale worker from binding a second
  payload after recovery.
- Open active/shadow relation Lines have one canonical database key. `knows` is symmetric; all other
  predicates remain directed. Legacy collisions stop relation migration without merging rows or
  summing observation counts.
- Windowed events use a stable occurrence ID derived from session, canonical window, and item key.
  Recurring events share a narrower series ID only when normalized title and canonical participants
  agree. Legacy title-hash endpoints remain untouched instead of receiving fabricated provenance.
- A parent delta is `applied` only when deterministic apply reports no item errors. Partial failure
  remains retryable and visible as `failed`.
- Each persisted delta seeds an ordered item ledger in the same transaction as its payload. A
  leased item resumes independently after failure; additive and relation-ending Line effects carry
  a unique database receipt in the same transaction as their graph mutation, so a crash after the
  effect but before the item acknowledgement cannot count or create it twice. Each item also records
  whether it changed geometry, preserving the structural rebuild signal if the process dies before
  publishing the parent status.
- A new entity or assertion is recorded as a candidate on first sighting. It becomes a durable Point
  only after the same canonical candidate appears in two known, distinct sessions. More windows in
  one session add audit evidence but do not satisfy independence; invalid or unknown session context
  fails closed. Repeated same-window entity candidates are coalesced by canonical identity before
  the immutable item ledger; contradictory active/ended claims drop as a group, and nested
  assertion/relation/event references share the deterministic winner. Already established Points
  and explicit owner decisions retain their authority.
- PersonGraph treats a Point-backed person observation as already durable evidence: it may discover
  or adopt the roster identity, but it does not copy the Point body into a second `person-event`.
  If one raw identity Point arrives after one receipt-bearing derived roster head for the same
  person file, the two are merged through one atomic multi-predecessor supersession without
  incrementing interaction sightings. Point maintenance runs before entry interactions with a
  separate bounded budget; the first same-file fact after an entry-created roster supplies one
  identity bridge without becoming a sighting or copying every later receipt. Durable source events
  remain idempotent through slug/display-name adoption. Ambiguous raw identities or roster heads,
  plus unreceipted legacy heads, fail closed and are left for explicit legacy repair.
- Face, Volume, and Root producers bind canonical input hashes to UTC-day receipts. Re-running the
  same producer/input/day does not add an observation, footprint, promotion vote, Root supersession,
  or duplicate LLM call on the sequential build path. A later UTC day is a new resample.

## Evidence and compatibility

Raw captures retain their configured local retention policy. A TimelineBlock is a bounded evidence
projection, not a promise that every frame is stored forever. Existing blocks migrate to `legacy`
because their original normalization path cannot be reconstructed safely. No migration guesses from
entry wording or silently deletes prior model state.

Pre-item-ledger memory deltas keep the same conservative boundary. A legacy row explicitly marked
`not_requested` can take its historical context-free apply path. A legacy `pending` or `failed` row
with Point/Line/event effects is not replayed automatically: some effects may already have committed,
and inventing a receipt after the fact could count an additive Line twice. It remains failed and
owner-auditable until an explicit repair can establish what happened. The affected window is
quarantined and its session watermark advances, so one irrecoverable historical receipt cannot block
all later, fully receipted windows.

An `llm` block has passed content-signal and response-shape checks; it is not yet a per-entry
cryptographic grounding guarantee. Source receipts plus locally verified evidence spans are part of
the next promotion hardening slice.

## Next hardening slices

1. Per-entry source receipts and evidence-span validation before normalized claims can promote.
2. Durable source-event receipts and trigger-to-captured-content snapshot alignment when queueing
   delays observation.
3. An owner-visible dirty-data repair workflow for legacy open-Line collision reports.
4. Candidate/receipt diagnostics in owner-facing model health surfaces without exposing raw capture
   content.
5. A batch/indexed PersonGraph identity lookup so large legacy stores do not rescan every current
   Point for each bounded source event during enrichment.
