# Coast functional parity: reuse-first correction

> Snapshot date: 2026-07-30. This revision supersedes the earlier greenfield
> estimate in this file. Persome Runtime evidence is pinned to public commit
> `1caab28a76846caaf11fd3b19bf6f1b371e312d9`; Coast evidence is pinned to
> Coast Local 1.0 build 131000 and the accompanying
> [reverse-engineering report](./coast-local-1.0-build-131000-reverse-engineering.md).
> The estimates assume one product owner using three to five coding agents in
> parallel. They are planning ranges, not delivery commitments.

## Correction

The original estimate scoped only the current public Runtime tree. That was the
wrong unit of analysis for "what Persome already has": native capture, Rewind,
permissions, lifecycle, and distribution code had been deliberately separated
into an existing product companion, while raw recall REST/UI surfaces remain
recoverable from pre-slim Runtime history.

After auditing the repo family, installed application, aggregate local capture
state, and public history, the corrected conclusion is:

> Persome's infrastructure is already built. Coast-like parity is now an
> integration and product-adjustment project, not a new capture platform.

The earlier 8–12 month estimate double-counted completed work and should not be
used.

## Corrected schedule

These ranges are cumulative calendar time from a focused start:

| Target | AI-native elapsed time | Acceptance boundary |
|---|---:|---|
| Integrated proof | 3–5 working days | Existing native capture and Rewind run against the current Persome Runtime contract under Persome naming |
| 70–80% perceived Coast parity | 5–8 working days | Automatic pixels, AX-backed context, searchable screenshot timeline, app/domain filtering, pause, and agent drill-down |
| Internal daily driver | 2–3 weeks | Seven-day dogfood, bounded media growth, sleep/wake and restart recovery, exclusions, independent pixel/text retention |
| Coast-Lite function-complete | 4–6 weeks | Dense long-lived pixels, media/frame index, OCR boxes, crop/scrub, privacy controls, and migration/recovery |
| Production beta | 8–10 calendar weeks | Function-complete build plus a final 30-day release-candidate soak, signing/notarization/update, and compatibility checks |
| Literal edge-behavior parity with 90-day evidence | 14–16 calendar weeks minimum | Deeper dynamic comparison plus an uninterrupted 90-day evidence window |

The first two rows are mainly coding and integration. The final rows are
dominated by real elapsed validation: AI can accelerate implementation, but it
cannot compress a 30- or 90-day soak.

## What is already complete

The relevant product is the union of the public Runtime and the existing native
companion, not either repository in isolation.

