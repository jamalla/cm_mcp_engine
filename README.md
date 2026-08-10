# cm_mcp_engine

**The hands.** Turns approved contracts into runnable code, sandboxes it, caches it, and serves the
result as MCP tools. Makes no decisions — it is handed a tool name and arguments and executes them.

Contracts are authored and reviewed in [`cm_mcp_contracts`](../cm_mcp_contracts). The routing agent
and UI live in [`cm_mcp_agent`](../cm_mcp_agent) and reach this service over MCP.

```bash
uv sync --extra dev
uv run pytest                 # 66 tests, no network, no ports
pwsh scripts/dev.ps1          # mock upstream :8787 + FastMCP :8765
pwsh scripts/dev.ps1 -Stop
```

## Where contracts come from

Most explicit wins, so a developer, CI, and a deployment each get the source they need:

| # | Source | For |
|---|---|---|
| 1 | `CM_REGISTRY_FILE` | an explicit registry artifact |
| 2 | `CM_CONTRACTS_DIR` | an explicit contracts directory |
| 3 | `../cm_mcp_contracts/contracts` | auto-detected sibling checkout — local development |
| 4 | `./registry.generated.json` | the version pinned into this repo — deployment |

The engine prints which one it resolved at startup and reports it from `list_contracts`, because a
catalog that looks wrong is almost always a source that is not what you assumed.

`registry.generated.json` is absent until `consume-registry.yml` opens the first pin PR. That is the
honest state of a fresh clone: this engine serves the registry the contracts repo published, and
nothing until it has.

## Upstreams live here, not in contracts

A contract names an upstream — `binding.http.api: "salla"` — and stops there. It carries no host, no
token, and no way to name one. Both live in `UPSTREAMS` in [`cm_engine/config.py`](cm_engine/config.py):

| | |
|---|---|
| `salla` | `https://api.salla.dev/admin/v2`, bearer `SALLA_ACCESS_TOKEN` |

This is the trust boundary. A contributor writes a contract; they do not get to decide where a
merchant's OAuth token travels, and they cannot pin a host that later drifts. Adding an upstream is a
change to that table, reviewed here, by the people who hold the credentials.

With `DEV_OFFLINE=1` (the default) every upstream resolves to `cm_engine/mock_upstream.py` instead —
same envelope, same pagination, same error shape, so the generated code is identical either way.
Credentials go in `.env`; see `.env.example`.

## What it does with them

- **code_mode** — deterministic template fill, no LLM. The contract already declares the endpoint,
  the parameter mapping, the envelope and the failure modes, so generation is repeatable and
  byte-stable, which is what makes the code cache trustworthy. The contract's regexes are compiled
  into the generated source as real guards.
- **request building** — path placeholders, query parameters with array styles (`single`, `bracket`,
  `repeat`, `csv`) and pinned constants, and bodies in `none` / `passthrough` / `mapped` mode.
  camelCase tool arguments become the upstream's wire names on the way out.
- **response handling** — unwraps `response.dataPath`, keeps the `pagination` object beside it, and
  projects the payload down to the fields `interface.response.schema` promised, so a chatty upstream
  cannot leak a new field to an agent.
- **failure handling** — a status outside `successStatuses` becomes the sentence the contract wrote
  for it ("no category exists with this id"), plus the upstream's own message, plus any per-field
  errors, plus whether retrying may help. An undocumented status says so.
- **sandbox** — subprocess, hard timeout, environment scrubbed to only the upstream token this tool's
  own contract implies. **Not a security boundary** (see gaps below).
- **two caches** — code keyed by `name@version`, so a version bump retires stale code for free;
  results keyed by `name@version` + the args `caching.keyBy` names, written *only* when the tool is
  read-only and cacheable. A destructive tool is never cached, and that rule lives in one function.
- **stage events** — each pipeline stage is emitted as an MCP log notification, which is how the
  agent's UI renders a live trace. On a cache hit `code_generated` and `executing` are not emitted
  at all, because they did not happen.

MCP's `ToolAnnotations` are served straight from `interface.annotations` under MCP's own field names,
so what a client is told matches what the review approved. `readOnlyHint` is also what the result
cache gates on — one declaration, read in one place.

## Validation: schema vs executability

This repo deliberately does **not** re-validate contracts against the JSON Schema. That is the
contracts gate's job, and duplicating the rulebook would give us two versions of it to disagree.

What it checks instead is the question the other repo cannot answer: **can this engine run it?**

```bash
uv run python scripts/check_registry.py registry.generated.json
uv run python scripts/check_registry.py --contracts-dir ../cm_mcp_contracts/contracts
```

```
  OK  list_categories@1.0.0    http    9664 chars

  - list_categories: skipped -- binding.http.api 'shopify' is not an upstream this
    engine is configured for (configured: ['salla'])
```

Refused here, and nowhere else: an unconfigured `api`, an unregistered `builtin://` handler, an
unsupported `binding.type`, a `{placeholder}` with no parameter mapping, a missing `dataPath`, a
`kind` this engine cannot run.

`consume-registry.yml` runs exactly this against every newly published registry, then runs the MCP
surface tests against it, and only then opens a PR pinning it. A contract that is valid upstream and
unrunnable here fails a workflow instead of a demo. Merging that PR is manual: it changes which tools
this service serves.

## Adding a capability

A new upstream, a new `binding.type`, or a new `builtin://` handler is a change **here**, and it must
land before a contract using it merges upstream — otherwise consume-registry rejects the registry.
Handlers go in `cm_engine/engine/builtins.py`, keyed by the exact `builtin://name` a contract
references; upstreams go in the `UPSTREAMS` table.

## Tests

Unit tests run against `tests/fixtures/contracts/` — a pinned set, not a sibling checkout, so this
repo tests green in a clone of itself. `tests/test_http_binding.py` executes the generated code for
real against the offline upstream, because codegen producing parseable Python proves nothing about
whether the request it builds is the one the contract described.

`tests/test_mcp_surface.py` is deliberately registry-agnostic: `consume-registry.yml` runs it against
a *candidate* registry whose tool list is whatever was just merged upstream, so nothing in it may
name a particular tool. It asserts the contract-to-MCP mapping and the shape of the pipeline trace,
which hold for a contract it has never seen.

`tests/test_wire_contract.py` pins the stage-event envelope shared with `cm_mcp_agent`; the mirror
of that file lives in that repo. Change the shape in one place and one of the two goes red — which
is the only thing keeping two repositories honest about a protocol they do not share code for.

`tests/test_spike_fastmcp.py` pins the four FastMCP assumptions the live trace rests on. If an
upgrade breaks the demo, that file fails first.

## POC gaps

- **The sandbox is not a security boundary.** A subprocess with a timeout and a scrubbed
  environment stops accidents and runaway loops, not untrusted code.
- **One credential per upstream.** A real deployment resolves the *installing merchant's* token per
  call; this reads one token from the environment.
- **`retryable` is surfaced, not acted on.** The engine reports that retrying may help and leaves the
  decision to the caller.
- **`dependencies` is declared but not resolved** — the schema models it; the engine ignores it.
- **Approval tokens are process-local** and lost on restart.
- **Caches are in-memory plus a code directory** — no persistence, no eviction beyond TTL.
