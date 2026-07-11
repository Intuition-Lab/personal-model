# HTTP API

Persome exposes a deliberately small loopback HTTP API from the same ASGI
application that hosts MCP. HTTP owns health, trusted capture ingestion, the
model explorer, and optional Chat. Memory retrieval and correction live in MCP.

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
| GET | `/model` | Open the offline Point/Line/Face/Volume/Root explorer. |
| GET | `/model/graph` | Read the canonical versioned model snapshot. |
| GET | `/model/node?id=...` | Resolve a snapshot Point ID or relation endpoint to receipts and its relation tree. |

The model page renders snapshot Points and Lines directly, then derives the
Face, Volume, and Root hierarchy from their declared `members`. It loads its
pinned Three.js modules from `/model/assets/*`; those package resources are
intentionally omitted from OpenAPI.

`/status.data.llm_profile` reports the effective provider, protocol, model,
endpoint, key variable name, credential presence, and legacy-migration state.
It never returns the credential value. Provider network probes run only for
the explicit `GET /status?check_models=true` request and are cached briefly.
`/status.data.ocr` reports the configured tier, Runtime and model availability,
kill switch, Screen Recording, and effective readiness. `/health` exposes only
the compact OCR state because it is the unauthenticated liveness route.

## Chat routes

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/chat/sessions` | List or create local chat sessions. |
| GET, DELETE | `/chat/sessions/{session_id}` | Read or delete a session. |
| GET, POST | `/chat/sessions/{session_id}/messages` | Read messages or stream a reply. |

Chat consumes the same memory and provenance interfaces as MCP. It is not a
second model store. Shell, arbitrary filesystem, and Web tools are omitted by
default. The terminal client requires `[chat] unsafe_local_tools_enabled =
true` plus exact one-shot approval for each call. HTTP Chat has no trusted
approval channel and refuses these calls. Only user-installed skill Markdown
can load as guidance; model-generated `memory/skills` content is excluded.
Configured external MCP servers are separate explicit trust grants and their
calls also need approval.

There is no browser Chat page in this repository. `persome chat` is the shipped
interactive client; the routes above support trusted local product clients.

## Model contract

`GET /model/graph` wraps a `model` object with the same schema returned by the
MCP `get_model_snapshot` tool and CLI `persome model export`:

```text
schema_version, generated_at, build,
points, lines, faces, volumes, root, receipts, stats
```

Every Line derived from activity carries `source_kind`, `source_id`, and
`source_receipt`. Legacy `event:<id>` identities are normalized to
`event:intent:<id>` and are read only when an old `intents` table exists.

The loopback viewer receives raw local graph/model detail so its owner can
inspect the real person model. `persome model export` and MCP
`get_model_snapshot` apply deterministic redaction by default; `/model/graph`
is not a publication endpoint.

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
- `/captures/ingest` assumes a trusted local producer that obtains the owner
  token through an approved local secret channel and sends the bearer header;
  it is not a public upload API.
- Model assets and graph data load from the same loopback server with no CDN dependency.
- LLM and embedding egress only use endpoints configured by the user.
- Unknown and removed product/admin routes return `404`.
