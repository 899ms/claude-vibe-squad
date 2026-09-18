#!/usr/bin/env python3
"""Reject public capability declarations backed by policy-withheld plugins."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "python"))
sys.path.insert(0, str(ROOT / "tools" / "export"))

from path_policy import Policy, load_policy  # noqa: E402
from specialist_capability_source import (  # noqa: E402
    SOURCE_RELATIVE,
    is_usable_capability,
    load_source,
)
from validate_capability_homes import _adapter_globs, load_adapters  # noqa: E402


def declarations(root: Path) -> list[tuple[str, str, str]]:
    """Read executable surfaces with the same parsers used by their validators.

    Known-unavailable entries and the provider catalogue describe possible
    integrations; neither is an executable assignment. Operation names are
    resolved through that catalogue so an adapter cannot bypass the check by
    advertising a withheld server's operation instead of its MCP identifier.
    """
    result: list[tuple[str, str, str]] = []
    entries, payload = load_source(root)
    providers: dict[str, set[str]] = {}
    for server in payload["servers"]:
        for operation in server["provides"]:
            providers.setdefault(operation, set()).add(server["id"])

    def add(path: str, field: str, identifier: str) -> None:
        result.append((path, field, identifier))

    def operations(path: str, identifiers: list[str] | tuple[str, ...]) -> None:
        for identifier in identifiers:
            for provider in sorted(providers.get(identifier, ())):
                add(path, f"tools.{identifier}.provider", provider)

    for (specialist, lane), entry in entries.items():
        for kind in ("mcps", "tools"):
            for ref in entry[kind]:
                if is_usable_capability(kind, ref.availability) or ref.availability == "mcp-operation":
                    field = f"{specialist}.{lane}.{kind}.{ref.identifier}"
                    if kind == "mcps":
                        add(SOURCE_RELATIVE.as_posix(), field, ref.identifier)
                    elif ref.provided_by:
                        add(SOURCE_RELATIVE.as_posix(), field, ref.provided_by)
                    else:
                        operations(SOURCE_RELATIVE.as_posix(), [ref.identifier])

    # Include unrouted adapters too: absence from the routing map must not hide
    # a declaration in a native agent that a public user can load directly.
    names = {path.stem.replace("_", "-") if lane == "gpt-codex" else path.stem: {}
             for lane, path in _adapter_globs(root)}
    adapters, errors = load_adapters(root, names)
    if errors:
        raise ValueError(f"cannot audit malformed adapters: {json.dumps(errors)}")
    for adapter in adapters.values():
        for identifier in adapter["mcps"]:
            add(adapter["adapter"], "mcps", identifier)
        operations(adapter["adapter"], adapter["tools"])

    index_path = "model-lanes/generated-specialist-capabilities.json"
    for entry in json.loads((root / index_path).read_text())["entries"]:
        for identifier in entry["mcps"]:
            add(index_path, "mcps", identifier)
        operations(index_path, entry["tools"])
        for plan in entry.get("primary_plan", []):
            if plan["kind"] == "mcps":
                add(index_path, "primary_plan", plan["capability_id"])

    lane_path = "model-lanes/lane-capabilities.tsv"
    with (root / lane_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            for identifier in json.loads(row["mcp_surface"]):
                add(lane_path, f"{row['lane']}.mcp_surface", identifier)

    runtime_path = "shared/specialist-runtime-map.tsv"
    with (root / runtime_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            for field in ("required_tools", "preferred_tools"):
                value = row[field]
                if not (value.startswith("[") and value.endswith("]")):
                    raise ValueError(f"malformed runtime tool list: {row['specialist']}.{field}")
                for identifier in value[1:-1].split(","):
                    if identifier.strip():
                        add(runtime_path, field, identifier.strip())
    return sorted(set(result))


def audit(root: Path, policy: Policy | None = None) -> list[dict[str, str]]:
    policy = policy or load_policy(root / "tools/export/policy/path-policy.json")
    issues = []
    for path, field, identifier in declarations(root):
        if policy.classify(path) != "public":
            continue
        server = identifier.removeprefix("lead:")
        # These are the repository plugin entry points, also used by the
        # marketplace projector and the local MCP transport templates. Check
        # paths against policy even when the withheld tree is absent locally.
        for suffix in (".claude-plugin/plugin.json", "mcp_server.py"):
            implementation = f"plugins/{server}/{suffix}"
            decision = policy.decision(implementation)
            if decision.classification != "public":
                issues.append({"path": path, "field": field, "identifier": identifier,
                               "implementation": implementation,
                               "rule": decision.policy_pattern})
                break
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        issues = audit(args.repo_root.resolve())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"public-plugin-capabilities: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "fail" if issues else "pass",
                      "promise_files": len({issue["path"] for issue in issues}),
                      "diagnostics": issues}, indent=2, sort_keys=True))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
