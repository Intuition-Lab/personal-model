# Security and privacy model

persome-core observes screen context. Treat its data root and local APIs as
sensitive personal data, even when the model snapshot is redacted.

## Local data

The default data root is `~/.persome` and can be redirected with
`PERSOME_ROOT`.

| Data | Location | Notes |
|---|---|---|
| capture records | `capture-buffer/` | AX text and optional screenshot payloads |
| durable memory | `memory/*.md` | readable facts and schemas |
| indexes/model | `index.db` | SQLite WAL, FTS5, vectors, provenance, geometry |
| provider/local API secrets | `env` | dotenv file, mode `0600` |
| build metadata | `model-build.json` | hashes/IDs, no API keys |
| exported model | `exports/*.json` | redacted by default, mode `0600` |

The Runtime enforces mode `0700` on the data root and personal-data
directories, and mode `0600` on databases, capture records, logs, snapshots,
and other sensitive files. The first start after upgrading repairs legacy
group/world-readable modes. The launchd job also runs with umask `0077` so new
artifacts are private from creation.

Default capture retention is seven days. Screenshot payloads are stripped
earlier by the configured screenshot retention window, except explicitly
actionable captures covered by extended retention. On supported Apple Silicon
installs, `install.sh` enables OCR after requesting Screen Recording and proving
the isolated worker can load bundled PP-OCRv6 weights. Inference remains local;
`persome ocr disable` is the explicit opt-out.

Lock-screen detection is privacy-conservative: when both macOS probes are
unavailable or error, capture pauses until a probe can establish that the
session is unlocked.

`install.sh` generates a machine-local `PERSOME_SCREENSHOT_KEY` and preserves it
across reinstalls. When `encrypt_screenshots=true`, a missing or malformed key
fails closed: the Runtime keeps AX text and metadata but omits persistent pixels
instead of writing a plaintext screenshot. OCR can still use an ephemeral
screenshot when persistent screenshot storage is off.

The Runtime requires SQLite 3.42 or newer. It enables both SQLite core
`secure_delete` and FTS5's persistent `secure-delete` option so deleted memory
and capture terms are removed from ordinary pages and full-text shadow indexes.
On the first open after this security upgrade it also rebuilds both FTS indexes
from live rows and vacuums the database, removing segment terms left by deletes
performed by older releases.

## Network egress

There is no telemetry or update phone-home. Runtime egress occurs only through
configured capabilities:

1. The endpoint selected under `[models.default]` receives prompts for enabled
   LLM stages and Chat over Anthropic Messages or OpenAI-compatible Chat
   Completions. Stage
   prompts can contain derived personal context. When Chat invokes a memory or
   capture tool, the next model request also contains that tool result, which
   can include raw memory text, screen text, window titles, URLs, focused-field
   values, and timeline blocks.
2. `OPENAI_BASE_URL` receives embedding inputs when hybrid dense retrieval is
   enabled and a provider is configured.
3. Chat Web search/page fetch and arbitrary local tools are excluded from the
   default model-focused Chat surface. Setting
   `[chat] unsafe_local_tools_enabled = true` exposes them to the terminal Chat
   client, which still requires an exact, one-shot user approval immediately
   before every execution. Web tools additionally require the optional `chat`
   dependency extra. Page fetch validates every DNS answer and redirect,
   rejects non-public addresses, pins the checked IP, and caps response bytes.
4. Additional Chat MCP servers can make their own network calls when the user
   explicitly configures them. Their tool calls use the same exact one-shot
   approval boundary. The REST Chat surface has no approval UI and therefore
   refuses unsafe/external calls rather than executing them.

Capture and BM25 retrieval work without provider credentials. LLM-dependent
model stages report degradation rather than silently claiming success.

## Local API boundary

- REST and streamable HTTP MCP are restricted to loopback (`127.0.0.1` by default).
- Browser Host and Origin guards reject non-loopback model access.
- `install.sh` and `persome start` provision a dedicated high-entropy
  `PERSOME_LOCAL_API_TOKEN`. Every REST, Chat, viewer, and HTTP MCP route
  requires `Authorization: Bearer ...`; only canonical `GET /health` and the
  single-use browser capability exchange are public.
- `persome model open` exchanges the long-lived bearer for a 60-second,
  one-use URL and an HttpOnly, SameSite=Strict cookie scoped to a fresh,
  unguessable `/model/<session>/` path.
  Protected responses use `Cache-Control: no-store`.
