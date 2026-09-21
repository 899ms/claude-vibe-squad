"""Project capability assignments into a disposable public candidate.

The provider catalogue remains intact: it is also the public promise detector's
operation dictionary. Only assignments and their generated surfaces change.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/python"))
sys.path.insert(0, str(ROOT / "tools/export"))

from path_policy import Policy  # noqa: E402
import lane_adapter_registry as registry  # noqa: E402
import validate_capability_homes as homes  # noqa: E402
from gen_runtime_tool_summary import render_runtime_map  # noqa: E402
from specialist_capability_source import SOURCE_RELATIVE, load_source  # noqa: E402


def project_public_capabilities(root: Path, policy: Policy) -> tuple[str, ...]:
    """Rewrite existing candidate files only; never pass the private checkout."""
    source = root / SOURCE_RELATIVE
    if not source.is_file():
        return ()
    # Validate the input before transforming it; malformed declarations must
    # not become apparently valid by being filtered out.
    _entries, payload = load_source(root)
    changed: list[str] = []

    def write(path: Path, content: str) -> None:
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or path.is_symlink() or policy.classify(relative) != "public":
            raise ValueError(f"capability projection cannot create or replace {relative}")
        if path.read_text(encoding="utf-8") != content:
            registry.atomic_write_text(path, content)
            changed.append(relative)

    def withheld(identifier: str) -> bool:
        server = identifier.removeprefix("lead:")
        return any(policy.classify(f"plugins/{server}/{suffix}") != "public"
                   for suffix in (".claude-plugin/plugin.json", "mcp_server.py"))

    withheld_providers = {server["id"] for server in payload["servers"]
                          if withheld(server["id"])}
    for entry in payload["entries"]:
        for ref in entry["mcps"]:
            if withheld(ref["id"]):
                withheld_providers.add(ref["id"])
                ref.update(requirement="preferred", availability="needs-operator-install",
                           evidence="operator-install-required")
        for ref in entry["tools"]:
            if ref.get("provided_by") and withheld(ref["provided_by"]):
                ref.pop("provided_by")
                ref.update(requirement="preferred", availability="needs-operator-install",
                           evidence="operator-install-required")
    payload["public_projection"] = {"withheld_providers": sorted(withheld_providers)}
    write(source, json.dumps(payload, indent=2) + "\n")

    lane_path = root / "model-lanes/lane-capabilities.tsv"
    with lane_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        header = reader.fieldnames
        rows = list(reader)
    for row in rows:
        for field in ("mcp_surface", "staged_mcp_surface"):
            row[field] = json.dumps([identifier for identifier in json.loads(row[field])
                                     if not withheld(identifier)], separators=(",", ":"))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=header, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    write(lane_path, output.getvalue())

    entries, _payload = load_source(root)
    for specialist, lane in sorted(entries):
        target = registry._target(root, lane, specialist)
        if not target.is_file():
            continue  # Projection never recreates policy-withheld adapters.
        existing = target.read_text(encoding="utf-8")
        if ((specialist in registry.ADVISOR_GENERATED_ROLES and lane in {"claude", "gpt-codex"})
                or (specialist in registry.GROK_GENERATED_ROLES and lane == "grok")):
            rendered = registry.render_adapter(root, lane, specialist)
        else:
            rendered = registry.upsert_capability_projection(root, lane, specialist, existing)
        write(target, rendered)
        if lane == "grok":
            prompt = root / "model-lanes/grok/.grok/prompts" / f"{specialist}.md"
            if prompt.is_file():
                write(prompt, registry.render_grok_prompt(root, specialist))

    write(root / "shared/specialist-runtime-map.tsv", render_runtime_map(root))
    adapters, issues = homes.load_adapters(root, homes.runtime_rows(root))
    if issues:
        raise ValueError(f"cannot project malformed capability adapters: {issues}")
    write(root / homes.INDEX_RELATIVE, homes.render_index(
        root, adapters, homes.load_policy(root),
        lane_inventory=homes.load_lane_inventory(root), source_entries=entries,
    ))
    return tuple(changed)
