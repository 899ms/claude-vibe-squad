# Board-spawned MCP surface

Status: canonical architecture guide and reproducible canary contract

A board-spawned worker's effective MCP surface comes from the role projection
selected for that worker. It is not the lane's capability declaration, and it
is not the server inventory reported by a separately started process.

This distinction was established with an in-process live-manifest probe. This
public guide preserves the finding and the exact method needed to reproduce it,
but intentionally publishes no host result, namespace total, installed-server
inventory, connector-family inventory, linked-account call, or dated machine
snapshot. Run the probe in your own worker and retain its result in the
appropriate local evidence store.

## Three surfaces, three questions

| Surface | Question it answers | Authority |
|---|---|---|
| Lane declaration | What is the maximum capability set this lane may support? | The lane capability registry |
| Role projection | Which capabilities should this role receive on this lane? | The selected role/lane capability source and generated adapter |
| Live worker manifest | Which tools can this running worker actually invoke? | The running worker's own tool manifest plus bounded liveness probes |

The familiar “3 vs 9” argument is a category error when one number comes from a
role projection and the other from a lane declaration. Neither number proves
the live surface. A standalone CLI's configured-server list is a third kind of
answer and does not prove what an already-running board worker received.

Configuration remains useful for setting the expected result. It does not
establish the observed result. Capability is proven only when the target worker
enumerates its own bound manifest and successfully performs an authorized,
bounded call through each reported namespace.

## Canary contract

For a correctly projected worker, the expected runtime namespace prefixes are
the prefixes corresponding to that worker's role projection. Build the expected
set from the role/lane projection selected by the dispatcher, not from the
lane-wide ceiling and not from a standalone process's configuration.

The controller must compare two independently obtained values:

1. **Expected:** the role projection resolved for the dispatched specialist and
   lane before launch.
2. **Observed:** the MCP namespace prefixes enumerated from the running worker's
   live tool manifest.

Do not place the expected prefix list in the probe packet. A worker that can
echo the expected answer without reading its manifest is not a useful canary.

Classify the result as follows:

- `PASS` only when the observed prefixes equal the expected role-projection
  prefixes and every observed namespace passes one bounded, read-only liveness
  probe.
- `FAIL` when a projected namespace is missing, an unprojected namespace is
  present, or an observed namespace cannot complete its bounded probe.
- `NOT_MEASURED` when the live manifest is unavailable, the worker identity or
  projection cannot be bound to the result, or the evidence is malformed.

An empty result is not a pass. The positive control is a known-good fixture or
worker whose manifest contains a synthetic namespace and whose bounded probe
succeeds. Run the same parser and comparison against that fixture; the control
must report `PASS` before a missing or empty real result can support a
conclusion.

## Reproduce the measurement

Supply the expected set as operator-local configuration, never as a committed
host answer. Set `CANARY_MCP_EXPECTED_JSON` to a sorted, unique, non-empty JSON
array, or write that array to `_state/canary-mcp-expected.json`; override the
file location with `CANARY_MCP_EXPECTED_FILE`. Run
`bin/canary.sh --emit-mcp-expectation-example` for an editable placeholder,
then derive the real set from the selected role projection in
`model-lanes/specialist-lane-capabilities.v1.json`, bounded by
`model-lanes/lane-capabilities.tsv`. Missing or invalid configuration reports
`NOT_MEASURED`, never a pass.

Run the inventory expression inside the board-spawned worker being measured.
Do not read TOML, TSV, JSON, an adapter, or a child process's settings as a
substitute for the live manifest.

Where the runtime exposes `ALL_TOOLS`, this expression enumerates its MCP server
prefixes without embedding an expected answer:

```javascript
const runtimeMcpPrefixes = [...new Set(
  ALL_TOOLS
    .map(tool => tool.name)
    .filter(name => name.startsWith("mcp__"))
    .map(name => name.split("__")[1])
)].sort();
text(JSON.stringify(runtimeMcpPrefixes));
```

If the runtime uses another manifest API, apply the same operation to that live
manifest: select MCP tools, extract the server prefix, deduplicate, and sort.
Record the literal expression or command with its literal output in the local
measurement artifact. If no live-manifest operation exists, report
`NOT_MEASURED`; do not fall back to configuration and call it runtime evidence.

After enumeration, choose one non-mutating liveness operation for each observed
namespace. Prefer a health, version, schema, or public metadata operation that
does not access a linked account. If a namespace offers no authorized
account-independent operation, record it as visible but unverified; do not make
an authenticated account call merely to make the canary pass.

The worker should return one machine-readable record containing:

```json
{
  "inventory_command": "<literal command or expression>",
  "server_prefixes": ["<sorted runtime prefix>"],
  "successful_probes": ["<sorted runtime prefix>"],
  "worker_binding": {
    "task_id": "<task identifier>",
    "attempt_id": "<attempt identifier>",
    "generation": "<dispatch generation>"
  }
}
```

The controller must reject duplicate or unsorted arrays, mismatched worker
bindings, missing request evidence, and results inferred from the response
instead of the persisted dispatch context. Store the actual prefix values and
probe details with local run evidence rather than copying them into this public
architecture guide.

## How to interpret drift

- **Expected but absent:** the role projection was not delivered, the launch
  override failed, or the namespace failed to initialize.
- **Observed but not expected:** the runtime or host added capability outside
  the role projection. Treat it as a boundary violation until an explicit
  projection and policy authorize it.
- **Visible but not callable:** discovery alone did not prove capability. Check
  authorization and initialization, then rerun a safe bounded probe.
- **Present only in a standalone process:** the result describes that process's
  configuration, not the board worker.

When investigating drift, trace the path from capability source to generated
adapter to launch allowlist to the bound worker manifest. The first three
explain what should happen; only the last one establishes what did happen.

## Publication boundary

This document is deliberately a transferable measurement protocol, not a map
of any operator's machine. Public documentation should state the architecture,
the canary contract, and the reproduction method. Host-specific namespace
values, counts, installed-server lists, connector inventories, authenticated
call evidence, and dated snapshots belong in access-controlled run evidence.
