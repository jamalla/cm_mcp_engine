# cm_mcp_engine

**The hands.** Turns approved contracts into runnable code, sandboxes it, caches it, and serves the
result as MCP tools. Makes no decisions — it is handed a tool name and arguments and executes them.

Contracts are authored and reviewed in [`cm_mcp_contracts`](../cm_mcp_contracts). The routing agent
and UI live in [`cm_mcp_agent`](../cm_mcp_agent) and reach this service over MCP.

```bash
uv sync --extra dev
uv run pytest                 # 90 tests, no network, no ports
pwsh scripts/dev.ps1          # mock upstream :8787 + FastMCP :8765
pwsh scripts/dev.ps1 -Stop
```

## Where contracts come from

**The rule: this engine reads its own repository.** What it serves is
`./registry/registry.json` — the registry the pipeline published, `consume-registry` verified, and a
human merged here.

It used to auto-detect a sibling `../cm_mcp_contracts/contracts` checkout, which was convenient and
wrong. That directory holds whatever someone is editing, on whatever branch, including contracts that
never passed the gate — so an engine reading it serves unapproved tools while reporting a tool count
that looks perfectly legitimate. There is now no path to another repository in this one, and
[tests/test_own_repo_only.py](tests/test_own_repo_only.py) fails if one comes back.

Two overrides remain. Both take a deliberate environment variable, and both are reported as
unapproved by `list_contracts` and shown in amber in the UI:

| override | what it points at | for |
|---|---|---|
| `CM_REGISTRY_FILE` | a registry index built elsewhere | CI verifying a candidate; iterating locally |
| `CM_CONTRACTS_DIR` | a bare directory of contract files — no index, no hashes | this repo's own tests |

With neither set there is exactly one answer and it lives here. A fresh clone with no registry pinned
yet fails with "no registry at …, run the consume-registry workflow to pin one" rather than quietly
substituting whatever is nearby: serving nothing beats serving something unapproved.

**To iterate on a contract locally**, build a registry from your contracts checkout and point the
engine at it — same index-and-hashes shape the pipeline publishes, so local behaviour matches
production:

```bash
cd ../cm_mcp_contracts && uv run python scripts/build_registry.py --out ../local-registry
cd ../cm_mcp_engine && CM_REGISTRY_FILE=../local-registry/registry.json uv run python -m cm_engine.server
```

The engine prints which source it resolved at startup and reports it from `list_contracts`, because a
catalog that looks wrong is almost always a source that is not what you assumed.

`registry/` is absent until `consume-registry.yml` opens the first pin PR. That is the honest state
of a fresh clone: this engine serves the registry the contracts repo published, and nothing until it
has.

### The pinned registry is an index, not one big file

```
registry/
  registry.json                    provenance + a sha256 per contract
  contracts/list_categories.json   the contract, exactly as published
```

One file per contract because a registry of five hundred tools inlined into a single JSON file makes
the pin PR unreviewable — you cannot see *which* contract changed, `git blame` stops answering "who
changed this tool", and two registry updates touching different tools conflict.

The index is what makes that directory a registry rather than a folder. The engine serves **what the
index lists**, verified byte-for-byte:

| situation | what happens |
|---|---|
| file listed, hash matches | served |
| file edited after publication | refused, hash mismatch named in the warning |
| file present but unlisted | refused — unlisted means never published |
| file listed but missing | refused, named against its tool |
| index path escaping the registry | refused — an index is data from another repo |

So "approved" means "listed in an index the pipeline produced and a human merged", not "present on
disk". A registry pinned in the older single-file layout still loads, so nothing breaks before the
first new pin PR lands.

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

## How a credential reaches a call — and how it doesn't

Four rules, each of which is a security property rather than a preference:

1. **The contract names an upstream and a set of scopes. Nothing else.** No host, no credential, no
   way to express one. Least privilege is reviewable because the ask is in the diff.
2. **The generated module carries the *name* of an environment variable, never a value.**
   `TOKEN_ENV = 'SALLA_ACCESS_TOKEN'` and `headers["authorization"] = f"Bearer {os.environ.get(TOKEN_ENV, '')}"`.
   Generated code is cached on disk and shown to an audience; a token in it would be a token in both.
3. **The sandbox environment is an allowlist plus this tool's one credential.** A tool cannot read a
   secret it never asked for, and cannot read another upstream's at all.
4. **The principal — whose store, whose token, whose cached results — is decided by the engine, never
   by the arguments.** Tool arguments are written by a language model, so anything reachable from
   them is reachable by whoever can put text in front of it. `_principal()` in
   [server.py](cm_engine/server.py) is where a deployment maps an authenticated MCP session to the
   merchant install behind it. [tests/test_principal.py](tests/test_principal.py) pins the
   confused-deputy case: an argument called `principal` changes nothing.

Cache identity follows from the same reasoning. The result cache keys on the tool, the **generation**
(a digest of everything the code was generated from, engine-side resolution included), the
**principal**, and the arguments `keyBy` names — so a corrected contract cannot be answered from its
predecessor's results, turning `DEV_OFFLINE` off cannot serve a module aimed at the mock, and two
merchants asking the same question cannot see each other's data. The code cache omits the principal
on purpose: the module is identical for everyone precisely because the credential is not in it.

**What a production deployment replaces**, all behind `CredentialProvider.resolve` in
[credentials.py](cm_engine/credentials.py): one token per install rather than per process (Salla
issues one per store, with a refresh token and an expiry), storage in a secrets manager rather than a
process environment, and rotation. Nothing above that seam moves.

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
- **two caches** — keyed on a digest of *everything the code was generated from*, the engine's own
  upstream resolution included, because nothing forces a contributor to bump `contractVersion` when
  they correct a contract and nothing bumps it when `DEV_OFFLINE` changes the host. Results also key
  on the principal and the args `caching.keyBy` names, and are written *only* for a read-only tool
  that opted in. A write is never cached — decided from the HTTP verb, not from what the contract
  claims about itself.
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
uv run python scripts/check_registry.py registry/registry.json
uv run python scripts/check_registry.py --contracts-dir path/to/contract/files
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
- **One credential per upstream, and one principal.** The seam is right and the implementation is a
  single environment variable: `EnvCredentials` ignores the principal because there is only one, and
  `_principal()` returns a constant because there is no session to map. Multi-tenant needs both
  replaced — and until then the tenant-scoped cache keys are correct but untested against real
  traffic.
- **No token refresh.** Salla access tokens expire; nothing here renews one.
- **`retryable` is surfaced, not acted on.** The engine reports that retrying may help and leaves the
  decision to the caller.
- **`dependencies` is declared but not resolved** — the schema models it; the engine ignores it.
- **Approval tokens are process-local** and lost on restart.
- **Caches are in-memory plus a code directory** — no persistence, no eviction beyond TTL.
