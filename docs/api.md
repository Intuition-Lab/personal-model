# HTTP API

Persome exposes a deliberately small loopback HTTP API from the same ASGI
application that hosts MCP. HTTP owns health, trusted capture ingestion, and
the model explorer. Memory retrieval and correction live in MCP.

The generated contract is [`openapi.json`](../openapi.json). Regenerate it after
route or model changes:

```bash
uv run python scripts/regen_openapi.py
```

`tests/test_openapi_drift.py` requires the committed file to byte-match the live
runtime schema.

## Runtime routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness plus compact OCR readiness (`ok` or `degraded`). |
| POST | `/auth/browser-bootstrap` | Exchange the bearer for a 60-second, one-use viewer URL. |
| GET | `/permissions` | macOS Accessibility and Screen Recording state. |
| GET | `/status` | Daemon, capture, OCR, session, memory, and provider status. |
| POST | `/captures/ingest` | Ingest one bearer-authenticated capture from a trusted local producer. |
| POST | `/health-events/import` | Atomically import up to 1,000 normalized wearable/health changes from a trusted local connector. |
| POST | `/mobile/events/ingest` | Ingest a paired mobile event; `Idempotency-Key` must equal `event_id`. |
| GET | `/model` | Open the offline Point/Line/Face/Volume/Root explorer. |
| GET | `/model/graph` | Read the canonical versioned model snapshot. |
| GET | `/model/evidence?ref=...&as_of=...` | Resolve a model ID or receipt into direct sources and separately labeled nearby context, optionally bounded by an ISO 8601 historical cutoff. |
| GET | `/model/node?id=...` | Resolve a snapshot Point ID or relation endpoint to receipts and its relation tree. |
| POST | `/model/edit` | Apply one owner correction to a modeled object: rewrite its wording or reject it. |

### Wearable and health event import

`POST /health-events/import` is the local connector boundary for Apple HealthKit,
Health Connect, vendor APIs, and file/BLE adapters. Every event needs a stable
provider `event_id`; repeated imports are accepted and counted as duplicates.
If the same provider ID arrives with changed normalized content, it corrects the
existing observation and is counted as `corrected`; an identical replay remains
a `duplicate`. `deleted_events` contains provider-scoped IDs removed by an
anchored source query. Deletions and new/corrected events commit in one SQLite
transaction, so a connector can persist its next source anchor only after this
request succeeds without leaving stale observations behind.
Times must be ISO 8601 values with an explicit offset. The Runtime stores raw
normalized observations and provenance locally; it does not treat consumer
device measurements as medical diagnoses.

The route has a 2 MiB HTTP and semantic payload ceiling, a combined 1,000-change
limit, 4 KiB UTF-8 string-value limit, and 64 KiB metadata limit per event.
NaN and infinities are rejected before FastAPI validation can echo invalid JSON.

The model page renders snapshot Points and Lines directly, then derives the
Face, Volume, and Root hierarchy from their declared `members`. It loads its
pinned Three.js modules from `/model/assets/*`; those package resources are
intentionally omitted from OpenAPI.

Receipt buttons in the viewer call `/model/evidence`. The response is a
progressive-disclosure node with `sources` for explicit stored lineage and
`context` for time-adjacent captures. Human-readable `label` values let clients
present evidence without exposing internal IDs; Point version links are kept in
`history`. The local viewer organizes this as Overview, Evidence, and History,
with drill-down breadcrumbs and raw receipts under technical details. An unknown
or retention-expired payload returns `status=missing` while preserving the
original receipt for audit.

`/status.data.llm_profile` reports the effective provider, protocol, model,
endpoint, key variable name, credential presence, and legacy-migration state.
It never returns the credential value. Provider network probes run only for
the explicit `GET /status?check_models=true` request and are cached briefly.
`/status.data.ocr` reports the configured tier, Runtime and model availability,
kill switch, Screen Recording, and effective readiness. `/permissions` does not
infer Accessibility from the terminal or Python daemon: in daemon mode it runs
the source-versioned helper and optional watcher self-checks plus the Runtime's
Screen Recording preflight. In trusted-ingest mode those OS permissions belong
to the producer and are reported as not applicable to the daemon. `/health`
exposes only compact OCR state because it is the unauthenticated liveness route.

