# cm_mcp_engine

**The hands.** Turns approved contracts into runnable code, sandboxes it, caches it, and serves the
result as MCP tools. Makes no decisions — it is handed a tool name and arguments and executes them.

Contracts are authored and reviewed in [`cm_mcp_contracts`](../cm_mcp_contracts). The routing agent
and UI live in [`cm_mcp_agent`](../cm_mcp_agent) and reach this service over MCP.

```bash
uv sync --extra dev
uv run pytest                 # 45 tests, no network, no ports
pwsh scripts/dev.ps1          # mock partner API :8787 + FastMCP :8765
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

## What it does with them

- **code_mode** — deterministic template fill, no LLM. The contract already declares the API, the
  inputs, and the validation, so generation is repeatable and byte-stable, which is what makes the
  code cache trustworthy. The contract's regexes are compiled into the generated source as real
  guards.
- **sandbox** — subprocess, hard timeout, environment scrubbed to only the secrets the contract
  declared via `secretRef`. **Not a security boundary** (see gaps below).
- **two caches** — code keyed by `name@version`, so a version bump retires stale code for free;
  results keyed by `name@version` + the args `caching.keyBy` names, written *only* when the tool is
  read-only and cacheable. A destructive tool is never cached, and that rule lives in one function.
- **stage events** — each pipeline stage is emitted as an MCP log notification, which is how the
  agent's UI renders a live trace. On a cache hit `code_generated` and `executing` are not emitted
  at all, because they did not happen.

## Validation: schema vs executability

This repo deliberately does **not** re-validate contracts against the JSON Schema. That is the
contracts gate's job, and duplicating the rulebook would give us two versions of it to disagree.

What it checks instead is the question the other repo cannot answer: **can this engine run it?**

```bash
uv run python scripts/check_registry.py registry.generated.json
uv run python scripts/check_registry.py --contracts-dir ../cm_mcp_contracts/contracts
```

```
  OK  get_order_status@1.0.0    http    3648 chars
  ...
  - estimate_delivery_window: skipped -- no builtin handler registered for
    'builtin://forecast_weather' (this engine provides: [...])
```

`consume-registry.yml` runs exactly this against every newly published registry, then runs the MCP
surface tests against it, and only then opens a PR pinning it. A contract that is valid upstream
and unrunnable here fails a workflow instead of a demo.

## Adding a capability

A new `binding.type` or a new `builtin://` handler is a change **here**, and it must land before a
contract using it merges upstream — otherwise consume-registry rejects the registry. Add handlers to
`cm_engine/engine/builtins.py`; they are keyed by the exact `builtin://name` a contract references.

## Tests

Unit tests run against `tests/fixtures/contracts/` — a pinned set, not a sibling checkout, so this
repo tests green in a clone of itself. The real contracts are exercised by `check_registry.py`
against the published artifact, which is the more meaningful check anyway.

`tests/test_wire_contract.py` pins the stage-event envelope shared with `cm_mcp_agent`; the mirror
of that file lives in that repo. Change the shape in one place and one of the two goes red — which
is the only thing keeping two repositories honest about a protocol they do not share code for.

`tests/test_spike_fastmcp.py` pins the four FastMCP assumptions the live trace rests on. If an
upgrade breaks the demo, that file fails first.

## POC gaps

- **The sandbox is not a security boundary.** A subprocess with a timeout and a scrubbed
  environment stops accidents and runaway loops, not untrusted code.
- **`dependencies` is declared but not resolved** — the schema models it; the engine ignores it.
- **Approval tokens are process-local** and lost on restart.
- **Caches are in-memory plus a code directory** — no persistence, no eviction beyond TTL.
- **No partner authn/authz.** Secrets are read from the environment by name.