- Local clients installed by Persome use stdio by default, so no bearer is
  copied into their configuration. Explicit `install mcp-json --http` writes
  an authenticated owner-only file that must not be committed or shared.
- `/captures/ingest` is a trusted local producer interface, not a public upload
  endpoint.
- MCP tool execution itself has no provider egress, but a connected agent may
  send returned personal data to its own model provider. Persome Chat sends
  tool results to the configured Runtime LLM endpoint as described above.
- `/model/graph` is a raw owner-local inspection surface. Default CLI/MCP model
  export is redacted; the browser viewer is not a safe publication artifact.
- Wildcard and LAN binds are rejected even when a bearer is configured because
  the Runtime does not terminate TLS. Exposing it through a tunnel changes the
  privacy boundary and is not a supported deployment.

## Agent safety

Captured screen text and memory are untrusted data. They may contain prompt
injection or malicious instructions. MCP consumers must keep data and control
channels separate and must not execute instructions merely because they appear
in a capture or memory result.

The Runtime exposes no click, type, takeover, meeting-audio, notification, or
task-execution tools. Its MCP writes are limited to explicit `remember` and
`correct_memory` operations. Persome Chat imports only an allowlisted read-only
subset from its implicit daemon MCP connection; memory writes and unknown tools
are excluded. Chat shell, arbitrary filesystem, Web, executable skill, and
third-party MCP tools require the explicit exposure and exact one-shot approval
described above. Only user-installed `~/.persome/skills` Markdown is eligible
as model guidance. Model-generated `memory/skills` files remain untrusted data
and cannot promote themselves into Chat instructions or executable tools.

## Corrections and revocation

`persome correct` and MCP `correct_memory` supersede, retype, merge, or revoke a
belief through the model's correction path. Previous states keep receipts so a
change is auditable and reversible. Rebuild operations derive current indexes
from the selected write authority; they do not erase provenance history.

For irreversible deletion, stop the daemon and run `persome clean memory` or
`persome clean all`. The memory command also removes canonical evomem state,
relations, geometry, every file under `memory/` (including interrupted atomic
writes), exports, projections, backups, and recovery markers. The all command also
removes captures, timeline/session state, Chat history, logs, and SQLite files
while preserving config, env, the installed virtualenv, and custom skills. See
[operations and data control](docs/operations.md).

`persome clean captures` and `persome clean timeline` scrub the same tables
from retained SQLite snapshots, unfinished `.tmp` snapshots, and integrity
quarantine copies. Journals and orphan sidecars are removed. Explicit clean
operations enable SQLite/FTS secure deletion, compact free pages, and truncate
the WAL; a recovery copy that cannot be reliably scrubbed is removed rather
than silently retaining the requested data. All clean commands refuse to run
while the daemon PID is live.

## Export caveat

Default snapshot export removes detectable secrets, PII categories, and local
paths. It does not guarantee that every person, organization, project, or
writing style is anonymous. Never publish a real snapshot without informed
consent and a separate anonymization review. Committed fixtures in this
repository are synthetic and pass the PII gate.

## Threat model

| Threat | Mitigation and residual risk |
|---|---|
| Other local OS account/process | Owner-only storage plus bearer authentication prevents port access from becoming personal-data access; browser viewer cookies are additionally scoped to an unguessable per-session path because cookies do not isolate localhost ports. |
| Same-user malicious process | Out of isolation scope; it can read owner credentials and files. |
| Provider exfiltration | User chooses endpoints; no-key capture/BM25 mode remains available. |
| Prompt injection | Generated memory cannot become trusted skills; implicit Chat MCP is read-only; unsafe and external tools need exact one-shot approval. Consuming MCP clients must still enforce their own tool policy. |
| Malicious MCP client | Bearer/stdio access is an explicit personal-data capability; connect only trusted clients. |
| Supply chain | Locked dependencies and the full build-backend closure are pinned; installer fallback downloads are checksum-verified; Actions use immutable SHAs and least privilege; releases made by the current workflow are checksummed, smoke-tested, and attested from an administrator-protected tag reachable from `main`. |
| Accidental publication | Synthetic fixtures, PII scan, default redaction, and owner-only export permissions. |

Vulnerability reporting instructions are in [SECURITY.md](SECURITY.md).