## Model contract

`GET /model/graph` wraps a complete `model` object with the same schema written
by CLI `persome model export`:

```text
schema_version, generated_at, build,
points, lines, faces, volumes, root, receipts, stats
```

Every Line derived from activity carries `source_kind`, `source_id`, and
`source_receipt`. Legacy `event:<id>` identities are normalized to
`event:intent:<id>` and are read only when an old `intents` table exists.

The loopback viewer receives raw local graph/model detail so its owner can
inspect the real person model. `persome model export` applies deterministic
redaction by default; `/model/graph` is not a publication endpoint.

MCP `get_model_snapshot` reads the same live generation but returns a separate
bounded envelope: the default overview carries compact Root/Face/Volume data
and canonical totals, while Points, Lines, Faces, Volumes, Root, and receipts are
available in pages or by exact ID. Each page's `content[0].text` JSON payload is
capped at 64 KiB; JSON-RPC framing and escaping are outside that payload
budget. Full snapshots are exported to a local file instead of being placed in
one MCP result.

The authenticated viewer polls for model changes, but the Runtime keeps one
owner-local graph payload in memory for at most 15 seconds and makes refresh
single-flight. This bounds repeated snapshot work across polling tabs without
writing raw graph content to another file. The browser also coalesces overlapping
polls and turns a request that exceeds 45 seconds into an explicit retry state.
Its refresh identity includes every displayed Line field and the compact
`index_health` note, so an in-place relation correction or health transition
cannot be mistaken for an unchanged graph.

The viewer omits `as_of` at **Now**. During time travel it sends the selected
cutoff on every nested evidence drill-down. The resolver then omits a
`next_version` until that successor's effective start and excludes nearby
captures after the cutoff; a successor whose start cannot be established is
hidden rather than exposed early. This bounds server-returned evidence instead
of relying only on cards already filtered in the browser.

## Security boundary

- The server is restricted to loopback and defaults to `127.0.0.1`; wildcard
  and LAN binds are rejected even with a bearer because the server has no TLS.
- Origin and host guards reject non-loopback browser access.
- Every API/MCP route except canonical `GET /health` requires the dedicated
  local bearer. The generated OpenAPI contract declares `LocalBearer` globally;
  the browser viewer may instead use the bearer-derived capability below.
- Use `persome model open`; the viewer bootstrap never puts the long-lived
  bearer in a URL. It exchanges the one-use nonce for an HttpOnly cookie scoped
  to a fresh unguessable viewer path (localhost cookies have no port boundary),
  and protected responses are not cacheable.
- The viewer capability carries write authority for `POST /model/edit`, because
  the viewer is the owner's own correction surface. The methods it accepts are
  an explicit allowlist rather than an omission. What makes that safe is the
  combination the capability already relies on: the cookie is `HttpOnly` and
  `SameSite=Strict` so no cross-site page can drive it, the path token must
  match the cookie, the capability expires, the listener is loopback-only, and
  the origin guard runs in front. Bearer holders are unaffected.
- `/captures/ingest` assumes a trusted local producer that obtains the owner
  token through an approved local secret channel and sends the bearer header;
  it is not a public upload API.
- `/mobile/events/ingest` is also loopback-only. The paired-device bridge
  terminates pinned TLS and expiring device sessions, then forwards validated
  events with the local bearer; the bearer is never provisioned to the phone.
  Runtime receipts reserve `(device.id, event_id)` for 90 days and return the
  original capture identity on a matching retry; different content under the
  same identity is a `409`. `captured_at` must include a timezone and is stored
  as owner-reported provenance alongside the Runtime's separate `received_at`.
- Model assets and graph data load from the same loopback server with no CDN dependency.
- LLM and embedding egress only use endpoints configured by the user.
- Unknown and removed product/admin routes return `404`.
