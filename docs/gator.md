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
  -> Point and Line
  -> independent-evidence promotion for Face, Volume, Root
```

## Current enforced gates

- Secure or paused capture follows the existing privacy boundary. Gator never restores content
  removed there.
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

## Evidence and compatibility

Raw captures retain their configured local retention policy. A TimelineBlock is a bounded evidence
projection, not a promise that every frame is stored forever. Existing blocks migrate to `legacy`
because their original normalization path cannot be reconstructed safely. No migration guesses from
entry wording or silently deletes prior model state.

An `llm` block has passed content-signal and response-shape checks; it is not yet a per-entry
cryptographic grounding guarantee. Source receipts plus locally verified evidence spans are part of
the next promotion hardening slice.

## Next hardening slices

1. Per-entry source receipts and evidence-span validation before normalized claims can promote.
2. Candidate-state Point/Line promotion based on independent session receipts instead of ordinary
   first sighting.
3. Item-level apply receipts, including exactly-once additive Line reinforcement after a process
   crash. Window-level claims are already enforced but cannot prove every effect committed once.
4. An owner-visible dirty-data repair workflow for legacy open-Line collision reports.
5. Independent input receipts for Face, Volume, and Root resamples.