| Capability | Evidence-backed status | Reuse decision |
|---|---|---|
| Native macOS capture owner | Existing Swift app owns Accessibility and Screen Recording under one TCC identity | Reuse |
| ScreenCaptureKit pixels | Existing code captures the focused window and primary display with `SCShareableContent` and `SCScreenshotManager` | Reuse |
| In-process AX | Existing code has an `AXObserver`, frontmost focused-window tree traversal, deep-tree filtering, secure-field redaction, and bounded timeout | Reuse |
| AX + screenshot coordination | One single-flight capture task reads a fresh AX tree, captures pixels, and posts both in one observation; it also supplies a focused-window JPEG when AX is weak | Reuse, add explicit timing fields |
| Trusted ingest | The current Runtime already defines the bounded Swift payload and routes it through the same enrich, OCR, dedup, persistence, and session path as daemon capture ([contract](../../src/persome/api/models.py#L50-L92), [scheduler](../../src/persome/capture/scheduler.py#L421-L493), [ingest mode](../../src/persome/capture/scheduler.py#L1043-L1077)) | Reuse |
| OCR | Local subprocess-isolated OCR already returns text, geometry, and confidence, with Paddle on Apple Silicon and Vision on Intel ([capture docs](../capture.md#local-ocr-fallback)) | Reuse; persist geometry |
| Search and evidence drill-down | SQLite FTS/BM25, time/app filters, exact capture handles, optional screenshots, and optional full AX are already exposed to MCP ([capture MCP](../../src/persome/mcp/captures.py#L161-L386)) | Reuse |
| Screenshot privacy | AES-256-GCM persistence, lock/secure-input suppression, tiered retention, loopback bearer auth, and owner-local access already exist ([config](../../src/persome/config.py#L47-L91), [API boundary](../../SECURITY_PRIVACY.md#local-api-boundary)) | Reuse |
| Rewind UI | Existing native SwiftUI day view already has event/15/30/60-minute density, timeline cards, lazy screenshot loading, and full-resolution zoom | Reuse; remove product gating and rebrand |
| Raw REST surface | [Pre-slim public history](https://github.com/Persome-ai/persome-core/blob/bb3a13e7df592a1559727766357c003182ab09a8/src/persome/api/routes.py#L474-L788) contains `/captures/current`, `/captures`, `/captures/recent`, `/timeline`, `/rewind/day`, and `/rewind/screenshot`, including encrypted image reads and a one-scan day implementation | Restore thin authenticated wrappers or point the client at equivalent current MCP logic |
| Product shell | Existing SwiftUI window/menu-bar/settings/permission/daemon-lifecycle shell is complete | Reuse and narrow |
| Distribution | Existing build path includes Developer ID signing, notarization, stapling, Sparkle updates, DMG/ZIP artifacts, and release verification | Reuse |
| Personal model | Timeline, sessions, incremental modeling, evidence receipts, correction, forgetting, MCP, and `/model` are already the production Runtime path ([architecture](../../ARCHITECTURE.md#data-flow)) | Keep unchanged |

The local installed build is stronger evidence than source presence alone: it is
Developer-ID signed, links ScreenCaptureKit and Sparkle, and the local index
contains 3,814 capture rows, of which 3,326 are `capture_source="ingest"` and
3,325 identify the in-process Swift AX source. These are aggregate counts only;
no captured content was inspected or copied into this report.

The existing capture is **logically aligned, not atomic**: within one
single-flight task it reads AX first and pixels immediately afterward. That is
also the right honesty boundary for Coast. The adjustment is to record
`observation_id`, `ax_captured_at`, `pixel_captured_at`, `skew_ms`, and
freshness/completeness rather than claim impossible simultaneity.

## What actually remains

### P0 — assemble the existing pieces

Estimated: 3–5 working days, with workstreams parallelized.

1. Rebase the native companion from its older pinned Runtime contract onto the
   current public Runtime, names, token handling, paths, and health receipts.
2. Restore the small authenticated raw REST facade from public history, or adapt
   the existing native client to the equivalent current MCP/store functions.
3. Put the existing Rewind view behind the normal Persome navigation instead of
   a developer/product gate.
4. Reuse the existing permission, capture-status, pause, daemon lifecycle,
   signing, notarization, Sparkle, and packaging paths under the Persome shell.
5. Freeze PID/window/display identity at capture admission and mark or reject a
   frame if focus changes between its AX and pixel reads.
6. Add contract tests proving that one observation preserves AX, pixels, OCR
   provenance, timestamps, and an evidence handle end to end.

This produces an integrated proof without inventing a new capture host,
timeline, updater, or installer.

### P1 — make pixels dense without making modeling dense

Estimated: another 2–4 working days for the first useful version.

The current native path is event-driven with a ten-minute heartbeat. Also, the
Runtime's consecutive fingerprint intentionally excludes screenshots and raw AX
([fingerprint](../../src/persome/capture/scheduler.py#L785-L806)). Therefore
video, canvas, images, and other pixel-only changes can disappear even though
the macOS acquisition code already exists.

Decouple the existing AX event lane from a separate raw-pixel clock:

```text
existing AXObserver --> latest versioned AX snapshot ---------.
                                                            |
existing ScreenCaptureKit clock --> raw frame + AX reference +--> Rewind/search
                                      |
                                      `--> semantic sampler --> existing ingest --> model
```

The raw lane should use a fixed two-second or activity-adaptive clock, a
perceptual/pixel hash, bounded single-flight backpressure, and explicit dropped
frame counters. Pixel persistence must not fail merely because AX is unavailable.
AX stays event-driven; a frame references the newest compatible snapshot and
records its age rather than rewalking a deep tree every two seconds. Only focus
changes, meaningful AX changes, OCR-relevant frames, or periodic samples should
enter the existing semantic pipeline. Do not make 14,400 frames per eight-hour
day drive timeline/session/LLM work.

For the first daily-driver build, encrypted JPEG plus independent retention is
enough. HEVC segmentation is an optimization to earn with measured disk and
energy data, not a prerequisite for perceived parity.

### P2 — close product gaps that users can notice

Estimated: 3–7 working days, partly parallel with P1.

- Persist OCR boxes and confidence instead of retaining only flattened text.
- Normalize domain and bundle metadata and add date/app/domain search filters.
- Add app/domain exclusions, private-window conservative handling, timed pause,
  inactivity pause, and one clear egress/Airgap control.
- Add display ID, pixel dimensions, focused-window bounds, and capture skew.
  Enumerate all visible windows only if real use shows that background-window
  recall is necessary.
- Expose storage growth, retention, last successful frame, drop rate, and
  permission health in the existing settings/status UI.

### P3 — optional exact-Coast work

Estimated: another 2–4 engineering weeks if literal implementation/retention
parity is required.

- HEIC/HEVC segments and a logical frame-to-media index;
- longer independent pixel/text retention and compaction;
- complete multi-display/window geometry and crop export;
- representative segment cover/sample and usage analytics;
- more exact browser private-mode and window-occlusion behavior;
- a persistent content-addressed AX graph, but only if profiling proves that
  fresh per-observation trees are the bottleneck.

None of these is required to prove the core daily loop. Coast's internal storage
or AX representation is not itself a user-facing acceptance criterion.

## AI development plan

One person should own contracts, merge order, real-Mac verification, and product
decisions. Three to five agents can work independently:

| Agent lane | First deliverable |
|---|---|
| Native capture | Dense/adaptive clock, pixel hash, timing/skew fields, raw/semantic split |
| Runtime/store | Raw frame schema/spool, retention, search/domain aggregation, restored REST facade |
| Product UI | Rebrand/extract Rewind, filters, privacy/status controls |
| Packaging | Current Runtime bundle, lifecycle, signing/notarization/Sparkle verification |
| Red team/tests | Coast acceptance script, migration/recovery tests, resource telemetry |

Integrate at least daily. Do not let each lane independently invent frame IDs,
retention semantics, or authorization.

## Validation that still takes real time

The coding estimate is short; the evidence schedule is not:

1. **48 hours:** capture-to-search latency, AX/pixel skew, drops, CPU, RSS,
   wakeups, energy, encrypted disk growth, and delete/retention correctness.
2. **7–14 days:** daily-driver use across lock/unlock, sleep/wake, restart,
   crash recovery, network loss, browser changes, and display changes.
3. **30 days:** frozen release candidate, clean install, upgrade, rollback,
   uninstall, TCC stability, notarization, Sparkle, and bounded storage.
4. **90 days:** only for a literal long-duration parity claim.

If capture, frame schema, media storage, encryption, or retention changes during
a soak, restart the affected evidence window.

## Final assessment

Persome does not need to reproduce Coast's codebase. The practical target is:

> Reuse the already-running native AX/ScreenCaptureKit/Rewind product layer, add
> a dense but isolated raw-pixel lane, and keep Persome's evidence-linked model
> as the semantic layer above it.

That makes the realistic answer **days to a convincing integrated build, two
to three weeks to an internal daily driver on the existing encrypted-JPEG
substrate, four to six weeks for function-complete Coast-Lite parity, and eight
to ten calendar weeks for a production beta with a real 30-day evidence
window**.
